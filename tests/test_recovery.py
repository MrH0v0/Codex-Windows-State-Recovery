from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_ROOT = SKILL_ROOT / "scripts"
GUARD_SCRIPT = SCRIPT_ROOT / "codex_update_guard.py"
RESTORE_SCRIPT = SCRIPT_ROOT / "Restore-CodexLastHealthy.ps1"
MERGE_SCRIPT = SCRIPT_ROOT / "merge_recovered_projects.py"
AUDIT_SCRIPT = SCRIPT_ROOT / "audit_codex_state.py"
INSTALL_SCRIPT = SCRIPT_ROOT / "Install-CodexRecoveryGuard.ps1"
UNINSTALL_SCRIPT = SCRIPT_ROOT / "Uninstall-CodexRecoveryGuard.ps1"
PROCESS_REGISTRY_SCRIPT = (
    SCRIPT_ROOT / "Repair-CodexChatProcessRegistry.ps1"
)


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def create_database(path: Path, count: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "CREATE TABLE threads (id INTEGER PRIMARY KEY, title TEXT)"
        )
        connection.executemany(
            "INSERT INTO threads(id, title) VALUES(?, ?)",
            ((index, f"thread-{index}") for index in range(1, count + 1)),
        )
        connection.commit()
    finally:
        connection.close()


def set_database_count(path: Path, count: int) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute("DELETE FROM threads WHERE id > ?", (count,))
        connection.commit()
    finally:
        connection.close()


def database_count(path: Path) -> int:
    connection = sqlite3.connect(path)
    try:
        row = connection.execute("SELECT COUNT(*) FROM threads").fetchone()
        return int(row[0])
    finally:
        connection.close()


class RecoveryFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="codex-recovery-test-"
        )
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        self.guard = self.root / "guard"
        self.home.mkdir()
        self.guard.mkdir()
        self.projects = [
            self.root / "projects" / "项目一",
            self.root / "projects" / "project-two",
        ]
        for project in self.projects:
            project.mkdir(parents=True)
        self.runtime = self.root / "runtime"
        self.runtime.mkdir()
        for name in ("notify.exe", "node_repl.exe", "node.exe", "codex.exe"):
            (self.runtime / name).write_bytes(b"test")

        literal = lambda path: str(path).replace("'", "''")
        config = f"""model_reasoning_effort = "xhigh"
sandbox_mode = "workspace-write"
approval_policy = "on-request"
notify = ['{literal(self.runtime / "notify.exe")}', 'turn-ended']

[windows]
sandbox = "unelevated"

[features]
computer_use = true
multi_agent = true

[plugins."sites@openai-bundled"]
enabled = true
[plugins."browser@openai-bundled"]
enabled = true
[plugins."chrome@openai-bundled"]
enabled = true
[plugins."computer-use@openai-bundled"]
enabled = true

[mcp_servers.node_repl]
command = '{literal(self.runtime / "node_repl.exe")}'

[mcp_servers.node_repl.env]
NODE_REPL_NODE_PATH = '{literal(self.runtime / "node.exe")}'
CODEX_CLI_PATH = '{literal(self.runtime / "codex.exe")}'
"""
        (self.home / "config.toml").write_text(config, encoding="utf-8")
        project_map = {
            f"local-{index}": {
                "id": f"local-{index}",
                "name": project.name,
                "rootPaths": [str(project)],
                "createdAt": 1_700_000_000_000 + index,
                "updatedAt": 1_700_000_000_000 + index,
            }
            for index, project in enumerate(self.projects, 1)
        }
        self.original_state = {
            "local-projects": project_map,
            "project-order": list(project_map),
            "electron-saved-workspace-roots": [
                str(project) for project in self.projects
            ],
        }
        write_json(
            self.home / ".codex-global-state.json",
            self.original_state,
        )
        create_database(self.home / "state_5.sqlite", 100)
        create_database(self.home / "sqlite" / "state_5.sqlite", 101)
        for plugin in ("sites", "browser", "chrome", "computer-use"):
            for version in ("latest", "1.0.0"):
                manifest = (
                    self.home
                    / "plugins"
                    / "cache"
                    / "openai-bundled"
                    / plugin
                    / version
                    / ".codex-plugin"
                    / "plugin.json"
                )
                write_json(manifest, {"name": plugin})

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_guard(
        self,
        *,
        refresh: bool = False,
        force_snapshot: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        command = [
            sys.executable,
            str(GUARD_SCRIPT),
            "--codex-home",
            str(self.home),
            "--guard-home",
            str(self.guard),
            "--package-version",
            "99.1.2.3",
        ]
        if refresh:
            command.append("--refresh-baseline")
        if force_snapshot:
            command.append("--force-snapshot")
        return subprocess.run(
            command,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )

    def latest_report(self) -> dict:
        return json.loads(
            (self.guard / "reports" / "latest.json").read_text(
                encoding="utf-8-sig"
            )
        )

    def initialize(self) -> tuple[dict, Path]:
        result = self.run_guard(refresh=True, force_snapshot=True)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        report = self.latest_report()
        return report, Path(report["healthySnapshot"])

    def run_restore(
        self,
        snapshot: Path,
        *arguments: str,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(RESTORE_SCRIPT),
                *arguments,
                "-SnapshotPath",
                str(snapshot),
                "-CodexHome",
                str(self.home),
                "-GuardHome",
                str(self.guard),
                "-TestMode",
            ],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )

    def test_missing_baseline_is_never_adopted_implicitly(self) -> None:
        result = self.run_guard()
        self.assertEqual(result.returncode, 2)
        self.assertFalse((self.guard / "expected-state.json").exists())
        self.assertEqual(self.latest_report()["status"], "critical")

    def test_healthy_baseline_and_atomic_snapshot_are_strict(self) -> None:
        report, snapshot = self.initialize()
        expected = json.loads(
            (self.guard / "expected-state.json").read_text(
                encoding="utf-8-sig"
            )
        )
        self.assertEqual(expected["schemaVersion"], 2)
        self.assertEqual(expected["baselineThreadCounts"]["legacy"], 100)
        self.assertEqual(expected["minimumThreadCounts"]["legacy"], 95)
        self.assertEqual(
            expected["requiredPluginCaches"],
            ["browser", "chrome", "computer-use", "sites"],
        )
        self.assertTrue(snapshot.is_dir())
        self.assertFalse(
            any(
                item.name.startswith(".") and ".partial-" in item.name
                for item in snapshot.parent.iterdir()
            )
        )
        manifest = json.loads(
            (snapshot / "manifest.json").read_text(encoding="utf-8-sig")
        )
        self.assertTrue(all(item.get("relative") for item in manifest["files"]))
        self.assertEqual(report["status"], "healthy")

    def test_uninstalled_optional_plugin_is_not_forced_into_baseline(self) -> None:
        config_path = self.home / "config.toml"
        config = config_path.read_text(encoding="utf-8")
        config = config.replace(
            '[plugins."sites@openai-bundled"]\nenabled = true\n',
            "",
        )
        config_path.write_text(config, encoding="utf-8")
        shutil.rmtree(
            self.home
            / "plugins"
            / "cache"
            / "openai-bundled"
            / "sites"
        )
        result = self.run_guard(refresh=True)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        expected = json.loads(
            (self.guard / "expected-state.json").read_text(
                encoding="utf-8-sig"
            )
        )
        self.assertNotIn("sites", expected["requiredPluginCaches"])

    def test_project_order_drift_is_detected(self) -> None:
        self.initialize()
        state_path = self.home / ".codex-global-state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["project-order"].pop()
        write_json(state_path, state)
        self.assertEqual(self.run_guard().returncode, 1)
        check = next(
            item
            for item in self.latest_report()["checks"]
            if item["name"] == "projects"
        )
        self.assertEqual(check["status"], "warning")
        self.assertEqual(len(check["detail"]["missing_from_order"]), 1)

    def test_partial_and_severe_database_loss_are_distinguished(self) -> None:
        self.initialize()
        database = self.home / "state_5.sqlite"
        set_database_count(database, 98)
        self.assertEqual(self.run_guard().returncode, 1)
        partial = next(
            item
            for item in self.latest_report()["checks"]
            if item["name"] == "database_legacy"
        )
        self.assertEqual(partial["status"], "warning")
        set_database_count(database, 90)
        self.assertEqual(self.run_guard().returncode, 2)
        severe = next(
            item
            for item in self.latest_report()["checks"]
            if item["name"] == "database_legacy"
        )
        self.assertEqual(severe["status"], "error")

    def test_corrupt_database_produces_report_instead_of_crash(self) -> None:
        self.initialize()
        (self.home / "state_5.sqlite").write_bytes(b"not sqlite")
        result = self.run_guard()
        self.assertEqual(result.returncode, 2, result.stderr + result.stdout)
        check = next(
            item
            for item in self.latest_report()["checks"]
            if item["name"] == "database_legacy"
        )
        self.assertEqual(check["status"], "error")
        self.assertTrue(check["detail"]["error"])

    def test_nul_config_cannot_replace_baseline(self) -> None:
        self.initialize()
        expected_path = self.guard / "expected-state.json"
        expected_hash = hashlib.sha256(expected_path.read_bytes()).hexdigest()
        (self.home / "config.toml").write_bytes(b"\x00" * 100)
        self.assertEqual(self.run_guard().returncode, 2)
        self.assertEqual(
            hashlib.sha256(expected_path.read_bytes()).hexdigest(),
            expected_hash,
        )

    def test_nul_process_registry_is_reported_and_rejected_as_baseline(
        self,
    ) -> None:
        registry = (
            self.home / "process_manager" / "chat_processes.json"
        )
        registry.parent.mkdir(parents=True)
        registry.write_bytes(b"\x00" * 256)
        rejected = self.run_guard(refresh=True)
        self.assertEqual(
            rejected.returncode,
            2,
            rejected.stderr + rejected.stdout,
        )
        self.assertFalse((self.guard / "expected-state.json").exists())

        registry.write_text("[]\n", encoding="utf-8")
        accepted = self.run_guard(refresh=True)
        self.assertEqual(
            accepted.returncode,
            0,
            accepted.stderr + accepted.stdout,
        )
        check = next(
            item
            for item in self.latest_report()["checks"]
            if item["name"] == "process_manager"
        )
        self.assertEqual(check["status"], "ok")
        self.assertEqual(check["detail"]["record_count"], 0)

    @unittest.skipUnless(
        shutil.which("powershell.exe"),
        "Windows PowerShell is required",
    )
    def test_process_registry_repair_is_explicit_and_backed_up(self) -> None:
        registry = (
            self.home / "process_manager" / "chat_processes.json"
        )
        registry.parent.mkdir(parents=True)
        registry.write_bytes(b"\x00" * 128)
        base = [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(PROCESS_REGISTRY_SCRIPT),
            "-CodexHome",
            str(self.home),
            "-TestMode",
        ]
        inspection = subprocess.run(
            base,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )
        self.assertEqual(inspection.returncode, 2)
        self.assertEqual(registry.read_bytes(), b"\x00" * 128)

        repair = subprocess.run(
            [
                *base,
                "-ConfirmReset",
                "-ConfirmedCurrentSchemaArray",
            ],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )
        self.assertEqual(
            repair.returncode,
            0,
            repair.stderr + repair.stdout,
        )
        report = json.loads(repair.stdout)
        self.assertEqual(registry.read_text(encoding="utf-8").strip(), "[]")
        self.assertTrue(Path(report["backup"]).is_file())

    def test_version_only_plugin_cache_is_degraded(self) -> None:
        self.initialize()
        latest = (
            self.home
            / "plugins"
            / "cache"
            / "openai-bundled"
            / "browser"
            / "latest"
        )
        shutil.rmtree(latest)
        self.assertEqual(self.run_guard().returncode, 1)
        check = next(
            item
            for item in self.latest_report()["checks"]
            if item["name"] == "plugin_cache"
        )
        self.assertIn("browser", check["detail"]["unstable_latest"])

    def test_corrupt_guard_metadata_keeps_lkg_pointer(self) -> None:
        _, snapshot = self.initialize()
        (self.guard / "guard-state.json").write_bytes(b"\x00\x00")
        self.assertEqual(self.run_guard().returncode, 1)
        state = json.loads(
            (self.guard / "guard-state.json").read_text(
                encoding="utf-8-sig"
            )
        )
        self.assertEqual(Path(state["lastHealthySnapshot"]), snapshot)

    @unittest.skipUnless(
        shutil.which("powershell.exe"),
        "Windows PowerShell is required",
    )
    def test_restore_validates_hashes_and_removes_stale_wal(self) -> None:
        _, snapshot = self.initialize()
        validation = self.run_restore(snapshot, "-ValidateOnly")
        self.assertEqual(
            validation.returncode,
            0,
            validation.stderr + validation.stdout,
        )

        (self.home / "config.toml").write_text(
            'sandbox_mode = "read-only"\n',
            encoding="utf-8",
        )
        set_database_count(self.home / "state_5.sqlite", 80)
        (self.home / "state_5.sqlite-wal").write_bytes(b"stale-wal")
        (self.home / "state_5.sqlite-shm").write_bytes(b"stale-shm")
        restore = self.run_restore(
            snapshot,
            "-ConfirmRestore",
            "-NoLaunch",
        )
        self.assertEqual(
            restore.returncode,
            0,
            restore.stderr + restore.stdout,
        )
        self.assertEqual(
            (self.home / "config.toml").read_bytes(),
            (snapshot / "config.toml").read_bytes(),
        )
        self.assertEqual(database_count(self.home / "state_5.sqlite"), 100)
        self.assertFalse((self.home / "state_5.sqlite-wal").exists())
        self.assertFalse((self.home / "state_5.sqlite-shm").exists())

    @unittest.skipUnless(
        shutil.which("powershell.exe"),
        "Windows PowerShell is required",
    )
    def test_tampered_snapshot_is_rejected(self) -> None:
        _, snapshot = self.initialize()
        tampered = snapshot.parent / f"{snapshot.name}-tampered"
        shutil.copytree(snapshot, tampered)
        with (tampered / "config.toml").open("ab") as stream:
            stream.write(b"\n# tampered\n")
        result = self.run_restore(tampered, "-ValidateOnly")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("manifest verification failed", result.stderr)

    def test_project_merge_requires_stop_confirmation_and_repairs_order(self) -> None:
        project_id = next(iter(self.original_state["local-projects"]))
        state_path = self.home / ".codex-global-state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["project-order"].remove(project_id)
        write_json(state_path, state)
        root_path = state["local-projects"][project_id]["rootPaths"][0]
        manifest_path = self.root / "recovery-manifest.json"
        write_json(
            manifest_path,
            {
                "projects": [
                    {
                        "name": "项目一",
                        "rootPath": root_path,
                        "createdAt": 1_700_000_000_001,
                    }
                ]
            },
        )
        before_state = state_path.read_bytes()
        base_command = [
            sys.executable,
            str(MERGE_SCRIPT),
            "--state",
            str(state_path),
            "--config",
            str(self.home / "config.toml"),
            "--manifest",
            str(manifest_path),
        ]
        dry_run = subprocess.run(
            base_command,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )
        self.assertEqual(dry_run.returncode, 0, dry_run.stderr)
        self.assertEqual(json.loads(dry_run.stdout)["mode"], "dry-run")
        self.assertEqual(state_path.read_bytes(), before_state)

        refused = subprocess.run(
            [*base_command, "--apply"],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("--confirm-codex-stopped", refused.stderr)

        applied = subprocess.run(
            [
                *base_command,
                "--apply",
                "--confirm-codex-stopped",
                "--trust-projects",
            ],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )
        self.assertEqual(applied.returncode, 0, applied.stderr)
        state_after = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertIn(project_id, state_after["project-order"])
        report = json.loads(applied.stdout)
        self.assertTrue(Path(report["backup"]).is_dir())

    def test_read_only_audit_does_not_modify_source_state(self) -> None:
        tracked = [
            self.home / "config.toml",
            self.home / ".codex-global-state.json",
            self.home / "state_5.sqlite",
            self.home / "sqlite" / "state_5.sqlite",
        ]
        before = {
            path: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in tracked
        }
        result = subprocess.run(
            [
                sys.executable,
                str(AUDIT_SCRIPT),
                "--codex-home",
                str(self.home),
            ],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )
        self.assertIn(result.returncode, (0, 1), result.stderr)
        after = {
            path: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in tracked
        }
        self.assertEqual(before, after)

    def test_audit_distinguishes_guard_install_and_backup_roots(self) -> None:
        installed = self.home / "maintenance" / "update-guard"
        installed.mkdir(parents=True)
        for name in (
            "codex_update_guard.py",
            "Invoke-CodexUpdateGuard.ps1",
            "Invoke-CodexUpdateMaintenance.ps1",
            "Restore-CodexLastHealthy.ps1",
        ):
            (installed / name).write_text("test\n", encoding="utf-8")
        write_json(installed / "expected-state.json", {"schemaVersion": 2})
        write_json(installed / "guard-state.json", {"schemaVersion": 2})
        snapshot = (
            self.home
            / "backups_state"
            / "update-guard"
            / "healthy"
            / "20260723-000000-000000"
        )
        snapshot.mkdir(parents=True)

        result = subprocess.run(
            [
                sys.executable,
                str(AUDIT_SCRIPT),
                "--codex-home",
                str(self.home),
            ],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )
        self.assertIn(result.returncode, (0, 1), result.stderr)
        report = json.loads(result.stdout)
        metadata = report["guard_metadata"]
        self.assertTrue(metadata["installed"])
        self.assertEqual(metadata["install_root"], str(installed))
        self.assertEqual(
            metadata["backup_root"],
            str(self.home / "backups_state" / "update-guard"),
        )
        self.assertTrue(metadata["expected-state.json"]["exists"])
        self.assertTrue(metadata["guard-state.json"]["exists"])
        self.assertEqual(metadata["healthy_snapshot_count"], 1)

    def test_audit_normalizes_nested_rollout_source_metadata(self) -> None:
        rollout = (
            self.home
            / "sessions"
            / "2026"
            / "07"
            / "23"
            / "rollout-test.jsonl"
        )
        rollout.parent.mkdir(parents=True)
        rollout.write_text(
            json.dumps(
                {
                "type": "session_meta",
                "payload": {
                    "id": "local-test-thread",
                    "cwd": str(self.projects[0]),
                    "source": {
                        "subagent": {
                            "parent_thread_id": "private-parent-id",
                            "depth": 1,
                        }
                    },
                },
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )

        result = subprocess.run(
            [
                sys.executable,
                str(AUDIT_SCRIPT),
                "--codex-home",
                str(self.home),
            ],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )
        self.assertIn(result.returncode, (0, 1), result.stderr)
        report = json.loads(result.stdout)
        source_counts = report["rollouts"]["source_counts"]
        self.assertEqual(source_counts["subagent"], 1)
        self.assertNotIn("private-parent-id", json.dumps(source_counts))

    @unittest.skipUnless(
        shutil.which("powershell.exe"),
        "Windows PowerShell is required",
    )
    def test_install_maintenance_and_uninstall_in_disposable_profile(
        self,
    ) -> None:
        profile = self.root / "disposable profile"
        disposable_home = profile / ".codex"
        shutil.copytree(self.home, disposable_home)
        environment = dict(os.environ)
        environment["USERPROFILE"] = str(profile)

        install = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(INSTALL_SCRIPT),
                "-ConfirmInstall",
                "-SkipScheduledTask",
            ],
            env=environment,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )
        self.assertEqual(
            install.returncode,
            0,
            install.stderr + install.stdout,
        )
        installed = disposable_home / "maintenance" / "update-guard"
        self.assertTrue((installed / "expected-state.json").is_file())
        self.assertTrue((installed / "guard-state.json").is_file())

        maintenance = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(installed / "Invoke-CodexUpdateMaintenance.ps1"),
            ],
            env=environment,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )
        self.assertEqual(
            maintenance.returncode,
            0,
            maintenance.stderr + maintenance.stdout,
        )
        maintenance_report = json.loads(
            (installed / "maintenance-latest.json").read_text(
                encoding="utf-8-sig"
            )
        )
        self.assertEqual(maintenance_report["status"], "healthy")
        self.assertFalse(
            maintenance_report["fastPatchIntegrationEnabled"]
        )

        uninstall = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(UNINSTALL_SCRIPT),
                "-ConfirmUninstall",
            ],
            env=environment,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )
        self.assertEqual(
            uninstall.returncode,
            0,
            uninstall.stderr + uninstall.stdout,
        )
        self.assertFalse((installed / "codex_update_guard.py").exists())
        self.assertTrue((installed / "expected-state.json").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
