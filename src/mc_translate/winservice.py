"""Windows Service host for the standalone executable.

The module is imported only by the Windows service command.  The normal
HTTP entrypoint remains dependency-free on Linux and macOS.
"""

import asyncio
import logging
import os
import threading
import sys

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
    _exe_args_ = ""

    def __init__(self, args):
        super().__init__(args)
        self.stop_handle = win32event.CreateEvent(None, 0, 0, None)
        self.stop_requested = threading.Event()

    def GetAcceptedControls(self):
        accepted = super().GetAcceptedControls()
        return accepted | win32service.SERVICE_ACCEPT_PRESHUTDOWN

    def SvcStop(self):
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        self.stop_requested.set()
        win32event.SetEvent(self.stop_handle)

    def SvcDoRun(self):
        servicemanager.LogInfoMsg(f"{self._svc_name_} started")
        try:
            asyncio.run(self._serve())
        except Exception as exc:  # pragma: no cover - SCM-only path
            servicemanager.LogErrorMsg(f"{self._svc_name_} failed: {exc}")
            raise

    async def _serve(self):
        config_path = os.environ.get("TRANSLATION_ENV_FILE", ".env")
        load_env(config_path)
        config = Config.from_env()
        runner = web.AppRunner(create_app(config))
        await runner.setup()
        site = web.TCPSite(runner, config.host, config.port)
        await site.start()
        try:
            while not self.stop_requested.is_set():
                await asyncio.sleep(0.5)
        finally:
            await runner.cleanup()
