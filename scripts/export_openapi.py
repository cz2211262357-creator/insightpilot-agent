"Export schema without reading local credentials or connecting any dependency."

import argparse
import json
from pathlib import Path

from pydantic import SecretStr

from insightpilot.api import create_app
from insightpilot.config import Settings


def export(root):
    return create_app(
        Settings(
            workspace_root=root,
            data_dir=root / ".data/insightpilot",
            cache_dir=root / ".cache/insightpilot",
            temp_dir=root / ".cache/insightpilot-tmp",
            mcp_signing_key=SecretStr("documentation-only-no-runtime-key-32"),
            model_api_key=SecretStr("documentation-only"),
            postgres_state_dsn=SecretStr(
                "postgresql://ip_state:documentation@127.0.0.1:5433/insightpilot"
            ),
            postgres_checkpoint_dsn=SecretStr(
                "postgresql://ip_checkpoint:documentation@127.0.0.1:5433/insightpilot"
            ),
            postgres_query_dsn=SecretStr(
                "postgresql://ip_reader:documentation@127.0.0.1:5433/insightpilot"
            ),
            redis_url=SecretStr("redis://:documentation@127.0.0.1:6382/0"),
        )
    ).openapi()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = args.workspace_root.resolve()
    output = args.output.resolve()
    if not output.is_relative_to(root):
        parser.error("output must remain inside the workspace")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(export(root), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
