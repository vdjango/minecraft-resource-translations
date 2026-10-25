import logging
import os
import sys

from aiohttp import web

from .api import create_app
from .config import Config, load_env


def main():
    # Service installers pass a config file explicitly; local runs keep the
    # existing .env convention.
    config_path = os.environ.get("TRANSLATION_ENV_FILE", ".env")
    load_env(config_path)
    config = Config.from_env()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    web.run_app(create_app(config), host=config.host, port=config.port, access_log=None)


def windows_service_command(args):
    """Install, remove, start, or stop the optional Windows service."""
    if sys.platform != "win32":
        raise SystemExit("Windows service commands are only available on Windows")
    try:
        from .winservice import TranslationService
        import win32serviceutil
    except ImportError as exc:
        raise SystemExit(
            "Windows service support requires the package built with the windows extra"
        ) from exc
    sys.argv = [sys.argv[0], *args]
    win32serviceutil.HandleCommandLine(TranslationService)


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "service":
        windows_service_command(sys.argv[2:])
    else:
        main()
