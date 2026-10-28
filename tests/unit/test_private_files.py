"""Synthetic tests: no real credentials, database, or provider access."""

import asyncio
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from scripts import accounts, backup, bootstrap, manage


class FakeCursor:
    async def fetchone(self):
        return None


class FakeAsyncConnection:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def execute(self, *args, **kwargs):
        return FakeCursor()


class FakeSyncConnection:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class FakePostgresContainer:
    labels = {"io.insightpilot.fixture": "synthetic-private-file-test"}

    def reload(self):
        pass

    def exec_run(self, args, user):
        return SimpleNamespace(exit_code=0, output=b"PGDMP synthetic backup bytes")


class PrivateFilesTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="insightpilot-private-")
        self.root = Path(self.directory.name).resolve()
        self.previous_umask = os.umask(0o022) if os.name == "posix" else None
        self.env = patch.dict(
            os.environ,
            {
                "INSIGHTPILOT_WORKSPACE_ROOT": str(self.root),
                "INSIGHTPILOT_POSTGRES_ADMIN_DSN": "synthetic-not-a-connection",
                "INSIGHTPILOT_PG_STATE_PASSWORD": "synthetic-state",
                "INSIGHTPILOT_PG_CHECKPOINT_PASSWORD": "synthetic-checkpoint",
                "INSIGHTPILOT_PG_QUERY_PASSWORD": "synthetic-query",
            },
        )
        self.env.start()

    def tearDown(self):
        self.env.stop()
        if self.previous_umask is not None:
            os.umask(self.previous_umask)
        self.directory.cleanup()

    def assert_private(self, path):
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def settings(self):
        return SimpleNamespace(
            workspace_root=self.root,
            data_dir=self.root / ".data" / "insightpilot",
            postgres_state_dsn=SimpleNamespace(get_secret_value=lambda: "synthetic-state-dsn"),
        )

    def invoke_account_creation(self):
        args = SimpleNamespace(
            workspace_root=self.root,
            tenant_id=uuid4(),
            email="synthetic@example.invalid",
            name="Synthetic",
            role="analyst",
        )
        with (
            patch.object(accounts.Settings, "from_environment", return_value=self.settings()),
            patch.object(
                accounts.psycopg.AsyncConnection,
                "connect",
                AsyncMock(return_value=FakeAsyncConnection()),
            ),
            patch.object(accounts, "create_account", AsyncMock(return_value=uuid4())),
        ):
            asyncio.run(accounts.main(args))

    def invoke_initialization(self):
        version = SimpleNamespace(id=uuid4(), tenant_ids=(uuid4(), uuid4()))
        with (
            patch.object(manage.Settings, "from_environment", return_value=self.settings()),
            patch.object(manage.psycopg, "connect", return_value=FakeSyncConnection()),
            patch.object(
                manage.psycopg.AsyncConnection,
                "connect",
                AsyncMock(return_value=FakeAsyncConnection()),
            ),
            patch.object(manage, "migrate"),
            patch.object(manage, "configure_role_passwords"),
            patch.object(manage, "seed_synthetic", return_value=version),
            patch.object(manage, "setup_checkpoints", AsyncMock()),
            patch.object(
                manage, "create_account", AsyncMock(side_effect=[uuid4(), uuid4(), uuid4()])
            ),
        ):
            asyncio.run(manage.initialize(self.root, create_accounts=True))

    @unittest.skipUnless(os.name == "posix", "POSIX mode bits do not configure Windows ACLs")
    def test_fresh_and_existing_private_env_are_owner_only(self):
        bootstrap.initialize(self.root)
        path = self.root / ".env.insightpilot"
        self.assert_private(path)
        before = path.read_bytes()
        os.chmod(path, 0o644)
        result = bootstrap.initialize(self.root)
        self.assertTrue(result["existing_configuration_preserved"])
        self.assertEqual(path.read_bytes(), before)
        self.assert_private(path)

    @unittest.skipUnless(os.name == "posix", "POSIX mode bits do not configure Windows ACLs")
    def test_initial_account_file_is_owner_only(self):
        self.invoke_initialization()
        destination = self.settings().data_dir / "accounts.json"
        self.assertEqual(len(json.loads(destination.read_text(encoding="utf-8"))), 3)
        self.assert_private(destination)

    @unittest.skipUnless(os.name == "posix", "POSIX mode bits do not configure Windows ACLs")
    def test_individual_account_file_is_owner_only(self):
        self.invoke_account_creation()
        destination = self.settings().data_dir / "accounts.json"
        self.assertEqual(len(json.loads(destination.read_text(encoding="utf-8"))), 1)
        self.assert_private(destination)

    @unittest.skipUnless(os.name == "posix", "POSIX mode bits do not configure Windows ACLs")
    def test_synthetic_backup_and_checksum_are_owner_only(self):
        path = backup.backup_container(
            FakePostgresContainer(), "insightpilot_fixture", self.root / ".data" / "backup"
        )
        self.assert_private(path)
        self.assert_private(path.with_suffix(".sha256"))

    def test_bootstrap_direct_script_works_outside_project_with_no_pythonpath(self):
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)
        env["PYTHONNOUSERSITE"] = "1"
        result = subprocess.run(
            [
                sys.executable,
                str(Path(bootstrap.__file__).resolve()),
                "--workspace-root",
                str(self.root),
            ],
            cwd=self.root,
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.root / ".env.insightpilot").exists())

    def test_failed_atomic_account_update_keeps_original_and_removes_temp(self):
        destination = self.settings().data_dir / "accounts.json"
        destination.parent.mkdir(parents=True)
        destination.write_text("[]", encoding="utf-8")
        with patch("os.replace", side_effect=OSError("synthetic failure")):
            with self.assertRaises(OSError):
                self.invoke_account_creation()
        self.assertEqual(destination.read_text(encoding="utf-8"), "[]")
        self.assertEqual([path.name for path in destination.parent.iterdir()], ["accounts.json"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
