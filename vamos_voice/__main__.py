"""``python -m vamos_voice``: validate the environment, then serve /health and /voice."""

from __future__ import annotations

import logging
import os
import sys

import uvicorn

from .server import ConfigError, build_app_from_env


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("VAMOS_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        app = build_app_from_env(os.environ)
    except ConfigError as err:
        logging.getLogger("vamos_voice").critical("cannot start: %s", err)
        sys.exit(2)
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8008")), log_level="info")


if __name__ == "__main__":
    main()
