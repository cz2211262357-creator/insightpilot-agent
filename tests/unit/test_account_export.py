import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location(
    "account_cli", Path(__file__).resolve().parents[2] / "scripts/accounts.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_credentials_export_refuses_source_or_external_directory(workspace_root):
    for destination in (workspace_root / "src", workspace_root.parent / "outside"):
        with pytest.raises(ValueError, match="private_credential_directory"):
            module.private_credential_path(
                SimpleNamespace(workspace_root=workspace_root, data_dir=destination)
            )
    private = workspace_root / ".data/insightpilot"
    assert (
        module.private_credential_path(
            SimpleNamespace(workspace_root=workspace_root, data_dir=private)
        )
        == private / "accounts.json"
    )
