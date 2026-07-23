from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime
import json
import os
from pathlib import Path
import re
from typing import Any


CWD_PATTERN = re.compile(r'\bcwd=(?:"([^"]+)"|([^ ]+))')


def canonical_path(value: str | Path) -> str:
    return os.path.normcase(os.path.normpath(str(value)))


def load_json_object(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    if b"\x00" in raw:
        raise ValueError(f"{path} contains NUL bytes")
    value = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} does not contain a JSON object")
    return value


def current_projects(
    state_path: Path,
) -> tuple[dict[str, str], dict[str, Any]]:
    state = load_json_object(state_path)
    roots: dict[str, str] = {}
    projects = state.get("local-projects", {})
    if isinstance(projects, dict):
        for project_id, project in projects.items():
            if not isinstance(project, dict):
                continue
            root_paths = project.get("rootPaths", [])
            if not isinstance(root_paths, list):
                continue
            for root in root_paths:
                if isinstance(root, str):
                    roots[canonical_path(root)] = str(project_id)
    return roots, state


def scan_sidebar_logs(log_root: Path) -> dict[str, dict[str, Any]]:
    hits: dict[str, list[dict[str, str]]] = defaultdict(list)
    display_paths: dict[str, str] = {}
    if not log_root.is_dir():
        return {}
    for log_path in log_root.rglob("*.log"):
        try:
            text = log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line_number, line in enumerate(text.splitlines(), start=1):
            if "source=sidebar_workspace_groups" not in line:
                continue
            match = CWD_PATTERN.search(line)
            if not match:
                continue
            cwd = match.group(1) or match.group(2)
            if cwd.casefold().endswith(r"\.git"):
                cwd = cwd[:-5]
            key = canonical_path(cwd)
            display_paths[key] = cwd
            hits[key].append(
                {
                    "timestamp": line[:24],
                    "log": str(log_path),
                    "line": str(line_number),
                }
            )

    result: dict[str, dict[str, Any]] = {}
    for key, items in hits.items():
        timestamps = [item["timestamp"] for item in items]
        display_path = display_paths[key]
        result[key] = {
            "root": display_path,
            "exists": Path(display_path).is_dir(),
            "has_git": (Path(display_path) / ".git").exists(),
            "first_seen": min(timestamps),
            "last_seen": max(timestamps),
            "evidence_count": len(items),
            "last_evidence": max(items, key=lambda item: item["timestamp"]),
        }
    return result


def scan_session_cwds(
    session_roots: list[Path],
) -> dict[str, dict[str, Any]]:
    hits: dict[str, list[dict[str, Any]]] = defaultdict(list)
    display_paths: dict[str, str] = {}
    for session_root in session_roots:
        if not session_root.is_dir():
            continue
        for rollout_path in session_root.rglob("*.jsonl"):
            try:
                with rollout_path.open("r", encoding="utf-8-sig") as stream:
                    first_line = stream.readline()
                first = json.loads(first_line)
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            if not isinstance(first, dict) or first.get("type") != "session_meta":
                continue
            payload = first.get("payload")
            if not isinstance(payload, dict):
                continue
            cwd = payload.get("cwd")
            if not isinstance(cwd, str) or "\x00" in cwd:
                continue
            key = canonical_path(cwd)
            display_paths[key] = cwd
            timestamp = payload.get("timestamp") or first.get("timestamp")
            if not isinstance(timestamp, str):
                try:
                    timestamp = datetime.fromtimestamp(
                        rollout_path.stat().st_mtime
                    ).astimezone().isoformat()
                except OSError:
                    continue
            hits[key].append(
                {
                    "timestamp": timestamp,
                    "thread_id": payload.get("id"),
                    "rollout": str(rollout_path),
                }
            )

    result: dict[str, dict[str, Any]] = {}
    for key, items in hits.items():
        timestamps = [str(item["timestamp"]) for item in items]
        display_path = display_paths[key]
        result[key] = {
            "root": display_path,
            "exists": Path(display_path).is_dir(),
            "thread_count": len(items),
            "first_thread": min(timestamps),
            "last_thread": max(timestamps),
            "latest_thread_id": max(
                items, key=lambda item: str(item["timestamp"])
            ).get("thread_id"),
        }
    return result


