"""Entry point: `python -m app` (used by the Docker image). Listens on all interfaces for the LAN."""
from __future__ import annotations

import uvicorn

from .config import load_config
from .main import create_app


def main() -> None:
    cfg = load_config()
    app = create_app(cfg, start_threads=True)
    uvicorn.run(app, host="0.0.0.0", port=cfg.port, log_level="info")


if __name__ == "__main__":
    main()
