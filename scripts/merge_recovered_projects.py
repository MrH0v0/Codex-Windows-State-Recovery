from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
import tomllib
from typing import Any


def load_json_object(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    if b"\x00" in raw:
        raise ValueError(f"{path} contains NUL bytes")
    value = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} does not contain a JSON object")
    return value


def canonical_path(value: str) -> str:
    return os.path.normcase(os.path.normpath(value))


def project_id(root_path: str) -> str:
    digest = hashlib.sha256(root_path.encode("utf-8")).hexdigest()
    return f"local-{digest[:32]}"


def atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.merge-",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    rendered = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8") + b"\n"
    atomic_write_bytes(path, rendered)


def toml_literal_key(value: str) -> str:
    if "'" not in value and "\n" not in value and "\r" not in value:
        return f"'{value}'"
    return json.dumps(value, ensure_ascii=False)


def is_path_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except (OSError, ValueError):
        return False


def require_project_manifest(
    path: Path,
    *,
    allow_broad_roots: bool,
) -> list[dict[str, Any]]:
    manifest = load_json_object(path)
    projects = manifest.get("projects")
    if not isinstance(projects, list) or not projects:
        raise ValueError("Recovery manifest does not contain projects")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, project in enumerate(projects):
        if not isinstance(project, dict):
            raise ValueError(f"projects[{index}] is not an object")
        name = project.get("name")
        root_path = project.get("rootPath")
        created_at = project.get("createdAt")
        if (
            not isinstance(name, str)
            or not name.strip()
            or "\x00" in name
        ):
            raise ValueError(f"projects[{index}].name is invalid")
        if (
            not isinstance(root_path, str)
            or not root_path
            or "\x00" in root_path
        ):
            raise ValueError(f"projects[{index}].rootPath is invalid")
        root = Path(root_path)
        if not root.is_absolute():
            raise ValueError(
                f"projects[{index}].rootPath must be absolute"
            )
        if not isinstance(created_at, int) or created_at <= 0:
            raise ValueError(f"projects[{index}].createdAt is invalid")
        if not root.is_dir():
            raise ValueError(f"Recovered project directory is missing: {root_path}")
        broad_roots = {
            canonical_path(Path.home()),
            canonical_path(Path.home() / "Desktop"),
            canonical_path(Path.home() / "Documents"),
            canonical_path(Path.home() / "Documents" / "Codex"),
        }
        if (
            not allow_broad_roots
            and canonical_path(root) in broad_roots
        ):
            raise ValueError(
                "Refusing a broad project root without "
                f"--allow-broad-roots: {root_path}"
            )
        codex_worktrees = Path.home() / ".codex" / "worktrees"
        if is_path_within(root, codex_worktrees):
            raise ValueError(
                f"Refusing a temporary Codex worktree: {root_path}"
            )
        key = canonical_path(root_path)
        if key in seen:
            raise ValueError(f"Duplicate recovered project root: {root_path}")
        seen.add(key)
        result.append(project)
    return result