def classify_candidate(
    root: str,
    *,
    profile: Path,
    codex_home: Path,
    sidebar_evidence: dict[str, Any] | None,
) -> tuple[str, list[str], bool]:
    path = Path(root)
    key = canonical_path(path)
    broad_roots = {
        canonical_path(profile),
        canonical_path(profile / "Desktop"),
        canonical_path(profile / "Documents"),
        canonical_path(profile / "Documents" / "Codex"),
    }
    reasons: list[str] = []
    eligible_for_review = False

    try:
        path.resolve().relative_to((codex_home / "worktrees").resolve())
        reasons.append("Codex 临时 worktree；不应自动恢复为长期项目")
        return "excluded", reasons, False
    except (OSError, ValueError):
        pass

    if key in broad_roots:
        reasons.append("路径范围过宽，可能只是启动目录")
        return "low", reasons, False
    if not path.is_dir():
        reasons.append("目录当前不存在")
        return "low", reasons, False
    if sidebar_evidence:
        count = int(sidebar_evidence.get("evidence_count", 0))
        if (path / ".git").exists() and count >= 1:
            reasons.append("存在侧栏日志证据且目录是 Git 工作区")
            eligible_for_review = True
            return "high", reasons, eligible_for_review
        if count >= 2:
            reasons.append("存在多次侧栏工作区日志证据")
            eligible_for_review = True
            return "medium", reasons, eligible_for_review
        reasons.append("仅有一次侧栏日志证据")
        return "low", reasons, False
    reasons.append("仅在线程元数据中出现；不能证明曾固定在侧栏")
    return "low", reasons, False


def default_log_root() -> Path:
    local_app_data = Path(
        os.environ.get(
            "LOCALAPPDATA",
            str(Path.home() / "AppData" / "Local"),
        )
    )
    return (
        local_app_data
        / "Packages"
        / "OpenAI.Codex_2p2nqsd0c76g0"
        / "LocalCache"
        / "Local"
        / "Codex"
        / "Logs"
    )


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(value, ensure_ascii=False, indent=2)
    path.write_bytes(b"\xef\xbb\xbf" + rendered.encode("utf-8") + b"\n")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only discovery of Codex sidebar project candidates. "
            "The script never edits global state."
        )
    )
    parser.add_argument(
        "--codex-home",
        type=Path,
        default=Path.home() / ".codex",
    )
    parser.add_argument("--state", type=Path)
    parser.add_argument("--log-root", type=Path)
    parser.add_argument(
        "--session-root",
        type=Path,
        action="append",
        default=[],
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    codex_home = args.codex_home.resolve()
    state_path = args.state or codex_home / ".codex-global-state.json"
    log_root = args.log_root or default_log_root()
    session_roots = args.session_root or [
        codex_home / "sessions",
        codex_home / "archived_sessions",
    ]

    current_roots, state = current_projects(state_path)
    sidebar = scan_sidebar_logs(log_root)
    sessions = scan_session_cwds(session_roots)
    all_keys = sorted(set(sidebar) | set(sessions))
    rows: list[dict[str, Any]] = []
    for key in all_keys:
        sidebar_row = sidebar.get(key)
        session_row = sessions.get(key)
        root = str(
            (sidebar_row or {}).get("root")
            or (session_row or {}).get("root")
            or key
        )
        confidence, reasons, eligible = classify_candidate(
            root,
            profile=Path.home(),
            codex_home=codex_home,
            sidebar_evidence=sidebar_row,
        )
        rows.append(
            {
                "root": root,
                "already_present": key in current_roots,
                "current_project_id": current_roots.get(key),
                "confidence": confidence,
                "eligible_for_manual_merge_review": (
                    eligible and key not in current_roots
                ),
                "reasons": reasons,
                "sidebar_evidence": sidebar_row,
                "session_evidence": session_row,
            }
        )

    missing_reviewable = [
        row
        for row in rows
        if row["eligible_for_manual_merge_review"]
    ]
    result = {
        "schema_version": 1,
        "mode": "read-only",
        "codex_home": str(codex_home),
        "state_path": str(state_path),
        "log_root": str(log_root),
        "session_roots": [str(path) for path in session_roots],
        "current_project_count": len(
            state.get("local-projects", {})
            if isinstance(state.get("local-projects"), dict)
            else {}
        ),
        "candidate_count": len(rows),
        "manual_merge_review_count": len(missing_reviewable),
        "manual_merge_review": missing_reviewable,
        "all_candidates": sorted(
            rows,
            key=lambda row: (
                row["confidence"] != "high",
                row["confidence"] != "medium",
                row["root"].casefold(),
            ),
        ),
        "safety": (
            "候选项仅用于人工复核；不要把仅有线程证据、宽泛目录、"
            "不存在目录或 Codex 临时 worktree 自动写入侧栏。"
        ),
    }
    if args.output:
        write_json(args.output, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
