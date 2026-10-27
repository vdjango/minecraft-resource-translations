"""Hub-managed public read mirrors, admission, health leases and read-through cache.

Mirrors never receive model keys, administrator credentials or caller tokens.
Only approved live nodes are discoverable. Dynamic data has a short strict TTL;
content-addressed artifacts are verified before entering a bounded disk cache.
"""

import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import secrets
import time
from collections import OrderedDict
from pathlib import Path
from urllib.parse import urlsplit

from aiohttp import ClientSession, ClientTimeout, TCPConnector, web
from aiohttp.resolver import DefaultResolver

from .hashing import canonical
from .management import key_hash
from .store import billing_period

LOG = logging.getLogger(__name__)


def origin(value, allow_private=False):
    if not isinstance(value, str) or len(value) > 300:
        raise ValueError("invalid_node_url")
    parsed = urlsplit(value)
    if (parsed.scheme != "https" and not (allow_private and parsed.scheme == "http")) or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/"):
        raise ValueError("invalid_node_url")
    if not allow_private and parsed.port not in (None, 443):
        raise ValueError("invalid_node_url")
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        if not re.fullmatch(r"[A-Za-z0-9.-]+", parsed.hostname) or any(not 1 <= len(label) <= 63 for label in parsed.hostname.split(".")):
            raise ValueError("invalid_node_url")
        if parsed.hostname.lower() == "localhost" or parsed.hostname.endswith(".local"):
            if not allow_private:
                raise ValueError("invalid_node_url")
    else:
        if not allow_private and not address.is_global:
            raise ValueError("invalid_node_url")
    return f"{parsed.scheme}://{parsed.netloc.lower()}"


class PublicResolver(DefaultResolver):
    async def resolve(self, host, port=0, family=0):
        records = await super().resolve(host, port, family)
        if not records or any(not ipaddress.ip_address(record["host"]).is_global for record in records):
            raise OSError("Private federation address blocked")
        return records


