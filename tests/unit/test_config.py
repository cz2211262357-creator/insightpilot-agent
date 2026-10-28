from pathlib import Path

import pytest

from insightpilot.config import ConfigurationError, Settings


@pytest.mark.parametrize(
    "variable", ["INSIGHTPILOT_DATA_DIR", "INSIGHTPILOT_TEMP_DIR", "INSIGHTPILOT_CACHE_DIR"]
)
def test_rejects_managed_paths_outside_workspace(workspace_root, monkeypatch, variable):
    monkeypatch.setenv(variable, str(workspace_root.parent / "outside-agent"))
    with pytest.raises(ConfigurationError):
        Settings.from_environment(workspace_root)


def test_rejects_relative_workspace_root():
    with pytest.raises(ConfigurationError):
        Settings.from_environment(Path("relative-project"))


def test_missing_model_credentials_fail_closed(workspace_root, monkeypatch):
    monkeypatch.delenv("INSIGHTPILOT_MODEL_API_KEY")
    with pytest.raises(ConfigurationError):
        Settings.from_environment(workspace_root)


def test_secrets_never_appear_in_configuration_representation(workspace_root):
    settings = Settings.from_environment(workspace_root)
    for rendered in [repr(settings), settings.model_dump_json()]:
        assert "synthetic-not-a-provider-key" not in rendered
        assert "synthetic-secret" not in rendered


def test_invalid_dsn_errors_do_not_echo_password(workspace_root, monkeypatch):
    monkeypatch.setenv(
        "INSIGHTPILOT_POSTGRES_QUERY_DSN", "not-a-dsn://user:do-not-disclose-this-password@host"
    )
    with pytest.raises(ConfigurationError) as error:
        Settings.from_environment(workspace_root)
    assert "do-not-disclose-this-password" not in str(error.value)


def test_managed_paths_resolve_under_the_requested_physical_root(workspace_root):
    settings = Settings.from_environment(workspace_root)
    assert settings.data_dir == workspace_root / ".data/insightpilot"
    assert settings.temp_dir == workspace_root / ".cache/insightpilot-tmp"
