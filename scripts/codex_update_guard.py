from __future__ import annotations

import argparse
import ctypes
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import time
import tomllib
from typing import Any


SCHEMA_VERSION = 2
HEALTHY_RETENTION = 14
EVIDENCE_RETENTION = 10
REPORT_RETENTION = 60
DAILY_SNAPSHOT_SECONDS = 24 * 60 * 60
EVIDENCE_REPEAT_SECONDS = 24 * 60 * 60
LOCK_STALE_SECONDS = 15 * 60
THREAD_ERROR_RATIO = 0.95


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat().replace("+00:00", "Z")


def stamp_now() -> str:
    return utc_now().strftime("%Y%m%d-%H%M%S-%f")


def canonical_path(value: str) -> str:
    return os.path.normcase(os.path.normpath(value))


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.guard-",
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


def atomic_write_json(path: Path, value: Any) -> None:
    rendered = json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")
    # Windows PowerShell 5 treats BOM-less UTF-8 as the active ANSI code page.
    # These reports contain Chinese project paths, so include a BOM to keep
    # Get-Content | ConvertFrom-Json reliable on the bundled Windows shell.
    atomic_write_bytes(path, b"\xef\xbb\xbf" + rendered + b"\n")


def load_json_object(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    if b"\x00" in raw:
        raise ValueError(f"{path.name} contains NUL bytes")
    value = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} is not a JSON object")
    return value


def load_optional_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    return load_json_object(path)


def check_record(
    name: str,
    status: str,
    detail: Any,
    *,
    critical: bool,
) -> dict[str, Any]:
    return {
        "name": name,
        "status": status,
        "critical": critical,
        "detail": detail,
    }


def read_config(config_path: Path) -> tuple[dict[str, Any], bytes]:
    raw = config_path.read_bytes()
    if not raw:
        raise ValueError("config.toml is empty")
    if b"\x00" in raw:
        raise ValueError("config.toml contains NUL bytes")
    text = raw.decode("utf-8-sig")
    return tomllib.loads(text), raw


