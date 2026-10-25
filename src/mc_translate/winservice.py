"""Windows Service host for the standalone executable.

The module is imported only by the Windows service command.  The normal
HTTP entrypoint remains dependency-free on Linux and macOS.
"""

import asyncio
import logging
import os
import threading
import sys
from pathlib import Path
import winreg

import servicemanager
import win32event
import win32service
import win32serviceutil
from aiohttp import web

from .api import create_app
from .config import Config, load_env


class TranslationService(win32serviceutil.ServiceFramework):
    _svc_name_ = "BuggcraftTranslation"
    _svc_display_name_ = "Buggcraft Minecraft Translation"
    _svc_description_ = "Buggcraft shared Minecraft resource translation service"
    _exe_name_ = sys.executable
    _exe_args_ = "--service-run"

    def __init__(self, args):
        super().__init__(args)
        self.stop_handle = win32event.CreateEvent(None, 0, 0, None)
        self.stop_requested = threading.Event()

    def SvcStop(self):
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        self.stop_requested.set()
        win32event.SetEvent(self.stop_handle)

    def SvcDoRun(self):
        log_path = Path(sys.executable).resolve().parent / "service.log"
        logging.basicConfig(
            filename=log_path,
            filemode="w",
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(message)s",
            force=True,
        )
        logging.info("Service process started")
        try:
            servicemanager.LogInfoMsg(f"{self._svc_name_} started")
            asyncio.run(self._serve())
        except Exception:  # pragma: no cover - SCM-only path
            logging.exception("Service startup failed")
            servicemanager.LogErrorMsg(f"{self._svc_name_} failed; see service.log")
            raise

    async def _serve(self):
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SYSTEM\CurrentControlSet\Services\BuggcraftTranslation\Parameters") as key:
            config_path = winreg.QueryValueEx(key, "EnvFile")[0]
        logging.info("Loading service configuration")
        os.chdir(Path(config_path).resolve().parent)
        load_env(config_path)
        config = Config.from_env()
        logging.info("Starting HTTP listener on %s:%s", config.host, config.port)
        app = await create_app(config)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, config.host, config.port)
        await site.start()
        logging.info("HTTP listener started")
        try:
            while not self.stop_requested.is_set():
                await asyncio.sleep(0.5)
        finally:
            await runner.cleanup()


def run_service():
    servicemanager.Initialize()
    servicemanager.PrepareToHostSingle(TranslationService)
    servicemanager.StartServiceCtrlDispatcher()
