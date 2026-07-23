from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import sys
from typing import Any


SCRIPT_ROOT = Path(__file__).resolve().parent
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

import codex_update_guard as guard  # noqa: E402


def scan_rollouts(codex_home: Path) -> dict[str, Any]:
    roots = [
        codex_home / "sessions",
        codex_home / "archived_sessions",
    ]
    source_counts: Counter[str] = Counter()
    total = 0
    invalid = 0
    invalid_examples: list[dict[str, str]] = []
    missing_cwd_total = 0
    missing_cwd: list[dict[str, Any]] = []
    for root in roots:
        if not root.is_dir():
            continue
        for path in root.rglob("*.jsonl"):
            total += 1
            try:
                with path.open("r", encoding="utf-8-sig") as stream:
                    first = json.loads(stream.readline())
                if (
                    not isinstance(first, dict)
                    or first.get("type") != "session_meta"
                    or not isinstance(first.get("payload"), dict)
                ):
                    raise ValueError("missing session_meta payload")
                payload = first["payload"]
                source = payload.get("source")
                if isinstance(source, str):
                    source_counts[source] += 1
                elif isinstance(source, dict):
                    if source.get("subagent") is not None:
                        source_counts["subagent"] += 1
                    elif source.get("cli") is not None:
                        source_counts["cli"] += 1
                    elif isinstance(source.get("type"), str):
                        source_counts[source["type"]] += 1
                    else:
                        source_counts["object"] += 1
                else:
                    source_counts["unknown"] += 1
                cwd = payload.get("cwd")
                if (
                    isinstance(cwd, str)
                    and "\x00" not in cwd
                    and not Path(cwd).is_dir()
                ):
                    missing_cwd_total += 1
                    if len(missing_cwd) < 100:
                        missing_cwd.append(
                            {
                                "thread_id": payload.get("id"),
                                "cwd": cwd,
                                "rollout": str(path),
                            }
                        )
            except (
                OSError,
                UnicodeError,
                json.JSONDecodeError,
                ValueError,
            ) as error:
                invalid += 1
                if len(invalid_examples) < 20:
                    invalid_examples.append(
                        {
                            "rollout": str(path),
                            "error": f"{type(error).__name__}: {error}",
                        }
                    )
    return {
        "roots": [str(path) for path in roots],
        "rollout_count": total,
        "invalid_rollout_count": invalid,
        "invalid_rollout_examples": invalid_examples,
        "source_counts": dict(sorted(source_counts.items())),
        "missing_cwd_count": missing_cwd_total,
        "missing_cwd_example_count": len(missing_cwd),
        "missing_cwd_examples": missing_cwd,
    }


def database_projection_check(
    codex_home: Path,
) -> dict[str, Any]:
    metrics = {
        "legacy": guard.database_health(codex_home / "state_5.sqlite"),
        "app": guard.database_health(
            codex_home / "sqlite" / "state_5.sqlite"
        ),
    }
    counts = [
        detail.get("thread_count")
        for detail in metrics.values()
        if isinstance(detail.get("thread_count"), int)
    ]
    difference = abs(counts[0] - counts[1]) if len(counts) == 2 else None
    tolerance = max(5, int(max(counts) * 0.05)) if counts else None
    status = "ok"
    if len(counts) != 2:
        status = "error"
    elif difference is not None and tolerance is not None and difference > tolerance:
        status = "warning"
    return guard.check_record(
        "database_projection",
        status,
        {
            "databases": metrics,
            "absolute_thread_count_difference": difference,
            "warning_tolerance": tolerance,
            "note": (
                "两个数据库允许少量异步投影差异；显著差异需结合界面与日志复核。"
            ),
        },
        critical=status == "error",
    )


def load_guard_metadata(codex_home: Path) -> dict[str, Any]:
    install_root = codex_home / "maintenance" / "update-guard"
    backup_root = codex_home / "backups_state" / "update-guard"
    required_install_files = (
        "codex_update_guard.py",
        "Invoke-CodexUpdateGuard.ps1",
        "Invoke-CodexUpdateMaintenance.ps1",
        "Restore-CodexLastHealthy.ps1",
    )
    install_files = {
        name: (install_root / name).is_file()
        for name in required_install_files
    }
    healthy_root = backup_root / "healthy"
    healthy_snapshot_count = 0
    if healthy_root.is_dir():
        try:
            healthy_snapshot_count = sum(
                1 for path in healthy_root.iterdir() if path.is_dir()
            )
        except OSError:
            healthy_snapshot_count = 0
    result: dict[str, Any] = {
        "install_root": str(install_root),
        "backup_root": str(backup_root),
        "installed": install_root.is_dir() and all(install_files.values()),
        "install_files": install_files,
        "backup_root_exists": backup_root.is_dir(),
        "healthy_snapshot_count": healthy_snapshot_count,
    }
    for name in ("expected-state.json", "guard-state.json"):
        path = install_root / name
        if not path.is_file():
            result[name] = {"exists": False}
            continue
        try:
            value = guard.load_json_object(path)
            result[name] = {
                "exists": True,
                "valid_json_object": True,
                "schema_version": value.get("schemaVersion"),
            }
        except (
            OSError,
            UnicodeError,
            ValueError,
            json.JSONDecodeError,
        ) as error:
            result[name] = {
                "exists": True,
                "valid_json_object": False,
                "error": f"{type(error).__name__}: {error}",
            }
    return result


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(value, ensure_ascii=False, indent=2)
    path.write_bytes(b"\xef\xbb\xbf" + rendered.encode("utf-8") + b"\n")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only audit of Codex Windows configuration, project state, "
            "thread databases, rollout metadata, plugin caches, and guard metadata."
        )
    )
    parser.add_argument(
        "--codex-home",
        type=Path,
        default=Path.home() / ".codex",
    )
    parser.add_argument("--package-version")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    codex_home = args.codex_home.resolve()
    cache = guard.plugin_cache_health(codex_home)
    enabled_cache_names: set[str] = set()
    try:
        config, _ = guard.read_config(codex_home / "config.toml")
        plugins = config.get("plugins")
        if isinstance(plugins, dict):
            enabled_cache_names = {
                name.split("@", 1)[0]
                for name, value in plugins.items()
                if isinstance(value, dict) and value.get("enabled") is True
            }
    except (
        OSError,
        UnicodeError,
        ValueError,
    ):
        pass
    expected = {
        "requiredPluginCaches": sorted(
            name
            for name, detail in cache.items()
            if detail.get("available") or name in enabled_cache_names
        )
    }
    health, _ = guard.evaluate_health(
        codex_home,
        expected,
        {},
        args.package_version,
    )
    health["checks"].append(database_projection_check(codex_home))
    health["status"] = guard.overall_status(health["checks"])
    result = {
        "schema_version": 1,
        "mode": "read-only",
        "codex_home": str(codex_home),
        "health": health,
        "rollouts": scan_rollouts(codex_home),
        "guard_metadata": load_guard_metadata(codex_home),
        "interpretation": {
            "healthy": "本次只读检查未发现状态层面的异常。",
            "degraded": "存在需要人工解释的漂移或缺失；不要直接覆盖文件。",
            "critical": "存在关键文件、数据库或结构错误；修复前先做可验证备份。",
        }.get(health["status"], "未知状态"),
    }
    if args.output:
        write_json(args.output, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if health["status"] == "healthy" else 1


if __name__ == "__main__":
    raise SystemExit(main())