class Federation:
    def __init__(self, app, config, store):
        self.app, self.config, self.store = app, config, store
        self.hub = origin(config.federation_hub, config.federation_allow_private) if config.federation_role == "mirror" else config.public_url.rstrip("/")
        self.directory = Path(config.database).resolve().parent
        self.credentials_path = self.directory / "federation-node.json"
        self.artifacts = self.directory / "mirror-artifacts"
        self.credentials = None
        self.state = "pending"
        self.active_until = 0
        self.cache = OrderedDict()
        self.cache_bytes = 0
        self.inflight = {}
        self.task = None
        self.stop = asyncio.Event()
        self.public_origins = {self.hub} if config.federation_role == "mirror" else set()
        self.status_error = None
        self.maintenance_day = None
        self.artifact_lock = asyncio.Lock()
        self.disk_index = OrderedDict()
        self.disk_bytes = 0

    async def open(self):
        await self.store.db.executescript("""
            CREATE TABLE IF NOT EXISTS federation_nodes(
                id TEXT PRIMARY KEY,token_hash TEXT NOT NULL,public_url TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,region TEXT NOT NULL,contact TEXT NOT NULL,state TEXT NOT NULL,
                created REAL NOT NULL,last_seen REAL NOT NULL DEFAULT 0,last_verified REAL NOT NULL DEFAULT 0,
                latency_ms REAL,stats TEXT NOT NULL DEFAULT '{}');
            CREATE INDEX IF NOT EXISTS federation_live ON federation_nodes(state,last_seen,last_verified);
            CREATE UNIQUE INDEX IF NOT EXISTS federation_token ON federation_nodes(token_hash);
        """)
        self.http = ClientSession(connector=TCPConnector(limit=24, limit_per_host=8, resolver=None if self.config.federation_allow_private else PublicResolver()), timeout=ClientTimeout(total=5), headers={"User-Agent": "Buggcraft-Federation-Mirror/1.0"})
        if self.config.federation_role == "mirror":
            origin(self.config.public_url, self.config.federation_allow_private)
            if self.hub == self.config.public_url.rstrip("/"):
                raise ValueError("Mirror cannot use itself as hub")
            self.artifacts.mkdir(parents=True, exist_ok=True)
            self.disk_index = await asyncio.to_thread(self.scan_artifacts)
            self.disk_bytes = sum(self.disk_index.values())
            await self.trim_artifacts()
            if self.credentials_path.exists():
                saved = json.loads(self.credentials_path.read_text(encoding="utf-8"))
                if saved.get("hub") != self.hub or saved.get("public_url") != self.config.public_url.rstrip("/"):
                    raise ValueError("Existing node identity belongs to another hub/public URL")
                self.credentials = saved
        await self.refresh_origins()
        return self

    async def refresh_origins(self):
        if self.config.federation_role == "hub":
            rows = await self.store.rows("SELECT public_url FROM federation_nodes WHERE state='active' AND last_seen>? AND last_verified>?", (time.time() - 90, time.time() - 120))
            self.public_origins = {row["public_url"] for row in rows}

    async def start(self):
        self.task = asyncio.create_task(self.run())

    async def run(self):
        while not self.stop.is_set():
            try:
                if self.config.federation_role == "mirror":
                    await self.join_or_heartbeat()
                else:
                    rows = await self.store.rows("SELECT * FROM federation_nodes WHERE state='active' AND last_seen>? AND last_verified<? ORDER BY last_verified LIMIT 8", (time.time() - 90, time.time() - 45))
                    await asyncio.gather(*(self.probe(row) for row in rows))
                    await self.refresh_origins()
            except Exception as exc:
                self.status_error = type(exc).__name__
                LOG.warning("Federation maintenance will retry (%s)", self.status_error)
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=30)
            except asyncio.TimeoutError:
                pass

    async def close(self):
        self.stop.set()
        if self.task:
            await self.task
        if self.inflight:
            await asyncio.gather(*self.inflight.values(), return_exceptions=True)
        await self.http.close()

    def save_credentials(self, data):
        temporary = self.credentials_path.with_suffix(".tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream)
        os.replace(temporary, self.credentials_path)

    async def join_or_heartbeat(self):
        if not self.credentials:
            payload = dict(name=self.config.federation_name, public_url=self.config.public_url.rstrip("/"), region=self.config.federation_region, contact=self.config.federation_contact)
            async with self.http.post(self.hub + "/v1/federation/nodes", json=payload, allow_redirects=False) as response:
                data = await self.read_response(response)
                if response.status != 201:
                    raise ValueError("Node registration was not accepted")
            self.credentials = dict(hub=self.hub, public_url=self.config.public_url.rstrip("/"), id=data["id"], token=data["node_token"])
            await asyncio.to_thread(self.save_credentials, self.credentials)
        traffic = await self.app["traffic"].snapshot(1)
        payload = dict(day=traffic["day"], requests=traffic["today"]["requests"], errors=traffic["today"]["errors"], fields=traffic["today"]["fields"], ready_fields=traffic["today"]["ready_fields"], launcher_instances=traffic["today"]["launcher_instances"], cache_hits=traffic["today"]["cache_hits"])
        async with self.http.post(self.hub + "/v1/federation/heartbeat", json=payload, headers={"Authorization": "Bearer " + self.credentials["token"]}, allow_redirects=False) as response:
            data = await self.read_response(response)
            if response.status != 200:
                self.state = "unavailable"
                if response.status in (401, 403):
                    self.active_until = 0
                raise ValueError("Node heartbeat was not accepted")
        self.state = data["state"]
        self.active_until = time.monotonic() + 90 if self.state == "active" else 0
        self.status_error = None
        if self.state == "active":
            async with self.http.get(self.hub + "/v1/federation/nodes", allow_redirects=False) as response:
                directory = await self.read_response(response)
                if response.status == 200:
                    self.public_origins = {self.hub} | {item["base_url"] for item in directory["nodes"]}
        if self.maintenance_day != traffic["day"]:
            await self.trim_artifacts()
            self.maintenance_day = traffic["day"]

    @staticmethod
    async def read_response(response):
        return json.loads(await Federation.read_bytes(response))

    @staticmethod
    async def read_bytes(response):
        body = bytearray()
        async for chunk in response.content.iter_chunked(65536):
            body.extend(chunk)
            if len(body) > 4 * 1024 * 1024:
                raise ValueError("Federation response exceeds limit")
        return bytes(body)

    async def registration(self, request):
        from .api import ApiError
        self.app["api"].rate(request, "node-application", 3, 86400)
        if self.config.federation_role != "hub":
            raise ApiError("hub_required", 409)
        data = await request.json()
        if not isinstance(data, dict) or set(data) != {"name", "public_url", "region", "contact"}:
            raise ApiError("invalid_request")
        for key, limit in (("name", 80), ("region", 64), ("contact", 254)):
            if not isinstance(data[key], str) or not 1 <= len(data[key].strip()) <= limit or any(ord(c) < 32 for c in data[key]):
                raise ApiError("invalid_request")
        url = origin(data["public_url"], self.config.federation_allow_private)
        if url == self.config.public_url.rstrip("/"):
            raise ApiError("invalid_node_url")
        node_id, token = secrets.token_hex(12), "bcn_" + secrets.token_urlsafe(32)
        async with self.store.transaction(invalidate=False) as db:
            if (await (await db.execute("SELECT COUNT(*) FROM federation_nodes")).fetchone())[0] >= 1000:
                raise ApiError("node_registration_full", 503, 3600)
            if await (await db.execute("SELECT 1 FROM federation_nodes WHERE public_url=?", (url,))).fetchone():
                raise ApiError("node_already_registered", 409)
            await db.execute("INSERT INTO federation_nodes(id,token_hash,public_url,name,region,contact,state,created) VALUES(?,?,?,?,?,?,?,?)", (node_id, key_hash(token), url, data["name"].strip(), data["region"].strip(), data["contact"].strip(), "pending", time.time()))
        return web.json_response(dict(id=node_id, node_token=token, state="pending", heartbeat_seconds=30), status=201)

    async def node_auth(self, request):
        from .api import ApiError
        token = request.headers.get("Authorization", "").removeprefix("Bearer ")
        if not token.startswith("bcn_") or len(token) > 128:
            raise ApiError("invalid_node_token", 401)
        row = await self.store.one("SELECT * FROM federation_nodes WHERE token_hash=?", (key_hash(token),))
        if not row:
            raise ApiError("invalid_node_token", 401)
        return row

    async def heartbeat(self, request):
        from .api import ApiError
        self.app["api"].rate(request, "node-heartbeat", 30)
        row = await self.node_auth(request)
        if row["state"] == "revoked":
            raise ApiError("node_revoked", 403)
        data = await request.json()
        names = {"requests", "errors", "fields", "ready_fields", "launcher_instances", "cache_hits"}
        if not isinstance(data, dict) or set(data) != names | {"day"} or data["day"] != billing_period()[0] or any(type(data[name]) is not int or not 0 <= data[name] <= 10**12 for name in names):
            raise ApiError("invalid_request")
        async with self.store.transaction(invalidate=False) as db:
            await db.execute("UPDATE federation_nodes SET last_seen=?,stats=? WHERE id=?", (time.time(), json.dumps(data), row["id"]))
        return web.json_response(dict(state=row["state"], heartbeat_seconds=30))

    async def health(self, request):
        value = dict(protocol="translation-mirror-v1", role=self.config.federation_role, hub_url=self.hub, node_id=self.credentials["id"] if self.credentials else "hub", ready=self.config.federation_role == "hub" or self.active_until > time.monotonic())
        nonce = request.query.get("nonce", "")
        if self.credentials and re.fullmatch("[a-f0-9]{32}", nonce):
            value["proof"] = hmac.digest(key_hash(self.credentials["token"]).encode(), nonce.encode(), "sha256").hex()
        return web.json_response(value, headers={"Cache-Control": "no-store"})

    async def probe(self, node):
        nonce = secrets.token_hex(16)
        start = time.perf_counter()
        try:
            async with self.http.get(node["public_url"] + "/v1/federation/health", params={"nonce": nonce}, allow_redirects=False, timeout=ClientTimeout(total=3)) as response:
                result = await self.read_response(response)
                proof = hmac.digest(node["token_hash"].encode(), nonce.encode(), "sha256").hex()
                valid = response.status == 200 and result.get("node_id") == node["id"] and result.get("hub_url") == self.config.public_url.rstrip("/") and hmac.compare_digest(result.get("proof", ""), proof)
        except Exception:
            return False
        if valid:
            async with self.store.transaction(invalidate=False) as db:
                await db.execute("UPDATE federation_nodes SET last_verified=?,latency_ms=? WHERE id=?", (time.time(), round((time.perf_counter() - start) * 1000, 2), node["id"]))
        return valid

    async def discovery(self, request):
        self.app["api"].rate(request, "node-discovery", 120)
        if self.config.federation_role == "mirror":
            async with self.http.get(self.hub + "/v1/federation/nodes", allow_redirects=False) as response:
                data = await self.read_response(response)
                if response.status != 200:
                    return web.json_response(dict(code="hub_unavailable"), status=503)
            self.public_origins = {self.hub} | {item["base_url"] for item in data["nodes"]}
            return web.json_response(data, headers={"Cache-Control": "public, max-age=15"})
        rows = await self.store.rows("SELECT id,public_url,name,region,latency_ms,stats FROM federation_nodes WHERE state='active' AND last_seen>? AND last_verified>? ORDER BY public_url LIMIT 100", (time.time() - 90, time.time() - 120))
        hint = request.query.get("region", "")[:64]
        rows.sort(key=lambda row: (row["region"] != hint, json.loads(row["stats"]).get("requests", 0), row["public_url"]))
        nodes = [dict(id=row["id"], base_url=row["public_url"], name=row["name"], region=row["region"], role="mirror", read_only=True) for row in rows[:6]]
        nodes.append(dict(id="hub", base_url=self.hub, name="主服务", region="origin", role="hub", read_only=False))
        return web.json_response(dict(protocol="translation-mirror-v1", hub_url=self.hub, expires_in=30, selection="client_rtt", nodes=nodes), headers={"Cache-Control": "public, max-age=15"})

    async def admin(self, request):
        from .api import ApiError
        self.app["api"].require_admin(request)
        if request.method == "POST":
            data = await request.json()
            if not isinstance(data, dict) or set(data) != {"id", "state"} or data["state"] not in ("active", "revoked"):
                raise ApiError("invalid_request")
            node = await self.store.one("SELECT * FROM federation_nodes WHERE id=?", (data["id"],))
            if not node:
                raise ApiError("node_not_found", 404)
            if data["state"] == "active" and not await self.probe(node):
                raise ApiError("node_verification_failed", 409)
            async with self.store.transaction(invalidate=False) as db:
                await db.execute("UPDATE federation_nodes SET state=? WHERE id=?", (data["state"], node["id"]))
            await self.refresh_origins()
        rows = await self.store.rows("SELECT id,public_url,name,region,contact,state,created,last_seen,last_verified,latency_ms,stats FROM federation_nodes ORDER BY created DESC LIMIT 100")
        for row in rows:
            row["stats"] = json.loads(row["stats"])
            row["online"] = row["state"] == "active" and row["last_seen"] > time.time() - 90 and row["last_verified"] > time.time() - 120
        return web.json_response(dict(role=self.config.federation_role, hub_url=self.hub, local_state=self.state if self.config.federation_role == "mirror" else "active", local_error=self.status_error, nodes=rows))

    @staticmethod
    def mirror_read(path, method):
        return (method == "POST" and path == "/v1/translations:resolve") or (method in ("GET", "HEAD") and (path.startswith("/v1/artifacts/") or path in ("/v1/capabilities", "/v1/catalog/projects", "/v1/catalog/search") or path.startswith("/v1/projects/") and path.rsplit("/", 1)[-1] in ("manifest", "source", "history", "detail")))

    async def proxy(self, request):
        from .api import ApiError
        self.app["api"].rate(request, "mirror-read", 1200)
        if not self.credentials or time.monotonic() >= self.active_until:
            raise ApiError("node_not_active", 503, 30)
        payload = None
        ids = None
        if request.method == "POST":
            data = await self.app["api"].body(request, "ResolveRequest")
            ids = [item["request_id"] for item in data["items"]]
            payload = dict(data, items=[dict(item, request_id=str(index)) for index, item in enumerate(data["items"])])
        path = request.path_qs
        identity = hashlib.sha256(path.encode() + (canonical(payload) if payload else b"")).digest()
        entry = self.cache.get(identity)
        hit = bool(entry and entry[0] > time.monotonic())
        if hit:
            self.cache.move_to_end(identity)
            body, content_type = entry[1], entry[2]
        else:
            # Coalesce identical cold requests without holding the SQLite writer.
            if len(self.inflight) >= 256 and identity not in self.inflight:
                raise ApiError("node_busy", 503, 1)
            task = self.inflight.get(identity)
            if not task:
                task = asyncio.create_task(self.fetch(path, payload))
                self.inflight[identity] = task
                def finished(done):
                    self.inflight.pop(identity, None)
                    if not done.cancelled():
                        done.exception()
                task.add_done_callback(finished)
            body, content_type, disk_hit = await asyncio.shield(task)
            hit = hit or disk_hit
            if not path.startswith("/v1/artifacts/"):
                old = self.cache.pop(identity, None)
                if old:
                    self.cache_bytes -= len(old[1])
                self.cache[identity] = (time.monotonic() + 2, body, content_type)
                self.cache_bytes += len(body)
                while len(self.cache) > 2048 or self.cache_bytes > 32 * 1024 * 1024:
                    _, removed = self.cache.popitem(last=False)
                    self.cache_bytes -= len(removed[1])
        if ids is not None:
            result = json.loads(body)
            for index, item in enumerate(result["results"]):
                item["request_id"] = ids[index]
            values = [field for item in result["results"] for field in item["fields"]]
            request["traffic_fields"] = dict(fields=len(values), ready_fields=sum(value["state"] == "ready" for value in values), missing_fields=sum(value["state"] == "missing" for value in values), pending_fields=sum(value["state"] not in ("ready", "missing") for value in values), stale_fields=sum(value["is_current"] is False for value in values))
            body = canonical(result)
        request["traffic_cache_hit"] = hit
        headers = {"X-Translation-Node": self.credentials["id"], "X-Translation-Cache": "hit" if hit else "miss"}
        if path.startswith("/v1/artifacts/"):
            headers.update({"Cache-Control": "public, max-age=31536000, immutable", "ETag": '"' + path.rsplit("/", 1)[-1] + '"'})
            if request.headers.get("If-None-Match") == headers["ETag"]:
                return web.Response(status=304, headers=headers)
        return web.Response(body=body, content_type=content_type, headers=headers)

    async def fetch(self, path, payload):
        from .api import ApiError
        artifact = path.startswith("/v1/artifacts/")
        filename = None
        if artifact:
            match = re.fullmatch(r"/v1/artifacts/([a-f0-9]{64})", path)
            if not match:
                raise ApiError("invalid_request")
            filename = self.artifacts / match[1]
            if filename.name in self.disk_index:
                try:
                    body = await asyncio.to_thread(filename.read_bytes)
                except FileNotFoundError:
                    body = b""
                if hashlib.sha256(body).hexdigest() == filename.name:
                    self.disk_index.move_to_end(filename.name)
                    return body, "application/json", True
                await asyncio.to_thread(filename.unlink, missing_ok=True)
                self.disk_bytes -= self.disk_index.pop(filename.name, 0)
        try:
            async with self.http.request("POST" if payload else "GET", self.hub + path, json=payload, headers={"Authorization": "Bearer " + self.credentials["token"]}, allow_redirects=False) as response:
                body = await self.read_bytes(response)
                if response.status != 200:
                    try:
                        result = json.loads(body)
                    except ValueError:
                        result = {}
                    raise ApiError(result.get("code", "hub_unavailable"), response.status if 400 <= response.status < 500 else 503, 2)
                if not artifact:
                    result = json.loads(body)
        except ApiError:
            raise
        except Exception:
            raise ApiError("hub_unavailable", 503, 2)
        if artifact:
            if hashlib.sha256(body).hexdigest() != filename.name:
                raise ApiError("artifact_integrity_failed", 502)
            async with self.artifact_lock:
                temporary = filename.with_suffix(".tmp")
                await asyncio.to_thread(temporary.write_bytes, body)
                await asyncio.to_thread(os.replace, temporary, filename)
                self.disk_bytes -= self.disk_index.pop(filename.name, 0)
                self.disk_index[filename.name] = len(body)
                self.disk_bytes += len(body)
                await self.trim_artifacts()
            return body, "application/json", False
        def rewrite(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    if key == "url" and isinstance(item, str) and item.startswith(self.hub + "/v1/artifacts/"):
                        value[key] = self.config.public_url.rstrip("/") + item[len(self.hub):]
                    else:
                        rewrite(item)
            elif isinstance(value, list):
                for item in value:
                    rewrite(item)
        rewrite(result)
        return canonical(result), "application/json", False

    def scan_artifacts(self):
        files = sorted((path for path in self.artifacts.iterdir() if re.fullmatch("[a-f0-9]{64}", path.name)), key=lambda path: path.stat().st_mtime)
        return OrderedDict((path.name, path.stat().st_size) for path in files)

    async def trim_artifacts(self):
        while self.disk_bytes > self.config.federation_cache_mb * 1024 * 1024 and self.disk_index:
            name, size = self.disk_index.popitem(last=False)
            self.disk_bytes -= size
            await asyncio.to_thread((self.artifacts / name).unlink, missing_ok=True)
