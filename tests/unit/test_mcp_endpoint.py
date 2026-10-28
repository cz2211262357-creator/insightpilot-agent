import pytest

from insightpilot.config import ConfigurationError, Settings


@pytest.mark.parametrize(
    "url",
    [
        "https://attacker.invalid/mcp",
        "http://127.0.0.1.evil.invalid/mcp",
        "http://user:secret@localhost:8011/mcp",
        "http://127.0.0.1:8011/mcp?url=other",
        "http://mcp:9999/mcp",
        "http://localhost:8011/arbitrary",
    ],
)
def test_model_tool_service_endpoint_is_fixed_local(url, workspace_root, monkeypatch):
    monkeypatch.setenv("INSIGHTPILOT_MCP_URL", url)
    with pytest.raises(ConfigurationError) as error:
        Settings.from_environment(workspace_root)
    assert "secret" not in str(error.value)
