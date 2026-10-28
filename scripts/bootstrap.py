"Initialize only an absent private local environment; never prints a credential."

import argparse
import json
import re
import secrets
from pathlib import Path

if __package__:
    from .private_files import create_private, restrict_existing_private
else:
    from private_files import create_private, restrict_existing_private


def literal_values(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    values = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        matched = re.fullmatch(r"\s*([A-Z][A-Z0-9_]*)\s*=\s*(.*?)\s*", line)
        if matched:
            value = matched[2]
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            values[matched[1]] = value
    return values


def initialize(root: Path) -> dict[str, bool]:
    if not root.is_absolute():
        raise ValueError("WorkspaceRoot must be absolute")
    root = root.resolve()
    private = root / ".env.insightpilot"
    if private.exists():
        restrict_existing_private(private)
        return {"created": False, "existing_configuration_preserved": True}
    model = literal_values(root / ".env")
    owner, state, checkpoint, reader, redis = [secrets.token_hex(32) for _ in range(5)]
    pairs = {
        "INSIGHTPILOT_PG_OWNER_PASSWORD": owner,
        "INSIGHTPILOT_PG_STATE_PASSWORD": state,
        "INSIGHTPILOT_PG_CHECKPOINT_PASSWORD": checkpoint,
        "INSIGHTPILOT_PG_QUERY_PASSWORD": reader,
        "INSIGHTPILOT_REDIS_PASSWORD": redis,
        "INSIGHTPILOT_POSTGRES_ADMIN_DSN": f"postgresql://postgres:{owner}@127.0.0.1:5433/insightpilot",
        "INSIGHTPILOT_POSTGRES_STATE_DSN": f"postgresql://ip_state:{state}@127.0.0.1:5433/insightpilot",
        "INSIGHTPILOT_POSTGRES_CHECKPOINT_DSN": f"postgresql://ip_checkpoint:{checkpoint}@127.0.0.1:5433/insightpilot",
        "INSIGHTPILOT_POSTGRES_QUERY_DSN": f"postgresql://ip_reader:{reader}@127.0.0.1:5433/insightpilot",
        "INSIGHTPILOT_REDIS_URL": f"redis://:{redis}@127.0.0.1:6382/0",
        "INSIGHTPILOT_MCP_SIGNING_KEY": secrets.token_hex(32),
        "INSIGHTPILOT_MODEL_API_KEY": model.get("SERVICEHUB_MODEL_API_KEY", ""),
        "INSIGHTPILOT_MODEL_BASE_URL": model.get(
            "SERVICEHUB_MODEL_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"
        ),
        "INSIGHTPILOT_MODEL_NAME": model.get("SERVICEHUB_MODEL_NAME", "qwen-plus"),
    }
    if any("\n" in value or "\r" in value for value in pairs.values()):
        raise ValueError("Invalid multiline environment value")
    create_private(private, "".join(f"{key}={value}\n" for key, value in pairs.items()).encode())
    return {"created": True, "model_key_configured": bool(pairs["INSIGHTPILOT_MODEL_API_KEY"])}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace-root", type=Path, required=True)
    print(json.dumps(initialize(parser.parse_args().workspace_root)))
