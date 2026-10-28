"Role-specific entry point. Service logic is added by its implementation task."

import argparse
import os
from pathlib import Path

import uvicorn

from insightpilot.api import create_app
from insightpilot.config import ConfigurationError, Settings


def main() -> None:
    parser = argparse.ArgumentParser(prog="insightpilot")
    parser.add_argument("role", choices=("api", "worker", "mcp"))
    parser.add_argument("--workspace-root", default=os.getenv("INSIGHTPILOT_WORKSPACE_ROOT"))
    parser.add_argument("--host", default="127.0.0.1", choices=("127.0.0.1", "0.0.0.0", "::1"))
    parser.add_argument("--port", type=int)
    args = parser.parse_args()
    os.environ["INSIGHTPILOT_PROCESS_ROLE"] = args.role
    try:
        settings = Settings.from_environment(
            Path(args.workspace_root) if args.workspace_root else Path.cwd()
        )
    except ConfigurationError:
        parser.error("invalid process configuration; load the private environment file")
    port = (
        args.port
        if args.port is not None
        else {"api": 8010, "mcp": 8011, "worker": 8012}[args.role]
    )
    if not 0 <= port <= 65535:
        parser.error("port is outside the allowed range")
    from insightpilot.mcp_server import create_mcp_app

    uvicorn.run(
        create_mcp_app(settings) if args.role == "mcp" else create_app(settings),
        host=args.host,
        port=port,
        loop="insightpilot.runtime:new_event_loop",
        access_log=False,
    )


if __name__ == "__main__":
    main()