def merge_state(
    state: dict[str, Any],
    recovered_projects: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    state = copy.deepcopy(state)
    local_projects = state.get("local-projects")
    if not isinstance(local_projects, dict):
        raise ValueError("Current global state local-projects is not an object")
    project_order = state.get("project-order")
    if not isinstance(project_order, list) or not all(
        isinstance(item, str) for item in project_order
    ):
        raise ValueError("Current global state project-order is not a string list")
    original_order = list(project_order)
    duplicate_order_ids: list[str] = []
    unknown_order_ids: list[str] = []
    normalized_order: list[str] = []
    seen_order_ids: set[str] = set()
    for project_id_value in original_order:
        if project_id_value not in local_projects:
            unknown_order_ids.append(project_id_value)
            continue
        if project_id_value in seen_order_ids:
            duplicate_order_ids.append(project_id_value)
            continue
        normalized_order.append(project_id_value)
        seen_order_ids.add(project_id_value)
    missing_existing_order_ids = [
        str(existing_id)
        for existing_id in local_projects
        if str(existing_id) not in seen_order_ids
    ]
    normalized_order.extend(missing_existing_order_ids)
    project_order = normalized_order

    roots_by_key: dict[str, str] = {}
    for existing_id, project in local_projects.items():
        if not isinstance(project, dict):
            continue
        roots = project.get("rootPaths")
        if not isinstance(roots, list):
            continue
        for root in roots:
            if isinstance(root, str):
                roots_by_key[canonical_path(root)] = str(existing_id)

    legacy_roots = state.get("electron-saved-workspace-roots", [])
    if not isinstance(legacy_roots, list) or not all(
        isinstance(item, str) for item in legacy_roots
    ):
        raise ValueError(
            "Current global state electron-saved-workspace-roots "
            "is not a string list"
        )
    legacy_root_keys = {canonical_path(root) for root in legacy_roots}

    added: list[dict[str, Any]] = []
    already_present: list[dict[str, Any]] = []
    repaired_order: list[str] = []
    repaired_legacy_roots: list[str] = []
    for recovered in recovered_projects:
        root_path = str(recovered["rootPath"])
        root_key = canonical_path(root_path)
        expected_id = project_id(root_path)
        existing_id = roots_by_key.get(root_key)
        if existing_id:
            if existing_id not in project_order:
                project_order.append(existing_id)
                repaired_order.append(existing_id)
            if root_key not in legacy_root_keys:
                legacy_roots.append(root_path)
                legacy_root_keys.add(root_key)
                repaired_legacy_roots.append(root_path)
            already_present.append(
                {
                    "id": existing_id,
                    "name": recovered["name"],
                    "rootPath": root_path,
                }
            )
            continue
        if expected_id in local_projects:
            raise ValueError(
                f"Project id collision for {root_path}: {expected_id}"
            )

        created_at = int(recovered["createdAt"])
        local_projects[expected_id] = {
            "id": expected_id,
            "name": str(recovered["name"]),
            "rootPaths": [root_path],
            "createdAt": created_at,
            "updatedAt": created_at,
        }
        roots_by_key[root_key] = expected_id
        if expected_id not in project_order:
            project_order.append(expected_id)
        if root_key not in legacy_root_keys:
            legacy_roots.append(root_path)
            legacy_root_keys.add(root_key)
        added.append(
            {
                "id": expected_id,
                "name": recovered["name"],
                "rootPath": root_path,
            }
        )

    state["local-projects"] = local_projects
    state["project-order"] = project_order
    state["electron-saved-workspace-roots"] = legacy_roots
    return state, {
        "added": added,
        "already_present": already_present,
        "repaired_project_order": repaired_order,
        "removed_duplicate_order_ids": duplicate_order_ids,
        "removed_unknown_order_ids": unknown_order_ids,
        "added_missing_existing_order_ids": missing_existing_order_ids,
        "repaired_legacy_roots": repaired_legacy_roots,
        "local_project_count": len(local_projects),
        "project_order_count": len(project_order),
        "legacy_root_count": len(legacy_roots),
    }


def merge_config_trust(
    config_text: str,
    recovered_projects: list[dict[str, Any]],
    *,
    trust_projects: bool,
) -> tuple[str, dict[str, Any]]:
    config = tomllib.loads(config_text)
    projects = config.get("projects", {})
    if not isinstance(projects, dict):
        raise ValueError("config.toml projects is not a table")

    existing = {canonical_path(path) for path in projects}
    additions: list[str] = []
    if trust_projects:
        for recovered in recovered_projects:
            root_path = str(recovered["rootPath"])
            if canonical_path(root_path) not in existing:
                additions.append(root_path)
                existing.add(canonical_path(root_path))

    rendered = config_text
    if additions:
        rendered = config_text.rstrip() + "\n"
        for root_path in additions:
            rendered += (
                f"\n[projects.{toml_literal_key(root_path)}]\n"
                'trust_level = "trusted"\n'
            )
        tomllib.loads(rendered)

    reloaded = tomllib.loads(rendered)
    reloaded_projects = reloaded.get("projects", {})
    trusted_count = (
        len(reloaded_projects) if isinstance(reloaded_projects, dict) else 0
    )
    return rendered, {
        "added_trust_roots": additions,
        "trust_projects_requested": trust_projects,
        "trusted_project_count": trusted_count,
    }


def timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")


def create_preimage(
    state_path: Path,
    config_path: Path,
    backup_root: Path,
) -> Path:
    destination = backup_root / timestamp()
    destination.mkdir(parents=True)
    for source in (state_path, config_path):
        target = destination / source.name
        target.write_bytes(source.read_bytes())
    return destination


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-codex-stopped", action="store_true")
    parser.add_argument("--trust-projects", action="store_true")
    parser.add_argument("--allow-broad-roots", action="store_true")
    parser.add_argument("--backup-root", type=Path)
    args = parser.parse_args()

    if args.apply and not args.confirm_codex_stopped:
        raise ValueError(
            "--apply requires --confirm-codex-stopped to avoid a lost update"
        )
    recovered_projects = require_project_manifest(
        args.manifest,
        allow_broad_roots=args.allow_broad_roots,
    )
    state = load_json_object(args.state)
    merged_state, state_result = merge_state(state, recovered_projects)
    rendered_state = json.dumps(
        merged_state,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8") + b"\n"
    json.loads(rendered_state.decode("utf-8"))

    config_raw = args.config.read_bytes()
    if not config_raw:
        raise ValueError(f"{args.config} is empty")
    if b"\x00" in config_raw:
        raise ValueError(f"{args.config} contains NUL bytes")
    config_text = config_raw.decode("utf-8-sig")
    rendered_config, config_result = merge_config_trust(
        config_text,
        recovered_projects,
        trust_projects=args.trust_projects,
    )
    tomllib.loads(rendered_config)

    backup_path: Path | None = None
    if args.apply:
        backup_root = (
            args.backup_root
            if args.backup_root
            else args.state.parent
            / "backups_state"
            / "project-merge"
        )
        backup_path = create_preimage(
            args.state,
            args.config,
            backup_root,
        )
        try:
            atomic_write_bytes(args.state, rendered_state)
            atomic_write_bytes(
                args.config,
                rendered_config.encode("utf-8"),
            )
            reloaded_state = load_json_object(args.state)
            if len(reloaded_state.get("local-projects", {})) != state_result[
                "local_project_count"
            ]:
                raise ValueError("Global state verification count mismatch")
            reloaded_projects = reloaded_state.get("local-projects", {})
            reloaded_order = reloaded_state.get("project-order", [])
            if (
                not isinstance(reloaded_projects, dict)
                or not isinstance(reloaded_order, list)
                or len(reloaded_order) != len(set(reloaded_order))
                or set(reloaded_order) != set(reloaded_projects)
            ):
                raise ValueError(
                    "Global state verification found project-order drift"
                )
            tomllib.loads(args.config.read_text(encoding="utf-8-sig"))
        except Exception:
            atomic_write_bytes(
                args.state,
                (backup_path / args.state.name).read_bytes(),
            )
            atomic_write_bytes(
                args.config,
                (backup_path / args.config.name).read_bytes(),
            )
            raise
    report = {
        "mode": "apply" if args.apply else "dry-run",
        "state": str(args.state),
        "config": str(args.config),
        "manifest": str(args.manifest),
        "backup": str(backup_path) if backup_path else None,
        "state_result": state_result,
        "config_result": config_result,
    }
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(
            args.output,
            b"\xef\xbb\xbf" + rendered.encode("utf-8"),
        )
    print(rendered, end="")


if __name__ == "__main__":
    main()
