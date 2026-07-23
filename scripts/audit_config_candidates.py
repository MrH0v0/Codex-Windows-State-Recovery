from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tomllib
from typing import Any


SENSITIVE_TOKENS = {
    "api_key",
    "apikey",
    "authorization",
    "bearer",
    "credential",
    "password",
    "secret",
    "token",
}

SAFE_SCALAR_KEYS = {
    "approval_policy",
    "base_url",
    "command",
    "disabled",
    "enabled",
    "env_key",
    "experimental_use_rmcp_client",
    "model",
    "model_provider",
    "model_reasoning_effort",
    "notify",
    "personality",
    "preferred_auth_method",
    "requires_openai_auth",
    "sandbox",
    "sandbox_mode",
    "service_tier",
    "source",
    "source_type",
    "startup_timeout_sec",
    "tool_timeout_sec",
    "transport",
    "trust_level",
    "url",
    "wire_api",
}


def candidate_paths(search_roots: list[Path]) -> list[Path]:
    paths: dict[str, Path] = {}
    for root in search_roots:
        if not root.exists():
            continue
        for pattern in ("config*.toml", "config*.toml.*", "*.toml.bak"):
            for path in root.rglob(pattern):
                if not path.is_file():
                    continue
                try:
                    paths[str(path.resolve()).casefold()] = path
                except OSError:
                    continue
    return sorted(
        paths.values(),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )


def is_sensitive(path: tuple[str, ...]) -> bool:
    joined = ".".join(path).casefold()
    return any(token in joined for token in SENSITIVE_TOKENS)


def display_value(path: tuple[str, ...], value: Any) -> Any:
    if is_sensitive(path):
        return "<redacted>"
    if not path or path[-1].casefold() not in SAFE_SCALAR_KEYS:
        return f"<{type(value).__name__}>"
    if isinstance(value, str) and len(value) > 300:
        return value[:300] + "…"
    return value


def flatten(value: Any, prefix: tuple[str, ...] = ()) -> dict[str, Any]:
    result: dict[str, Any] = {}
    if isinstance(value, dict):
        for key, child in value.items():
            result.update(flatten(child, prefix + (str(key),)))
        return result
    if isinstance(value, list):
        result[".".join(prefix)] = f"<list:{len(value)}>"
        return result
    result[".".join(prefix)] = display_value(prefix, value)
    return result


def analyze(path: Path, codex_home: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    nonzero = sum(byte != 0 for byte in raw)
    modified = datetime.fromtimestamp(
        path.stat().st_mtime,
        tz=timezone.utc,
    ).isoformat()
    result: dict[str, Any] = {
        "path": str(path),
        "is_live_config": path.resolve() == (codex_home / "config.toml").resolve(),
        "size": len(raw),
        "nonzero_bytes": nonzero,
        "contains_nul": b"\x00" in raw,
        "modified_utc": modified,
        "sha256": hashlib.sha256(raw).hexdigest(),
    }
    if not raw or not nonzero:
        result["parse_error"] = "empty-or-nul-filled"
        result["score"] = -100
        return result
    if b"\x00" in raw:
        result["parse_error"] = "contains-nul-bytes"
        result["score"] = -100
        return result
    try:
        parsed = tomllib.loads(raw.decode("utf-8-sig"))
    except (UnicodeError, tomllib.TOMLDecodeError) as error:
        result["parse_error"] = f"{type(error).__name__}: {error}"
        result["score"] = -100
        return result

    flat = flatten(parsed)
    result["top_level_keys"] = sorted(parsed)
    result["redacted_flat_view"] = flat
    path_checks: list[dict[str, Any]] = []
    for dotted, value in flat.items():
        if value == "<redacted>" or not isinstance(value, str):
            continue
        leaf = dotted.rsplit(".", 1)[-1].casefold()
        if leaf not in {"command", "source"}:
            continue
        expanded = os.path.expandvars(value)
        if "\\" not in expanded and "/" not in expanded:
            continue
        path_checks.append(
            {
                "key": dotted,
                "value": value,
                "exists": Path(expanded).exists(),
            }
        )
    result["path_checks"] = path_checks
    stale_count = sum(not item["exists"] for item in path_checks)
    result["score"] = (
        50
        + min(len(parsed), 20)
        + min(len(raw) // 1024, 20)
        - stale_count * 5
        + (5 if result["is_live_config"] else 0)
    )
    result["warning"] = (
        "分数只用于缩小人工复核范围，不代表可整文件覆盖当前配置。"
    )
    return result


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(value, ensure_ascii=False, indent=2)
    path.write_bytes(b"\xef\xbb\xbf" + rendered.encode("utf-8") + b"\n")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only, redacted analysis of Codex config.toml candidates."
        )
    )
    parser.add_argument(
        "--codex-home",
        type=Path,
        default=Path.home() / ".codex",
    )
    parser.add_argument(
        "--search-root",
        type=Path,
        action="append",
        default=[],
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    codex_home = args.codex_home.resolve()
    search_roots = args.search_root or [
        codex_home,
        codex_home / "backups_state",
    ]
    analyses = [
        analyze(path, codex_home)
        for path in candidate_paths(search_roots)
    ]
    valid = [
        entry for entry in analyses if "parse_error" not in entry
    ]
    ranked = sorted(
        valid,
        key=lambda entry: (
            int(entry["score"]),
            str(entry["modified_utc"]),
        ),
        reverse=True,
    )
    result = {
        "schema_version": 1,
        "mode": "read-only-redacted",
        "codex_home": str(codex_home),
        "search_roots": [str(path) for path in search_roots],
        "candidate_count": len(analyses),
        "valid_count": len(valid),
        "ranked_valid_candidates": ranked,
        "invalid_candidates": [
            entry for entry in analyses if "parse_error" in entry
        ],
        "merge_policy": (
            "仅按键人工并入已经验证的耐久配置；禁止用候选文件整份覆盖，"
            "禁止从报告恢复已脱敏的秘密值。"
        ),
    }
    if args.output:
        write_json(args.output, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if valid else 1


if __name__ == "__main__":
    raise SystemExit(main())
