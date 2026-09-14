"""Run the Redstone API and the preview gateway: two origins, one process.

    python -m redstone.api.serve

They share one PreviewManager, so the API can start a preview and the
gateway can serve it, while generated JavaScript is only ever served from
the gateway's per-preview origins -- never from the API's origin. Both
listen on loopback; putting either behind a public host is a deployment
decision (see docs/redstone/PREVIEW.md for the domain requirements).
"""

from __future__ import annotations

import asyncio
import os

import uvicorn

from ..config import load_config
from ..preview.gateway import create_preview_gateway_app
from .app import create_app


async def _serve() -> None:
    config = load_config()
    api = create_app(config=config)
    previews = api.state.preview_manager
    gateway = create_preview_gateway_app(previews, config.preview)

    raw_port = os.environ.get("REDSTONE_API_PORT", "8000")
    api_port = int(raw_port) if raw_port.isdigit() and 1024 <= int(raw_port) <= 65535 else 8000
    if api_port == config.preview.listen_port:
        raise SystemExit("The API and the preview gateway must not share a port.")

    previews.start_sweeper()
    try:
        await asyncio.gather(
            uvicorn.Server(uvicorn.Config(api, host="127.0.0.1", port=api_port,
                                          server_header=False)).serve(),
            # No `Server: uvicorn` on preview responses: infrastructure stays hidden.
            uvicorn.Server(uvicorn.Config(gateway, host="127.0.0.1",
                                          port=config.preview.listen_port,
                                          server_header=False)).serve(),
        )
    finally:
        previews.stop_sweeper()


def main() -> None:
    asyncio.run(_serve())


if __name__ == "__main__":
    main()
