"Validated process configuration; credentials never enter representations or errors."

import os
from pathlib import Path
from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, SecretStr, model_validator


class ConfigurationError(ValueError):
    """Safe configuration error code without supplied values."""


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    workspace_root: Path
    data_dir: Path
    cache_dir: Path
    temp_dir: Path
    mcp_signing_key: SecretStr
    model_api_key: SecretStr
    postgres_state_dsn: SecretStr
    postgres_checkpoint_dsn: SecretStr
    postgres_query_dsn: SecretStr
    redis_url: SecretStr
    mcp_url: str = "http://127.0.0.1:8011/mcp"
    model_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    model_name: str = "qwen-plus"
    process_role: Literal["api", "worker", "mcp"] = "api"
    web_origins: tuple[str, ...] = ("http://localhost:8090", "http://127.0.0.1:8090")
    environment: Literal["local", "test", "production"] = "local"

    @model_validator(mode="after")
    def validate_boundaries(self) -> Self:
        root = self.workspace_root
        if not root.is_absolute():
            raise ValueError("workspace_root_must_be_absolute")
        root = root.resolve()
        object.__setattr__(self, "workspace_root", root)
        for name in ("data_dir", "cache_dir", "temp_dir"):
            value = getattr(self, name)
            if not value.is_absolute():
                raise ValueError("managed_path_must_be_absolute")
            value = value.resolve()
            if value == root or not value.is_relative_to(root):
                raise ValueError("managed_path_outside_workspace")
            object.__setattr__(self, name, value)
        if not self.model_api_key.get_secret_value().strip():
            raise ValueError("model_credentials_missing")
        for origin in self.web_origins:
            endpoint = urlsplit(origin)
            if (
                endpoint.scheme not in {"http", "https"}
                or not endpoint.hostname
                or endpoint.path
                or endpoint.query
                or endpoint.fragment
                or endpoint.username
                or endpoint.password
                or self.environment == "production"
                and endpoint.scheme != "https"
            ):
                raise ValueError("invalid_web_origin")
        if len(self.mcp_signing_key.get_secret_value()) < 32:
            raise ValueError("mcp_signing_key_missing")
        endpoint = urlsplit(self.mcp_url)
        if (
            endpoint.scheme != "http"
            or endpoint.hostname not in {"127.0.0.1", "localhost", "::1", "mcp"}
            or endpoint.path != "/mcp"
            or endpoint.query
            or endpoint.fragment
            or endpoint.username
            or endpoint.password
            or endpoint.hostname == "mcp"
            and endpoint.port != 8011
        ):
            raise ValueError("invalid_local_mcp_endpoint")
        roles = []
        for secret in (
            self.postgres_state_dsn,
            self.postgres_checkpoint_dsn,
            self.postgres_query_dsn,
        ):
            uri = urlsplit(secret.get_secret_value())
            if (
                uri.scheme not in {"postgres", "postgresql"}
                or not uri.hostname
                or not uri.username
                or not uri.password
                or not uri.path.strip("/")
            ):
                raise ValueError("invalid_database_configuration")
            if uri.username.lower() in {"postgres", "root"}:
                raise ValueError("application_must_not_use_database_owner")
            roles.append(uri.username)
        if len(set(roles)) != 3:
            raise ValueError("database_roles_must_be_separate")
        uri = urlsplit(self.redis_url.get_secret_value())
        if uri.scheme not in {"redis", "rediss"} or not uri.hostname or not uri.password:
            raise ValueError("invalid_redis_configuration")
        uri = urlsplit(self.model_base_url)
        if (
            not uri.hostname
            or uri.username
            or uri.password
            or uri.query
            or uri.fragment
            or uri.scheme not in {"http", "https"}
            or uri.scheme == "http"
            and uri.hostname not in {"127.0.0.1", "localhost", "::1"}
        ):
            raise ValueError("invalid_model_endpoint")
        return self

    @classmethod
    def from_environment(cls, workspace_root: Path) -> Self:
        "Read process env only; launch scripts explicitly load the private local file."
        try:
            return cls(
                workspace_root=workspace_root,
                data_dir=Path(
                    os.getenv("INSIGHTPILOT_DATA_DIR", str(workspace_root / ".data/insightpilot"))
                ),
                cache_dir=Path(
                    os.getenv("INSIGHTPILOT_CACHE_DIR", str(workspace_root / ".cache/insightpilot"))
                ),
                temp_dir=Path(
                    os.getenv(
                        "INSIGHTPILOT_TEMP_DIR", str(workspace_root / ".cache/insightpilot-tmp")
                    )
                ),
                mcp_signing_key=os.getenv("INSIGHTPILOT_MCP_SIGNING_KEY", ""),
                model_api_key=os.getenv("INSIGHTPILOT_MODEL_API_KEY", ""),
                postgres_state_dsn=os.getenv("INSIGHTPILOT_POSTGRES_STATE_DSN", ""),
                postgres_checkpoint_dsn=os.getenv("INSIGHTPILOT_POSTGRES_CHECKPOINT_DSN", ""),
                postgres_query_dsn=os.getenv("INSIGHTPILOT_POSTGRES_QUERY_DSN", ""),
                redis_url=os.getenv("INSIGHTPILOT_REDIS_URL", ""),
                mcp_url=os.getenv("INSIGHTPILOT_MCP_URL", "http://127.0.0.1:8011/mcp"),
                model_base_url=os.getenv(
                    "INSIGHTPILOT_MODEL_BASE_URL",
                    "https://dashscope.aliyuncs.com/compatible-mode/v1",
                ),
                model_name=os.getenv("INSIGHTPILOT_MODEL_NAME", "qwen-plus"),
                process_role=os.getenv("INSIGHTPILOT_PROCESS_ROLE", "api"),
                web_origins=tuple(
                    os.getenv(
                        "INSIGHTPILOT_WEB_ORIGINS", "http://localhost:8090,http://127.0.0.1:8090"
                    ).split(",")
                ),
                environment=os.getenv("INSIGHTPILOT_ENVIRONMENT", "local"),
            )
        except (ValueError, TypeError, OSError):
            raise ConfigurationError("invalid_process_configuration") from None