def extract_local_projects(
    state: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    projects_raw = state.get("local-projects")
    if not isinstance(projects_raw, dict):
        raise ValueError("local-projects is not an object")
    projects: dict[str, dict[str, Any]] = {}
    roots: list[str] = []
    for project_id, value in projects_raw.items():
        if not isinstance(project_id, str) or not isinstance(value, dict):
            raise ValueError("local-projects contains an invalid entry")
        root_paths = value.get("rootPaths")
        if not isinstance(root_paths, list) or not all(
            isinstance(root, str) for root in root_paths
        ):
            raise ValueError(f"{project_id}.rootPaths is invalid")
        projects[project_id] = value
        roots.extend(root_paths)
    return projects, roots


def durable_config_view(config: dict[str, Any]) -> dict[str, Any]:
    def dictionary(value: Any) -> dict[str, Any]:
        return value if isinstance(value, dict) else {}

    projects = dictionary(config.get("projects"))
    project_view = {
        path: dictionary(value).get("trust_level")
        for path, value in sorted(projects.items())
    }
    plugins = dictionary(config.get("plugins"))
    plugin_view = {
        plugin: dictionary(value).get("enabled")
        for plugin, value in sorted(plugins.items())
    }
    marketplaces = dictionary(config.get("marketplaces"))
    marketplace_view = {
        name: {
            "source_type": dictionary(value).get("source_type"),
        }
        for name, value in sorted(marketplaces.items())
    }
    feature_names = ("computer_use", "multi_agent", "memories", "hooks")
    features = dictionary(config.get("features"))
    desktop = dictionary(config.get("desktop"))
    return {
        "model_reasoning_effort": config.get("model_reasoning_effort"),
        "personality": config.get("personality"),
        "sandbox_mode": config.get("sandbox_mode"),
        "approval_policy": config.get("approval_policy"),
        "approvals_reviewer": config.get("approvals_reviewer"),
        "windows_sandbox": dictionary(config.get("windows")).get("sandbox"),
        "features": {name: features.get(name) for name in feature_names},
        "projects": project_view,
        "plugins": plugin_view,
        "marketplaces": marketplace_view,
        "sandbox_workspace_write": dictionary(
            config.get("sandbox_workspace_write")
        ),
        "desktop": {
            key: value
            for key, value in desktop.items()
            if key not in {"lastSeenVersion", "last_seen_version"}
        },
        "memories": dictionary(config.get("memories")),
    }


def runtime_path_checks(config: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    notify = config.get("notify")
    if (
        isinstance(notify, list)
        and notify
        and isinstance(notify[0], str)
    ):
        result["notify"] = {
            "path": notify[0],
            "exists": Path(notify[0]).is_file(),
        }
    mcp_servers = config.get("mcp_servers")
    if isinstance(mcp_servers, dict):
        node_repl = mcp_servers.get("node_repl")
        if isinstance(node_repl, dict):
            command = node_repl.get("command")
            if isinstance(command, str):
                result["node_repl"] = {
                    "path": command,
                    "exists": Path(command).is_file(),
                }
            environment = node_repl.get("env")
            if isinstance(environment, dict):
                for key in ("NODE_REPL_NODE_PATH", "CODEX_CLI_PATH"):
                    value = environment.get(key)
                    if isinstance(value, str):
                        result[key] = {
                            "path": value,
                            "exists": Path(value).is_file(),
                        }
    return result


def database_health(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {
            "exists": False,
            "thread_count": None,
            "quick_check": None,
            "error": None,
        }
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(
            f"file:{path.as_posix()}?mode=ro",
            uri=True,
            timeout=10,
        )
        connection.execute("PRAGMA busy_timeout=10000")
        quick_row = connection.execute("PRAGMA quick_check").fetchone()
        quick_check = str(quick_row[0]) if quick_row else None
        count_row = connection.execute(
            "SELECT COUNT(*) FROM threads"
        ).fetchone()
        thread_count = int(count_row[0]) if count_row else None
        return {
            "exists": True,
            "thread_count": thread_count,
            "quick_check": quick_check,
            "error": None,
        }
    except (OSError, sqlite3.Error) as error:
        return {
            "exists": True,
            "thread_count": None,
            "quick_check": None,
            "error": f"{type(error).__name__}: {error}",
        }
    finally:
        if connection is not None:
            connection.close()


def process_manager_health(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {
            "exists": False,
            "valid": True,
            "record_count": 0,
            "size": None,
            "sha256": None,
            "error": None,
        }
    try:
        raw = path.read_bytes()
        if not raw:
            raise ValueError("chat_processes.json is empty")
        if b"\x00" in raw:
            raise ValueError("chat_processes.json contains NUL bytes")
        value = json.loads(raw.decode("utf-8-sig"))
        if not isinstance(value, list):
            raise ValueError("chat_processes.json is not an array")
        if not all(isinstance(item, dict) for item in value):
            raise ValueError(
                "chat_processes.json contains a non-object record"
            )
        return {
            "exists": True,
            "valid": True,
            "record_count": len(value),
            "size": len(raw),
            "sha256": sha256_bytes(raw),
            "error": None,
        }
    except (
        OSError,
        UnicodeError,
        ValueError,
        json.JSONDecodeError,
    ) as error:
        try:
            size = path.stat().st_size
            digest = sha256_file(path)
        except OSError:
            size = None
            digest = None
        return {
            "exists": True,
            "valid": False,
            "record_count": None,
            "size": size,
            "sha256": digest,
            "error": f"{type(error).__name__}: {error}",
        }


def valid_plugin_manifest(plugin_version_root: Path) -> bool:
    manifest = plugin_version_root / ".codex-plugin" / "plugin.json"
    if not manifest.is_file():
        return False
    try:
        load_json_object(manifest)
        return True
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        return False


def is_path_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except (OSError, ValueError):
        return False


def plugin_cache_detail(plugin_root: Path) -> dict[str, Any]:
    versions: list[str] = []
    if plugin_root.is_dir():
        try:
            versions = sorted(
                child.name
                for child in plugin_root.iterdir()
                if child.is_dir()
                and child.name != "latest"
                and valid_plugin_manifest(child)
            )
        except OSError:
            versions = []

    latest = plugin_root / "latest"
    latest_manifest_valid = valid_plugin_manifest(latest)
    latest_target: str | None = None
    latest_target_stable = False
    if latest_manifest_valid:
        try:
            resolved = latest.resolve(strict=True)
            latest_target = str(resolved)
            latest_target_stable = is_path_within(resolved, plugin_root)
        except OSError:
            latest_target_stable = False
    return {
        "available": latest_manifest_valid or bool(versions),
        "latest_manifest_valid": latest_manifest_valid,
        "latest_target": latest_target,
        "latest_target_stable": latest_target_stable,
        "valid_versions": versions,
    }


def plugin_cache_health(codex_home: Path) -> dict[str, dict[str, Any]]:
    cache_root = codex_home / "plugins" / "cache" / "openai-bundled"
    return {
        plugin: plugin_cache_detail(cache_root / plugin)
        for plugin in ("sites", "browser", "chrome", "computer-use")
    }


def expected_state_from_current(
    state: dict[str, Any],
    database: dict[str, dict[str, Any]],
    config: dict[str, Any],
    package_version: str | None,
    plugin_cache: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    projects, roots = extract_local_projects(state)
    baseline_threads: dict[str, int] = {}
    minimum_threads: dict[str, int] = {}
    for name, metrics in database.items():
        count = metrics.get("thread_count")
        if isinstance(count, int):
            baseline_threads[name] = count
            minimum_threads[name] = (
                count
                if count < 20
                else max(1, math.ceil(count * THREAD_ERROR_RATIO))
            )
    plugins = config.get("plugins")
    if not isinstance(plugins, dict):
        plugins = {}
    required_plugins = sorted(
        name
        for name, value in plugins.items()
        if isinstance(value, dict) and value.get("enabled") is True
    )
    required_plugin_cache_names = {
        name.split("@", 1)[0] for name in required_plugins
    }
    features = config.get("features")
    if not isinstance(features, dict):
        features = {}
    required_features = sorted(
        name for name, value in features.items() if value is True
    )
    required_plugin_caches = sorted(
        name
        for name, detail in plugin_cache.items()
        if (
            detail.get("available")
            and detail.get("latest_manifest_valid")
            and detail.get("latest_target_stable")
        )
        or name in required_plugin_cache_names
    )
    return {
        "schemaVersion": SCHEMA_VERSION,
        "refreshedAt": iso_now(),
        "packageVersionAtRefresh": package_version,
        "minimumProjectCount": len(projects),
        "requiredProjectIds": sorted(projects),
        "requiredProjectRoots": roots,
        "baselineThreadCounts": baseline_threads,
        "minimumThreadCounts": minimum_threads,
        "requiredPluginCaches": required_plugin_caches,
        "requiredEnabledPlugins": required_plugins,
        "requiredFeatureFlags": required_features,
        "requiredRuntimePaths": sorted(runtime_path_checks(config)),
        "expectedWindowsSandbox": (
            config.get("windows", {}).get("sandbox")
            if isinstance(config.get("windows"), dict)
            else None
        ),
    }


def overall_status(checks: list[dict[str, Any]]) -> str:
    if any(
        item["status"] == "error" and item["critical"] for item in checks
    ):
        return "critical"
    if any(item["status"] != "ok" for item in checks):
        return "degraded"
    return "healthy"


def validate_baseline_candidate(
    codex_home: Path,
) -> tuple[
    dict[str, Any],
    dict[str, Any],
    dict[str, dict[str, Any]],
]:
    config, _ = read_config(codex_home / "config.toml")
    state = load_json_object(codex_home / ".codex-global-state.json")
    projects, roots = extract_local_projects(state)
    if not projects:
        raise ValueError("refusing to baseline an empty project set")
    missing_directories = [root for root in roots if not Path(root).is_dir()]
    if missing_directories:
        raise ValueError(
            "refusing to baseline missing project directories: "
            + "; ".join(missing_directories)
        )
    project_order = state.get("project-order")
    if not isinstance(project_order, list) or not all(
        isinstance(item, str) for item in project_order
    ):
        raise ValueError("refusing to baseline an invalid project-order")
    if (
        len(project_order) != len(set(project_order))
        or set(project_order) != set(projects)
    ):
        raise ValueError(
            "refusing to baseline inconsistent local-projects/project-order"
        )
    database = {
        "legacy": database_health(codex_home / "state_5.sqlite"),
        "app": database_health(codex_home / "sqlite" / "state_5.sqlite"),
    }
    for name, metrics in database.items():
        if (
            not metrics.get("exists")
            or metrics.get("quick_check") != "ok"
            or not isinstance(metrics.get("thread_count"), int)
        ):
            raise ValueError(
                f"refusing to baseline unhealthy {name} database: "
                f"{metrics}"
            )
    process_manager = process_manager_health(
        codex_home / "process_manager" / "chat_processes.json"
    )
    if not process_manager.get("valid"):
        raise ValueError(
            "refusing to baseline an unhealthy process manager registry: "
            f"{process_manager}"
        )
    cache = plugin_cache_health(codex_home)
    configured_plugins = config.get("plugins")
    if not isinstance(configured_plugins, dict):
        configured_plugins = {}
    enabled_cache_names = {
        name.split("@", 1)[0]
        for name, value in configured_plugins.items()
        if (
            name.split("@", 1)[0] in cache
            and isinstance(value, dict)
            and value.get("enabled") is True
        )
    }
    invalid_cache = [
        name
        for name, detail in cache.items()
        if (
            detail.get("available")
            or name in enabled_cache_names
        )
        and (
            not detail.get("latest_manifest_valid")
            or not detail.get("latest_target_stable")
        )
    ]
    if invalid_cache:
        raise ValueError(
            "refusing to baseline invalid plugin cache: "
            + ", ".join(invalid_cache)
        )
    return config, state, database


def evaluate_health(
    codex_home: Path,
    expected: dict[str, Any],
    last_state: dict[str, Any],
    package_version: str | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    config_path = codex_home / "config.toml"
    global_state_path = codex_home / ".codex-global-state.json"

    config: dict[str, Any] | None = None
    config_raw: bytes | None = None
    try:
        config, config_raw = read_config(config_path)
        checks.append(
            check_record(
                "config",
                "ok",
                {
                    "path": str(config_path),
                    "size": len(config_raw),
                    "sha256": sha256_bytes(config_raw),
                },
                critical=True,
            )
        )
    except (OSError, UnicodeError, ValueError, tomllib.TOMLDecodeError) as error:
        checks.append(
            check_record(
                "config",
                "error",
                str(error),
                critical=True,
            )
        )

    state: dict[str, Any] | None = None
    projects: dict[str, dict[str, Any]] = {}
    project_roots: list[str] = []
    try:
        state = load_json_object(global_state_path)
        projects, project_roots = extract_local_projects(state)
        checks.append(
            check_record(
                "global_state",
                "ok",
                {
                    "path": str(global_state_path),
                    "size": global_state_path.stat().st_size,
                    "sha256": sha256_file(global_state_path),
                },
                critical=True,
            )
        )
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as error:
        checks.append(
            check_record(
                "global_state",
                "error",
                str(error),
                critical=True,
            )
        )

    minimum_project_count = expected.get("minimumProjectCount", 0)
    required_project_ids = expected.get("requiredProjectIds", [])
    required_roots = expected.get("requiredProjectRoots", [])
    if not isinstance(minimum_project_count, int):
        minimum_project_count = 0
    if not isinstance(required_project_ids, list):
        required_project_ids = []
    if not isinstance(required_roots, list):
        required_roots = []
    current_project_ids = set(projects)
    missing_required_project_ids = [
        project_id
        for project_id in required_project_ids
        if isinstance(project_id, str)
        and project_id not in current_project_ids
    ]
    current_root_keys = {
        canonical_path(root) for root in project_roots if isinstance(root, str)
    }
    missing_required_roots = [
        root
        for root in required_roots
        if isinstance(root, str)
        and canonical_path(root) not in current_root_keys
    ]
    missing_directories = [
        root for root in project_roots if not Path(root).is_dir()
    ]
    project_order_raw = state.get("project-order") if state else None
    project_order_valid = isinstance(project_order_raw, list) and all(
        isinstance(item, str) for item in project_order_raw
    )
    project_order = project_order_raw if project_order_valid else []
    duplicate_order_ids = sorted(
        {
            project_id
            for project_id in project_order
            if project_order.count(project_id) > 1
        }
    )
    missing_from_order = sorted(current_project_ids - set(project_order))
    unknown_order_ids = sorted(set(project_order) - current_project_ids)
    empty_root_project_ids = sorted(
        project_id
        for project_id, project in projects.items()
        if not project.get("rootPaths")
    )
    project_status = "ok"
    project_critical = False
    if not state:
        project_status = "error"
        project_critical = True
    elif len(projects) == 0 or (
        minimum_project_count > 0
        and len(projects) < max(1, int(minimum_project_count * 0.5))
    ):
        project_status = "error"
        project_critical = True
    elif (
        len(projects) < minimum_project_count
        or missing_required_project_ids
        or missing_required_roots
        or missing_directories
        or not project_order_valid
        or duplicate_order_ids
        or missing_from_order
        or unknown_order_ids
        or empty_root_project_ids
    ):
        project_status = "warning"
    checks.append(
        check_record(
            "projects",
            project_status,
            {
                "count": len(projects),
                "minimum_count": minimum_project_count,
                "missing_required_project_ids": missing_required_project_ids,
                "roots": project_roots,
                "missing_required_roots": missing_required_roots,
                "missing_directories": missing_directories,
                "project_order_valid": project_order_valid,
                "project_order_count": len(project_order),
                "duplicate_order_ids": duplicate_order_ids,
                "missing_from_order": missing_from_order,
                "unknown_order_ids": unknown_order_ids,
                "empty_root_project_ids": empty_root_project_ids,
            },
            critical=project_critical,
        )
    )

    process_manager = process_manager_health(
        codex_home / "process_manager" / "chat_processes.json"
    )
    checks.append(
        check_record(
            "process_manager",
            "ok" if process_manager.get("valid") else "warning",
            process_manager,
            critical=False,
        )
    )

    database = {
        "legacy": database_health(codex_home / "state_5.sqlite"),
        "app": database_health(codex_home / "sqlite" / "state_5.sqlite"),
    }
    expected_thread_counts = expected.get("minimumThreadCounts", {})
    if not isinstance(expected_thread_counts, dict):
        expected_thread_counts = {}
    baseline_thread_counts = expected.get("baselineThreadCounts", {})
    if not isinstance(baseline_thread_counts, dict):
        baseline_thread_counts = {}
    last_healthy_metrics = last_state.get("lastHealthyMetrics", {})
    if not isinstance(last_healthy_metrics, dict):
        last_healthy_metrics = {}
    for name, metrics in database.items():
        status = "ok"
        critical = False
        count = metrics.get("thread_count")
        minimum = expected_thread_counts.get(name)
        baseline = baseline_thread_counts.get(name)
        previous = last_healthy_metrics.get(name)
        if (
            not metrics.get("exists")
            or metrics.get("quick_check") != "ok"
            or not isinstance(count, int)
        ):
            status = "error"
            critical = True
        elif isinstance(minimum, int) and count < minimum:
            status = "error"
            critical = True
        elif isinstance(baseline, int) and count < baseline:
            status = "warning"
        elif (
            isinstance(previous, int)
            and count < previous
        ):
            status = "warning"
        checks.append(
            check_record(
                f"database_{name}",
                status,
                dict(
                    metrics,
                    baseline_thread_count=baseline,
                    minimum_thread_count=minimum,
                    previous_healthy_thread_count=previous,
                ),
                critical=critical,
            )
        )

    plugin_cache = plugin_cache_health(codex_home)
    required_plugins = expected.get("requiredPluginCaches", [])
    if not isinstance(required_plugins, list):
        required_plugins = []
    missing_plugins = [
        name
        for name in required_plugins
        if not plugin_cache.get(str(name), {}).get("available")
    ]
    unstable_latest_plugins = [
        name
        for name in required_plugins
        if plugin_cache.get(str(name), {}).get("available")
        and (
            not plugin_cache.get(str(name), {}).get(
                "latest_manifest_valid"
            )
            or not plugin_cache.get(str(name), {}).get(
                "latest_target_stable"
            )
        )
    ]
    checks.append(
        check_record(
            "plugin_cache",
            (
                "warning"
                if missing_plugins or unstable_latest_plugins
                else "ok"
            ),
            {
                "plugins": plugin_cache,
                "missing_required": missing_plugins,
                "unstable_latest": unstable_latest_plugins,
            },
            critical=False,
        )
    )

    runtime_paths = runtime_path_checks(config) if config else {}
    required_runtime_paths = expected.get("requiredRuntimePaths", [])
    if not isinstance(required_runtime_paths, list):
        required_runtime_paths = []
    missing_runtime_entries = [
        name
        for name in required_runtime_paths
        if isinstance(name, str) and name not in runtime_paths
    ]
    missing_runtime_paths = [
        name
        for name, value in runtime_paths.items()
        if isinstance(value, dict) and not value.get("exists")
    ]
    checks.append(
        check_record(
            "runtime_paths",
            (
                "warning"
                if missing_runtime_paths or missing_runtime_entries
                else "ok"
            ),
            {
                "paths": runtime_paths,
                "missing": missing_runtime_paths,
                "missing_entries": missing_runtime_entries,
            },
            critical=False,
        )
    )

    config_invariant_detail: dict[str, Any] = {}
    config_invariant_status = "ok"
    if config is not None:
        required_enabled_plugins = expected.get(
            "requiredEnabledPlugins", []
        )
        if not isinstance(required_enabled_plugins, list):
            required_enabled_plugins = []
        configured_plugins = config.get("plugins")
        if not isinstance(configured_plugins, dict):
            configured_plugins = {}
        disabled_plugins = [
            name
            for name in required_enabled_plugins
            if not (
                isinstance(configured_plugins.get(name), dict)
                and configured_plugins[name].get("enabled") is True
            )
        ]
        required_features = expected.get("requiredFeatureFlags", [])
        if not isinstance(required_features, list):
            required_features = []
        configured_features = config.get("features")
        if not isinstance(configured_features, dict):
            configured_features = {}
        disabled_features = [
            name
            for name in required_features
            if configured_features.get(name) is not True
        ]
        expected_windows_sandbox = expected.get("expectedWindowsSandbox")
        windows = config.get("windows")
        if not isinstance(windows, dict):
            windows = {}
        actual_windows_sandbox = windows.get("sandbox")
        sandbox_changed = (
            expected_windows_sandbox is not None
            and actual_windows_sandbox != expected_windows_sandbox
        )
        config_invariant_detail = {
            "disabled_required_plugins": disabled_plugins,
            "disabled_required_features": disabled_features,
            "expected_windows_sandbox": expected_windows_sandbox,
            "actual_windows_sandbox": actual_windows_sandbox,
            "windows_sandbox_changed": sandbox_changed,
        }
        if disabled_plugins or disabled_features or sandbox_changed:
            config_invariant_status = "warning"
    checks.append(
        check_record(
            "config_invariants",
            config_invariant_status,
            config_invariant_detail,
            critical=False,
        )
    )

    if not package_version:
        checks.append(
            check_record(
                "package_version",
                "warning",
                "OpenAI.Codex package version was not detected",
                critical=False,
            )
        )
    else:
        checks.append(
            check_record(
                "package_version",
                "ok",
                package_version,
                critical=False,
            )
        )

    status = overall_status(checks)

    semantic = {
        "packageVersion": package_version,
        "projects": [
            {
                "id": project_id,
                "name": project.get("name"),
                "rootPaths": project.get("rootPaths"),
            }
            for project_id, project in sorted(projects.items())
        ],
        "projectOrder": (
            state.get("project-order") if isinstance(state, dict) else None
        ),
        "config": durable_config_view(config) if config else None,
    }
    semantic_hash = sha256_bytes(
        json.dumps(
            semantic,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    report = {
        "schemaVersion": SCHEMA_VERSION,
        "checkedAt": iso_now(),
        "status": status,
        "packageVersion": package_version,
        "semanticHash": semantic_hash,
        "checks": checks,
    }
    context = {
        "config": config,
        "state": state,
        "database": database,
        "project_roots": project_roots,
        "semantic_hash": semantic_hash,
    }
    return report, context


def copy_file_with_manifest(
    source: Path,
    destination: Path,
    manifest: list[dict[str, Any]],
) -> None:
    if not source.is_file():
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    manifest.append(
        {
            "source": str(source),
            "snapshot": str(destination),
            "size": destination.stat().st_size,
            "sha256": sha256_file(destination),
        }
    )


def backup_sqlite(
    source: Path,
    destination: Path,
    manifest: list[dict[str, Any]],
) -> None:
    if not source.is_file():
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_connection = sqlite3.connect(
        f"file:{source.as_posix()}?mode=ro",
        uri=True,
        timeout=15,
    )
    destination_connection = sqlite3.connect(destination)
    try:
        source_connection.execute("PRAGMA busy_timeout=15000")
        source_connection.backup(destination_connection)
    finally:
        destination_connection.close()
        source_connection.close()
    manifest.append(
        {
            "source": str(source),
            "snapshot": str(destination),
            "size": destination.stat().st_size,
            "sha256": sha256_file(destination),
            "sqlite_quick_check": database_health(destination).get(
                "quick_check"
            ),
        }
    )


def create_snapshot(
    codex_home: Path,
    snapshot_root: Path,
    report: dict[str, Any],
    reason: str,
    *,
    include_databases: bool,
) -> Path:
    destination = snapshot_root / stamp_now()
    suffix = 0
    while destination.exists():
        suffix += 1
        destination = snapshot_root / f"{stamp_now()}-{suffix}"
    temporary = snapshot_root / (
        f".{destination.name}.partial-{os.getpid()}"
    )
    snapshot_root.mkdir(parents=True, exist_ok=True)
    if temporary.exists():
        shutil.rmtree(temporary)
    temporary.mkdir()
    files: list[dict[str, Any]] = []
    try:
        for relative in (
            "config.toml",
            ".codex-global-state.json",
            ".codex-global-state.json.bak",
            "session_index.jsonl",
        ):
            copy_file_with_manifest(
                codex_home / relative,
                temporary / relative,
                files,
            )
        if include_databases:
            backup_sqlite(
                codex_home / "state_5.sqlite",
                temporary / "state_5.sqlite",
                files,
            )
            backup_sqlite(
                codex_home / "sqlite" / "state_5.sqlite",
                temporary / "sqlite" / "state_5.sqlite",
                files,
            )
        for item in files:
            snapshot_path = Path(item["snapshot"])
            relative = snapshot_path.relative_to(temporary)
            item["relative"] = relative.as_posix()
            item["snapshot"] = str(destination / relative)
        manifest = {
            "schemaVersion": SCHEMA_VERSION,
            "createdAt": iso_now(),
            "reason": reason,
            "healthStatus": report["status"],
            "packageVersion": report.get("packageVersion"),
            "semanticHash": report.get("semanticHash"),
            "files": files,
        }
        atomic_write_json(temporary / "manifest.json", manifest)
        atomic_write_json(temporary / "health-report.json", report)
        os.replace(temporary, destination)
        return destination
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def trim_directories(root: Path, keep: int) -> None:
    if not root.is_dir():
        return
    directories = sorted(
        (item for item in root.iterdir() if item.is_dir()),
        key=lambda item: item.name,
        reverse=True,
    )
    for stale in directories[keep:]:
        shutil.rmtree(stale)


def trim_files(root: Path, keep: int) -> None:
    if not root.is_dir():
        return
    files = sorted(
        (item for item in root.glob("*.json") if item.name != "latest.json"),
        key=lambda item: item.name,
        reverse=True,
    )
    for stale in files[keep:]:
        stale.unlink(missing_ok=True)


def newest_healthy_snapshot(
    healthy_root: Path,
) -> tuple[Path | None, str | None]:
    if not healthy_root.is_dir():
        return None, None
    candidates = sorted(
        (item for item in healthy_root.iterdir() if item.is_dir()),
        key=lambda item: item.name,
        reverse=True,
    )
    for candidate in candidates:
        try:
            manifest = load_json_object(candidate / "manifest.json")
        except (
            OSError,
            UnicodeError,
            ValueError,
            json.JSONDecodeError,
        ):
            continue
        if manifest.get("healthStatus") == "healthy":
            created_at = manifest.get("createdAt")
            return (
                candidate,
                created_at if isinstance(created_at, str) else None,
            )
    return None, None


def parse_timestamp(value: Any) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def process_exists(process_id: int) -> bool:
    if process_id <= 0:
        return False
    if os.name == "nt":
        process_query_limited_information = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            process_query_limited_information,
            False,
            process_id,
        )
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(process_id, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def acquire_lock(lock_path: Path) -> int:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(
            lock_path,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
        )
    except FileExistsError:
        age = time.time() - lock_path.stat().st_mtime
        if age <= LOCK_STALE_SECONDS:
            raise RuntimeError("Another Codex update guard run is active")
        active_pid: int | None = None
        try:
            active_pid = int(
                lock_path.read_text(encoding="ascii").strip()
            )
        except (OSError, UnicodeError, ValueError):
            active_pid = None
        if active_pid is not None and process_exists(active_pid):
            raise RuntimeError(
                "Codex update guard lock is old but its process "
                f"is still active: pid={active_pid}"
            )
        lock_path.unlink()
        descriptor = os.open(
            lock_path,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
        )
    os.write(descriptor, f"{os.getpid()}\n".encode("ascii"))
    os.fsync(descriptor)
    return descriptor


def write_terminal_report(
    reports_root: Path,
    package_version: str | None,
    name: str,
    detail: str,
) -> dict[str, Any]:
    report = {
        "schemaVersion": SCHEMA_VERSION,
        "checkedAt": iso_now(),
        "status": "critical",
        "packageVersion": package_version,
        "semanticHash": None,
        "checks": [
            check_record(
                name,
                "error",
                detail,
                critical=True,
            )
        ],
        "baselineRefreshed": False,
    }
    reports_root.mkdir(parents=True, exist_ok=True)
    report_path = reports_root / f"{stamp_now()}.json"
    atomic_write_json(report_path, report)
    atomic_write_json(reports_root / "latest.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--codex-home", type=Path, required=True)
    parser.add_argument("--guard-home", type=Path, required=True)
    parser.add_argument("--package-version")
    parser.add_argument("--force-snapshot", action="store_true")
    parser.add_argument("--refresh-baseline", action="store_true")
    args = parser.parse_args()

    codex_home = args.codex_home.resolve()
    guard_home = args.guard_home.resolve()
    backup_root = codex_home / "backups_state" / "update-guard"
    healthy_root = backup_root / "healthy"
    evidence_root = backup_root / "evidence"
    reports_root = guard_home / "reports"
    expected_path = guard_home / "expected-state.json"
    state_path = guard_home / "guard-state.json"
    lock_path = guard_home / ".guard.lock"

    lock_descriptor = acquire_lock(lock_path)
    try:
        guard_state_error: str | None = None
        try:
            last_state = load_optional_json(state_path)
        except (
            OSError,
            UnicodeError,
            ValueError,
            json.JSONDecodeError,
        ) as error:
            last_state = {}
            guard_state_error = (
                f"guard-state.json could not be read: {error}"
            )

        try:
            expected = load_optional_json(expected_path)
        except (
            OSError,
            UnicodeError,
            ValueError,
            json.JSONDecodeError,
        ) as error:
            write_terminal_report(
                reports_root,
                args.package_version,
                "baseline",
                f"expected-state.json could not be read: {error}",
            )
            return 2

        if not expected and not args.refresh_baseline:
            write_terminal_report(
                reports_root,
                args.package_version,
                "baseline",
                "expected-state.json is missing; refusing to adopt the "
                "current state automatically. Validate the machine, then "
                "rerun with --refresh-baseline.",
            )
            return 2

        bootstrap_expected = args.refresh_baseline
        if bootstrap_expected:
            try:
                baseline_config, current_state, database_before = (
                    validate_baseline_candidate(codex_home)
                )
                expected = expected_state_from_current(
                    current_state,
                    database_before,
                    baseline_config,
                    args.package_version,
                    plugin_cache_health(codex_home),
                )
                atomic_write_json(expected_path, expected)
            except (
                OSError,
                UnicodeError,
                ValueError,
                json.JSONDecodeError,
                tomllib.TOMLDecodeError,
            ) as error:
                write_terminal_report(
                    reports_root,
                    args.package_version,
                    "baseline",
                    f"baseline refresh rejected: {error}",
                )
                return 2

        report, context = evaluate_health(
            codex_home,
            expected,
            last_state,
            args.package_version,
        )
        report["baselineRefreshed"] = bootstrap_expected
        if expected.get("schemaVersion") != SCHEMA_VERSION:
            report["checks"].append(
                check_record(
                    "baseline_schema",
                    "warning",
                    {
                        "expected": SCHEMA_VERSION,
                        "actual": expected.get("schemaVersion"),
                        "action": (
                            "review current state and refresh the baseline"
                        ),
                    },
                    critical=False,
                )
            )
        if guard_state_error:
            report["checks"].append(
                check_record(
                    "guard_state",
                    "warning",
                    guard_state_error,
                    critical=False,
                )
            )
        report["status"] = overall_status(report["checks"])

        last_snapshot_at = parse_timestamp(last_state.get("lastSnapshotAt"))
        last_evidence_at = parse_timestamp(last_state.get("lastEvidenceAt"))
        previous_semantic_hash = last_state.get("lastSemanticHash")
        previous_version = last_state.get("lastPackageVersion")
        now_seconds = time.time()

        snapshot_path: Path | None = None
        snapshot_reason: str | None = None
        if report["status"] == "healthy":
            if args.force_snapshot:
                snapshot_reason = "forced"
            elif previous_version != args.package_version:
                snapshot_reason = "package_version_changed"
            elif previous_semantic_hash != report["semanticHash"]:
                snapshot_reason = "durable_state_changed"
            elif (
                last_snapshot_at is None
                or now_seconds - last_snapshot_at >= DAILY_SNAPSHOT_SECONDS
            ):
                snapshot_reason = "daily_last_known_good"
            if snapshot_reason:
                snapshot_path = create_snapshot(
                    codex_home,
                    healthy_root,
                    report,
                    snapshot_reason,
                    include_databases=True,
                )
                report["healthySnapshot"] = str(snapshot_path)
                report["snapshotReason"] = snapshot_reason
        else:
            health_fingerprint = sha256_bytes(
                json.dumps(
                    [
                        {
                            "name": item["name"],
                            "status": item["status"],
                            "detail": item["detail"],
                        }
                        for item in report["checks"]
                        if item["status"] != "ok"
                    ],
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
            if (
                health_fingerprint != last_state.get(
                    "lastEvidenceFingerprint"
                )
                or last_evidence_at is None
                or now_seconds - last_evidence_at >= EVIDENCE_REPEAT_SECONDS
            ):
                snapshot_path = create_snapshot(
                    codex_home,
                    evidence_root,
                    report,
                    f"{report['status']}_evidence",
                    include_databases=False,
                )
                report["evidenceSnapshot"] = str(snapshot_path)
                report["evidenceFingerprint"] = health_fingerprint

        reports_root.mkdir(parents=True, exist_ok=True)
        report_path = reports_root / f"{stamp_now()}.json"
        atomic_write_json(report_path, report)
        atomic_write_json(reports_root / "latest.json", report)

        new_state = dict(last_state)
        if guard_state_error:
            recovered_snapshot, recovered_snapshot_at = (
                newest_healthy_snapshot(healthy_root)
            )
            if recovered_snapshot is not None:
                new_state["lastHealthySnapshot"] = str(
                    recovered_snapshot
                )
                if recovered_snapshot_at:
                    new_state["lastSnapshotAt"] = recovered_snapshot_at
        new_state.update(
            {
                "schemaVersion": SCHEMA_VERSION,
                "lastRunAt": report["checkedAt"],
                "lastStatus": report["status"],
                "lastPackageVersion": args.package_version,
                "lastSemanticHash": report["semanticHash"],
                "lastReport": str(report_path),
            }
        )
        if report["status"] == "healthy":
            new_state["lastHealthyAt"] = report["checkedAt"]
            new_state["lastHealthyMetrics"] = {
                name: metrics.get("thread_count")
                for name, metrics in context["database"].items()
            }
            if snapshot_path:
                new_state["lastHealthySnapshot"] = str(snapshot_path)
                new_state["lastSnapshotAt"] = report["checkedAt"]
        elif snapshot_path:
            new_state["lastEvidenceSnapshot"] = str(snapshot_path)
            new_state["lastEvidenceAt"] = report["checkedAt"]
            new_state["lastEvidenceFingerprint"] = report.get(
                "evidenceFingerprint"
            )
        atomic_write_json(state_path, new_state)

        trim_directories(healthy_root, HEALTHY_RETENTION)
        trim_directories(evidence_root, EVIDENCE_RETENTION)
        trim_files(reports_root, REPORT_RETENTION)

        print(json.dumps(report, ensure_ascii=False, indent=2))
        if report["status"] == "healthy":
            return 0
        if report["status"] == "degraded":
            return 1
        return 2
    finally:
        os.close(lock_descriptor)
        lock_path.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
