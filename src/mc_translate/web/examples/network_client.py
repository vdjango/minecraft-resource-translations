"""Optional Python client: nearest read mirror, anonymous installation ID, fallback.

Standard library only. Writes and all bearer credentials always target the hub.
Call select() once before browsing, then occasionally in a background thread.
"""

import concurrent.futures
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path


class TranslationNetwork:
    def __init__(self, hub="https://translate.bugg.icu", instance_file=None):
        self.hub = hub.rstrip("/")
        self.selected = self.hub
        self.failed_until = {}
        self.lock = threading.Lock()
        file = Path(instance_file) if instance_file else Path.home() / ".cache" / "minecraft-translations" / "instance-id"
        file.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.instance = str(uuid.UUID(file.read_text(encoding="utf-8").strip()))
        except (OSError, ValueError):
            self.instance = str(uuid.uuid4())
            fd = os.open(file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(self.instance)

    def _request(self, base, path, data=None, headers=None, timeout=15):
        safe = dict(headers or {})
        if base != self.hub:
            safe = {key: value for key, value in safe.items() if key.lower() not in ("authorization", "cookie", "idempotency-key")}
        request = urllib.request.Request(base + "/v1/" + path, data=None if data is None else json.dumps(data).encode(), headers={"Accept": "application/json", "Content-Type": "application/json", "User-Agent": "Translation-Launcher-SDK/1.0 Python", "X-Translation-Instance": self.instance, **safe})
        # Never follow mirror redirects with user headers.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, request, fp, code, message, headers, url):
                return None
        opener = urllib.request.build_opener(NoRedirect)
        with opener.open(request, timeout=timeout) as response:
            raw = response.read(4 * 1024 * 1024 + 1)
            if len(raw) > 4 * 1024 * 1024:
                raise ValueError("Response exceeds limit")
            return json.loads(raw)

    def select(self):
        try:
            directory = self._request(self.hub, "federation/nodes", timeout=2)
            if directory.get("hub_url") != self.hub or directory.get("protocol") != "translation-mirror-v1":
                return self.hub
            def probe(node):
                base = node["base_url"]
                parsed = urllib.parse.urlsplit(base)
                local_test = parsed.hostname == "127.0.0.1" and urllib.parse.urlsplit(self.hub).hostname == "127.0.0.1"
                if parsed.scheme != "https" and not local_test or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
                    raise ValueError("Invalid mirror URL")
                start = time.perf_counter()
                health = self._request(base, "federation/health", timeout=1.5)
                if not health.get("ready") or health.get("hub_url") != self.hub or health.get("node_id") != node["id"]:
                    raise ValueError("Wrong mirror")
                return (time.perf_counter() - start, base)
            candidates = []
            with concurrent.futures.ThreadPoolExecutor(max_workers=7) as pool:
                for future in concurrent.futures.as_completed([pool.submit(probe, node) for node in directory["nodes"][:7]]):
                    try:
                        candidates.append(future.result())
                    except Exception:
                        pass
            with self.lock:
                valid = sorted((duration, base) for duration, base in candidates if self.failed_until.get(base, 0) < time.monotonic())
                self.selected = valid[0][1] if valid else self.hub
            return self.selected
        except Exception:
            return self.hub

    @staticmethod
    def public_read(path, data):
        return (path == "translations:resolve") or (data is None and (path.startswith(("catalog/", "artifacts/")) or path == "capabilities" or path.startswith("projects/") and path.split("?", 1)[0].rsplit("/", 1)[-1] in ("manifest", "source", "history")))

    def call(self, path, data=None, headers=None):
        with self.lock:
            base = self.selected
        if self.public_read(path, data) and base != self.hub:
            try:
                return self._request(base, path, data, headers, timeout=2.5)
            except Exception:
                with self.lock:
                    self.failed_until[base] = time.monotonic() + 30
                    self.selected = self.hub
        return self._request(self.hub, path, data, headers)
