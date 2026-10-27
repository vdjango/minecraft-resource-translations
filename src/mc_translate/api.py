import asyncio
import base64
import hashlib
import hmac
import json
import math
import re
import secrets
import time
from collections import OrderedDict
from dataclasses import replace
from pathlib import Path

from aiohttp import ClientError, ClientSession, ClientTimeout, TCPConnector, web
from jsonschema import Draft202012Validator

from .config import Config
from .engine import TERMINAL, Engine
from .federation import Federation
from .hashing import canonical, digest, resource_key, resource_ref
from .languages import DEFAULT_LANGUAGE, LANGUAGES, language_options, normalize_language
from .mail import MailService, enqueue
from .management import Settings, key_hash
from .model import OpenAIModel
from .sources import Sources, read_json
from .store import POLICY, Store, billing_period, utc
from .traffic import Traffic, endpoint as traffic_endpoint

ROOT = Path(__file__).parent
CONTRACT = json.loads((ROOT / "contracts/openapi.json").read_text(encoding="utf-8"))
VALIDATORS = {
    name: Draft202012Validator(
        {"$ref": "#/components/schemas/" + name, "components": CONTRACT["components"]}
    )
    for name in (
        "ResolveRequest",
        "EnsureRequest",
        "AnonymousSessionRequest",
        "FeedbackRequest",
    )
}


class ApiError(Exception):
    def __init__(self, code, status=400, retry=0):
        self.code, self.status, self.retry = code, status, retry


def error(code, retry=0):
    return dict(
        code=code,
        message=code,
        retryable=bool(retry),
        request_id=secrets.token_hex(8),
        **({"retry_after_seconds": retry} if retry else {}),
    )


@web.middleware
async def observe(request, handler):
    category = traffic_endpoint(request.path) if request.method != "OPTIONS" else None
    if not category:
        return await handler(request)
    start = time.perf_counter()
    try:
        response = await handler(request)
    except Exception:
        request.app["traffic"].record(request, web.Response(status=500), (time.perf_counter() - start) * 1000, category)
        raise
    elapsed = (time.perf_counter() - start) * 1000
    if not response.prepared:
        response.headers["Server-Timing"] = f"app;dur={elapsed:.3f}"
        response.headers["Timing-Allow-Origin"] = "*"
    request.app["traffic"].record(request, response, elapsed, category)
    return response


@web.middleware
async def boundary(request, handler):
    try:
        token = request.headers.get("Authorization", "").removeprefix("Bearer ")
        federation = request.app["federation"]
        if federation.config.federation_role == "mirror" and Federation.mirror_read(request.path, request.method):
            response = await federation.proxy(request)
        else:
            response = None
        if response is None and token.startswith("bcn_") and not request.path.startswith("/v1/federation/"):
            node = await federation.node_auth(request)
            if node["state"] != "active":
                raise ApiError("node_not_active", 403)
            request["federation_node"] = node["id"]
        if response is None and token.startswith("bct_") and not request.path.startswith("/v1/admin/"):
            client = await request.app["store"].one(
                "SELECT * FROM client_keys WHERE hash=?", (key_hash(token),)
            )
            if not client:
                raise ApiError("invalid_client_key", 401)
            request["client_key"] = client
            if (
                request.path != "/v1/clients/application"
                and client["state"] != "active"
            ):
                raise ApiError("client_key_" + client["state"], 403)
            request.app["api"].client_rate(client)
        if response is None:
            if federation.config.federation_role == "mirror" and request.path.startswith("/v1/") and not request.path.startswith(("/v1/admin/", "/v1/federation/")):
                return web.json_response(dict(code="hub_required", hub_url=federation.hub), status=409, headers={"Access-Control-Allow-Origin": "*"})
            response = await handler(request)
    except ApiError as exc:
        response = web.json_response(error(exc.code, exc.retry), status=exc.status)
        if exc.retry:
            response.headers["Retry-After"] = str(exc.retry)
    except web.HTTPException as exc:
        response = web.json_response(
            error("request_too_large" if exc.status == 413 else "not_found"),
            status=exc.status,
        )
    except (ValueError, TypeError, KeyError):
        response = web.json_response(error("invalid_request"), status=400)
    response.headers.setdefault("Cache-Control", "no-store")
    if request.path.startswith("/assets/") and request.path.endswith((".js", ".css", ".png", ".svg", ".ico")):
        response.headers["Cache-Control"] = "public, max-age=0, must-revalidate"
        if re.fullmatch(r"/assets/vue-runtime-[A-Za-z0-9_-]+\.js", request.path):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    if request.path == "/developers" or request.path == "/v1/clients/applications":
        response.set_cookie("translation_device", request.app["api"].device(request), max_age=31536000, httponly=True, secure=request.app["api"].config.public_url.startswith("https:"), samesite="Lax")
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = (
        "Authorization, Content-Type, Idempotency-Key, Last-Event-ID, X-Translation-Instance"
    )
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
    response.headers["X-Content-Type-Options"] = "nosniff"
    if request.path.startswith(("/admin", "/v1/")) or request.path.startswith("/assets/") and request.path.endswith(".html"):
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self' " + " ".join(sorted(request.app["federation"].public_origins)) + "; img-src 'self' https:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
    )
    return response


class API:
    def __init__(self, app, config, store, engine):
        self.app, self.config, self.store, self.engine = app, config, store, engine
        self.cache, self.rates = OrderedDict(), OrderedDict()
        self.cache_bytes = 0
        self.lookup_cache = OrderedDict()
        self.lookup_cache_bytes = 0
        self.validated_resolves = OrderedDict()
        self.ensure_lock = asyncio.Lock()
        self.settings = Settings(config)
        self.event_streams = 0
        self.model_lists = OrderedDict()
        self.admin_sessions = {}
        self.cursor_key = hashlib.sha256(
            (config.admin_token or secrets.token_hex(32)).encode()
        ).digest()

    def page_cursor(self, key, filters):
        raw = canonical(dict(key=key, filters=filters, expires=int(time.time()) + 900))
        signature = hmac.digest(self.cursor_key, raw, "sha256")
        return base64.urlsafe_b64encode(raw + signature).decode().rstrip("=")

    def read_cursor(self, value, filters):
        if not value:
            return ""
        try:
            if len(value) > 2048:
                raise ValueError()
            signed = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
            raw, signature = signed[:-32], signed[-32:]
            if not hmac.compare_digest(
                hmac.digest(self.cursor_key, raw, "sha256"), signature
            ):
                raise ValueError()
            data = json.loads(raw)
            if data["expires"] < time.time() or data["filters"] != filters:
                raise ValueError()
            return data["key"]
        except (ValueError, KeyError, TypeError):
            raise ApiError("cursor_expired", 409)

    def ip(self, request):
        # Trust forwarding headers only from our local reverse proxy.
        if self.config.trusted_proxy and request.remote in ("127.0.0.1", "::1"):
            return request.headers.get("X-Real-IP", request.remote)[:64]
        return request.remote or "unknown"

    def rate(self, request, category, limit=600, seconds=60):
        if request.get("federation_node") and category == "read":
            limit = 12000
        key = (self.ip(request), category, int(time.time()) // seconds)
        count = self.rates.get(key, 0) + 1
        self.rates[key] = count
        self.rates.move_to_end(key)
        while len(self.rates) > 20000:
            self.rates.popitem(last=False)
        if count > limit:
            raise ApiError("rate_limited", 429, seconds)

    async def owner(self, request):
        if request.get("client_key"):
            return "client:" + request["client_key"]["id"]
        token = request.headers.get("Authorization", "")
        owner = (
            await self.store.owner(token[7:])
            if token.startswith("Bearer ") and len(token) < 256
            else None
        )
        if not owner:
            raise ApiError("unauthorized", 401)
        return owner

    def client_rate(self, client):
        key = ("client:" + client["id"], "requests", int(time.time()) // 60)
        self.rates[key] = self.rates.get(key, 0) + 1
        self.rates.move_to_end(key)
        while len(self.rates) > 20000:
            self.rates.popitem(last=False)
        if self.rates[key] > client["requests_per_minute"]:
            raise ApiError("client_rate_limited", 429, 60)

    def require_admin(self, request):
        self.rate(request, "admin", 60)
        token = request.headers.get("Authorization", "").removeprefix("Bearer ")
        session = self.admin_sessions.get(key_hash(request.cookies.get("translation_admin", "")))
        cookie_auth = session and session[0] > time.time() and session[1] == key_hash(self.config.admin_token)
        if not self.config.admin_token or not (hmac.compare_digest(token, self.config.admin_token) or cookie_auth):
            raise ApiError("unauthorized", 401)
        if cookie_auth and request.method == "POST":
            origin = request.headers.get("Origin")
            if origin and origin not in (self.config.public_url.rstrip("/"), request.scheme + "://" + request.host):
                raise ApiError("invalid_origin", 403)

    async def admin_session(self, request):
        if request.method == "GET":
            self.require_admin(request)
            return web.json_response(dict(authenticated=True))
        if request.method == "DELETE":
            self.admin_sessions.pop(key_hash(request.cookies.get("translation_admin", "")), None)
            response = web.json_response(dict(authenticated=False))
            response.del_cookie("translation_admin", path="/v1/admin")
            return response
        self.require_admin(request)
        response = web.json_response(dict(authenticated=True))
        if request.method == "POST":
            sid = secrets.token_urlsafe(32)
            self.admin_sessions = {k:v for k,v in self.admin_sessions.items() if v[0] > time.time()}
            if len(self.admin_sessions) >= 100:
                self.admin_sessions.pop(next(iter(self.admin_sessions)))
            self.admin_sessions[key_hash(sid)] = (time.time() + 3600, key_hash(self.config.admin_token))
            response.set_cookie("translation_admin", sid, max_age=3600, path="/v1/admin", httponly=True, secure=self.config.public_url.startswith("https:"), samesite="Strict")
        return response

    def device(self, request):
        cookie = request.cookies.get("translation_device", "")
        value, _, signature = cookie.partition(".")
        if re.fullmatch(r"[a-f0-9]{32}", value) and hmac.compare_digest(hmac.new(self.cursor_key, value.encode(), "sha256").hexdigest(), signature):
            return cookie
        value = secrets.token_hex(16) if request.method == "GET" else "0" * 32
        return value + "." + hmac.new(self.cursor_key, value.encode(), "sha256").hexdigest()

    async def admin_settings(self, request):
        self.require_admin(request)
        if request.method == "POST":
            try:
                changes = await request.json()
                resume_queue = bool(set(changes) & {"daily_token_limit", "monthly_token_limit", "daily_call_limit"})
                self.settings.apply(changes)
                if resume_queue:
                    async with self.store.transaction() as db:
                        await db.execute("DELETE FROM budget_pause")
                        await db.execute("UPDATE tasks SET next_attempt=0 WHERE state='deferred' AND json_extract(error,'$.code')='budget_exhausted'")
                    self.engine.wake.set()
                self.app["mail"].wake.set()
            except ValueError:
                raise ApiError("invalid_settings")
        return web.json_response(self.settings.public())

    async def admin_mail(self, request):
        self.require_admin(request)
        if request.method == "POST":
            data = await request.json()
            if data != {"action": "test"}:
                raise ApiError("invalid_request")
            if not self.config.mail_enabled:
                raise ApiError("mail_disabled", 409)
            async with self.store.transaction() as db:
                identity = await enqueue(
                    db, "test:" + secrets.token_hex(12), {"test": True}
                )
            self.app["mail"].wake.set()
            return web.json_response(dict(id=identity, state="pending"), status=202)
        return web.json_response(
            dict(
                enabled=self.config.mail_enabled,
                counts=await self.store.rows(
                    "SELECT state,count(*) AS count FROM mail_outbox GROUP BY state"
                ),
                messages=await self.store.rows(
                    "SELECT id,state,attempt,created,updated,error,sent_at FROM mail_outbox ORDER BY created DESC LIMIT 20"
                ),
            )
        )

    async def admin_models(self, request):
        self.require_admin(request)
        changes = await request.json() if request.method == "POST" else {}
        if not isinstance(changes, dict) or set(changes) - {"base_url", "api_key"}:
            raise ApiError("invalid_request")
        candidate = replace(self.config, database=":memory:")
        try:
            if changes:
                Settings(candidate).apply(changes, persist=False)
        except (ValueError, TypeError):
            raise ApiError("invalid_settings")
        if candidate.base_url.rstrip("/") != self.config.base_url.rstrip(
            "/"
        ) and not changes.get("api_key"):
            raise ApiError("model_key_required")
        if not candidate.api_key:
            raise ApiError("model_not_configured", 409)
        identity = key_hash(candidate.base_url + "\n" + candidate.api_key)
        cached = self.model_lists.get(identity)
        if cached and cached[0] > time.monotonic():
            return web.json_response(dict(models=cached[1]))
        try:
            async with self.app["http"].get(
                candidate.base_url.rstrip("/") + "/models",
                headers={"Authorization": "Bearer " + candidate.api_key},
                allow_redirects=False,
                timeout=ClientTimeout(total=15, connect=5),
            ) as response:
                if response.status != 200:
                    raise ApiError("model_list_unavailable", 502, 30)
                data = await read_json(response, 1048576)
            models = sorted(
                {
                    item["id"]
                    for item in data["data"]
                    if isinstance(item, dict)
                    and isinstance(item.get("id"), str)
                    and 0 < len(item["id"]) <= 128
                    and not any(c.isspace() for c in item["id"])
                }
            )
            if not models:
                raise ValueError()
        except (ClientError, TimeoutError, ValueError, KeyError, TypeError):
            raise ApiError("model_list_unavailable", 502, 30)
        self.model_lists[identity] = (time.monotonic() + 60, models)
        while len(self.model_lists) > 4:
            self.model_lists.popitem(last=False)
        return web.json_response(dict(models=models))

    @staticmethod
    def public_client(row):
        result = {
            name: row[name]
            for name in (
                "id",
                "prefix",
                "name",
                "contact",
                "website",
                "purpose",
                "state",
                "fields_per_hour",
                "requests_per_minute",
                "created",
                "updated",
            )
        }
        result["notice"] = {
            "pending": "申请正在等待管理员审核；审核通过后才可调用翻译接口。",
            "active": "密钥已启用，可以按下方额度接入。",
            "rejected": "申请已驳回，如需接入请重新提交。",
            "revoked": "密钥已撤销，请联系平台管理员。",
        }.get(result["state"], "")
        return result

    async def client_apply(self, request):
        self.rate(request, "applications", 30, 3600)
        day, _ = billing_period()
        bucket = key_hash(self.ip(request) + ":" + self.device(request))
        async with self.store.transaction() as db:
            row = await (await db.execute("SELECT count FROM application_attempts WHERE device=? AND day=?", (bucket, day))).fetchone()
            if row and row[0] >= 3:
                from datetime import datetime, timedelta

                from .store import LOCAL_TZ
                reset = (datetime.now(LOCAL_TZ) + timedelta(days=1)).replace(hour=0,minute=0,second=0,microsecond=0).timestamp()
                raise ApiError("application_daily_limit", 429, max(1, int(reset-time.time())))
            await db.execute("INSERT INTO application_attempts VALUES(?,?,1) ON CONFLICT(device,day) DO UPDATE SET count=count+1", (bucket, day))
            await db.execute("DELETE FROM application_attempts WHERE day<?", (billing_period(time.time()-7*86400)[0],))
        data = await request.json()
        if not isinstance(data, dict) or set(data) - {"name", "contact", "website", "purpose"} or not {"name", "contact", "purpose"} <= set(data):
            raise ApiError("invalid_request")
        data.setdefault("website", "")
        for name, maximum in (
            ("name", 100),
            ("contact", 200),
            ("website", 500),
            ("purpose", 1000),
        ):
            if (
                not isinstance(data[name], str)
                or (name != "website" and not data[name].strip())
                or len(data[name]) > maximum
            ):
                raise ApiError("invalid_request")
        token, identity, stamp = (
            "bct_" + secrets.token_urlsafe(32),
            "client_" + secrets.token_hex(12),
            time.time(),
        )
        async with self.store.transaction() as db:
            await db.execute(
                "INSERT INTO client_keys(id,hash,prefix,name,contact,website,purpose,created,updated) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    identity,
                    key_hash(token),
                    token[:12],
                    data["name"],
                    data["contact"],
                    data["website"],
                    data["purpose"],
                    stamp,
                    stamp,
                ),
            )
            await enqueue(
                db, "application:" + identity, dict(data, application_id=identity)
            )
        self.app["mail"].wake.set()
        return web.json_response(
            dict(
                application_id=identity,
                access_token=token,
                state="pending",
                notice="密钥仅显示一次，请保存；管理员批准后启用。",
            ),
            status=201,
        )

    async def client_status(self, request):
        client = request.get("client_key")
        if not client:
            raise ApiError("unauthorized", 401)
        result = self.public_client(client)
        if result["state"] != "active":
            for field in ("fields_per_hour", "requests_per_minute", "fields_used_this_hour"):
                result.pop(field, None)
        return web.json_response(result)

    async def admin_clients(self, request):
        self.require_admin(request)
        if request.method == "POST":
            data = await request.json()
            if (
                not isinstance(data, dict)
                or not {"id", "state"} <= set(data)
                or set(data) - {"id", "state", "fields_per_hour", "requests_per_minute"}
            ):
                raise ApiError("invalid_request")
            if data["state"] not in ("active", "rejected", "revoked"):
                raise ApiError("invalid_request")
            client = await self.store.one(
                "SELECT * FROM client_keys WHERE id=?", (data["id"],)
            )
            if not client:
                raise ApiError("client_not_found", 404)
            for name, maximum in (
                ("fields_per_hour", 100000),
                ("requests_per_minute", 10000),
            ):
                value = data.get(name, client[name])
                if type(value) is not int or not 1 <= value <= maximum:
                    raise ApiError("invalid_request")
            async with self.store.transaction() as db:
                await db.execute(
                    "UPDATE client_keys SET state=?,fields_per_hour=?,requests_per_minute=?,updated=? WHERE id=?",
                    (
                        data["state"],
                        data.get("fields_per_hour", client["fields_per_hour"]),
                        data.get("requests_per_minute", client["requests_per_minute"]),
                        time.time(),
                        data["id"],
                    ),
                )
        cursor = request.query.get("after", "")
        rows = await self.store.rows(
            "SELECT * FROM client_keys WHERE id>? ORDER BY id LIMIT 51", (cursor,)
        )
        clients = []
        for row in rows[:50]:
            usage = await self.store.one(
                "SELECT count FROM quotas WHERE bucket=? AND window=?",
                ("ensure-owner:client:" + row["id"], int(time.time()) // 3600),
            )
            clients.append(
                dict(
                    self.public_client(row),
                    fields_used_this_hour=usage["count"] if usage else 0,
                )
            )
        return web.json_response(
            dict(
                clients=clients, next_cursor=rows[49]["id"] if len(rows) > 50 else None
            )
        )

    async def body(self, request, schema):
        try:
            data = await request.json()
        except (ValueError, UnicodeError):
            raise ApiError("invalid_json")
        if isinstance(data, dict) and "items" in data:
            data.setdefault("target_language", DEFAULT_LANGUAGE)
        validation_key = None
        # Correlation IDs change every launch. Reuse validation only for the
        # exact previously validated document shape, independently validating
        # every ID on every request. Unknown fields/types remain in the key.
        if schema == "ResolveRequest" and isinstance(data, dict) and isinstance(data.get("items"), list):
            items = data["items"]
            if all(isinstance(item, dict) and isinstance(item.get("request_id"), str) and 1 <= len(item["request_id"]) <= 64 for item in items):
                validation_key = hashlib.sha256(canonical(dict(data, items=[dict(item, request_id=None) for item in items]))).digest()
        valid = validation_key in self.validated_resolves if validation_key else False
        if not valid and not VALIDATORS[schema].is_valid(data):
            raise ApiError("invalid_request")
        if validation_key:
            self.validated_resolves[validation_key] = True
            self.validated_resolves.move_to_end(validation_key)
            while len(self.validated_resolves) > 1024:
                self.validated_resolves.popitem(last=False)
        if "items" in data:
            try:
                data["target_language"] = normalize_language(data["target_language"])
            except ValueError:
                raise ApiError("unsupported_language_or_profile")
            if data["profile"] != "minecraft-v1":
                raise ApiError("unsupported_language_or_profile")
            if len({i["request_id"] for i in data["items"]}) != len(data["items"]):
                raise ApiError("duplicate_request_id")
            for item in data["items"]:
                resource_key(item["resource"])
                if len({f["field"] for f in item["fields"]}) != len(
                    item["fields"]
                ) or any(f["field"] == "changelog" for f in item["fields"]):
                    raise ApiError("unsupported_or_duplicate_field")
        return data

    async def capabilities(self, request):
        return web.json_response(
            dict(
                api_version="1.0",
                hash_schemas=["mc-text-v1", "mc-snapshot-v1"],
                providers=["modrinth", "curseforge"],
                target_languages=list(LANGUAGES),
                default_target_language=DEFAULT_LANGUAGE,
                languages=language_options(),
                profiles=["minecraft-v1"],
                generation_available=bool(self.config.api_key),
                limits=dict(
                    resolve_items=50,
                    ensure_items=20,
                    fields_per_item=3,
                    request_bytes=65536,
                    event_tickets=50,
                ),
            ),
            headers={"Cache-Control": "public, max-age=60"},
        )

    async def session(self, request):
        self.rate(request, "session", 10, 3600)
        await self.body(request, "AnonymousSessionRequest")
        if not await self.store.quota("session:" + self.ip(request), 10):
            raise ApiError("rate_limited", 429, 3600)
        token = await self.store.session(self.ip(request))
        return web.json_response(
            dict(
                access_token=token,
                token_type="Bearer",
                expires_in=3600,
                scopes=["translations:ensure", "jobs:read", "feedback:write"],
            )
        )

    def translation(self, row, field):
        value = dict(
            format="plain"
            if field in ("title", "summary")
            else row.get("format", "markdown"),
            translation_revision=row["revision"],
            policy_revision=row["policy"],
            quality=row["quality"],
            translation_required=row["quality"] != "original",
            generated_at=utc(row["generated"]),
        )
        if row["quality"] == "original":
            value["no_translation_reason"] = row.get("no_translation_reason") or ("project_name" if field == "title" else "unchanged")
        if field != "body":
            value["text"] = row["text"]
        value["artifact"] = dict(
            artifact_sha256=row["artifact"],
            url=self.config.public_url.rstrip("/") + "/v1/artifacts/" + row["artifact"],
        )
        return value

    async def field_result(
        self, resource, field, language, stale=False, source=None, loaded=None
    ):
        key = resource_key(resource)
        source = source if source is not None else await self.store.source(key)
        current = (
            source["fields"].get(field["field"], {}).get("source_hash")
            if source
            else None
        )
        matches = current == field["source_hash"] if current else None
        context = self.store.context(source)
        identity = (key, field["field"], field["source_hash"])
        candidates = loaded[0].get(identity, []) if loaded else []
        row = (
            next((r for r in candidates if r["context"] == context), None)
            if loaded
            else await self.store.translation(
                key, field["field"], field["source_hash"], language, context
            )
        )
        # Historical output is usable for its own requested hash, even after title changes.
        if not row and matches is False:
            row = (
                (candidates[0] if candidates else None)
                if loaded
                else await self.store.one(
                    "SELECT * FROM translations WHERE key=? AND field=? AND source_hash=? AND language=? AND policy=? ORDER BY generated DESC LIMIT 1",
                    (key, field["field"], field["source_hash"], language, POLICY),
                )
            )
        result = dict(
            field=field["field"],
            requested_source_hash=field["source_hash"],
            current_source_hash=current,
            state="missing",
            matches_request=False,
            is_current=matches,
            freshness="unknown"
            if not source
            else "fresh"
            if time.time() - source["checked"] < self.config.source_interval
            else "stale",
            source_checked_at=utc(source["checked"]) if source else None,
        )
        if row:
            result.update(
                state="ready",
                matches_request=True,
                translation=self.translation(row, field["field"]),
            )
        else:
            task = (
                loaded[1].get(identity)
                if loaded
                else await self.store.pending(
                    key, field["field"], field["source_hash"], language
                )
            )
            if task:
                state = task["state"]
                result["state"] = (
                    "running"
                    if state == "verifying"
                    else "missing"
                    if state in ("source_mismatch", "ready")
                    else state
                )
                if state == "failed":
                    # A read never starts paid work. Released launchers stop at
                    # `failed`, so expose the missing output with an ensure action.
                    result["state"] = "missing"
                    result["last_failure"] = dict(
                        code=json.loads(task["error"])["code"] if task["error"] else "internal_error",
                        failed_at=utc(task["updated"]),
                        retry_after_seconds=max(0, math.ceil(task["updated"] + 60 - time.time())),
                    )
                # Segment progress is on tickets; no incomplete document is marked ready.
                if state == "partial":
                    result["state"] = "running"
                if task["error"]:
                    result["error"] = error(
                        json.loads(task["error"])["code"],
                        max(1, result["last_failure"]["retry_after_seconds"])
                        if "last_failure" in result
                        else max(0, int(task["next_attempt"] - time.time())),
                    )
                elif state == "source_mismatch":
                    result["error"] = error("source_mismatch")
            if stale:
                fallback = await self.store.one(
                    "SELECT * FROM translations WHERE key=? AND field=? AND language=? AND policy=? AND source_hash<>? ORDER BY generated DESC LIMIT 1",
                    (key, field["field"], language, POLICY, field["source_hash"]),
                )
                if fallback:
                    result.update(
                        fallback_translation=self.translation(fallback, field["field"]),
                        fallback_source_hash=fallback["source_hash"],
                    )
        previous = (
            (key, field["field"]) in loaded[2]
            if loaded
            else bool(
                await self.store.one(
                    "SELECT 1 FROM translations WHERE key=? AND field=? AND language=? LIMIT 1",
                    (key, field["field"], language),
                )
            )
        )
        current_identity = (key, field["field"], current)
        current_task = (
            loaded[1].get(current_identity)
            if loaded
            else await self.store.pending(key, field["field"], current, language)
        )
        current_state = current_task["state"] if current_task else "missing"
        if matches and row:
            current_state = "ready"
        elif loaded and any(
            r["context"] == context for r in loaded[0].get(current_identity, [])
        ):
            current_state = "ready"
        changed = matches is False or (not row and previous)
        if not changed:
            code = "up_to_date" if row else "translation_pending"
            notice = "" if row else "正在准备译文，暂时显示原文"
            if row and row["quality"] == "original":
                code, notice = "not_required", "无需翻译，保留原文"
        elif current_state == "ready":
            code, notice = "source_updated", "原文已有更新，请刷新项目以查看最新译文"
        elif current_state in ("running", "partial", "verifying"):
            code, notice = "update_running", "原文已更新，正在翻译新版本"
        elif current_state in ("queued", "deferred"):
            code, notice = "update_queued", "原文已更新，新版本译文正在排队"
        elif current_state == "failed":
            code, notice = "update_failed", "原文已更新，新版翻译暂时失败，可先查看原文"
        else:
            code, notice = "update_pending", "原文已更新，等待新版本翻译"
        result["update"] = dict(
            code=code,
            source_changed=changed,
            previous_translation_available=previous,
            current_translation_state=current_state,
            notice=notice,
            action="refresh_source"
            if matches is False
            else "none"
            if row
            else "ensure",
        )
        if "last_failure" in result and matches is not False:
            result["update"]["notice"] = "上次翻译失败，再次访问将自动重试，暂时显示原文"
        return result

    async def resolve(self, request):
        self.rate(request, "read", 1200)
        raw = await request.read()
        key = hashlib.sha256(raw).digest()
        cached = self.cache.get(key)
        if (
            cached
            and cached[0] == self.store.version
            and time.monotonic() - cached[1] < 2
        ):
            self.cache.move_to_end(key)
            request["traffic_fields"] = cached[3]
            request["traffic_cache_hit"] = True
            return web.Response(body=cached[2], content_type="application/json")
        data = await self.body(request, "ResolveRequest")
        version = self.store.version
        cached_fields = {}
        needed = []
        for item in data["items"]:
            missing = []
            for field in item["fields"]:
                identity = (resource_key(item["resource"]), field["field"], field["source_hash"])
                cache_key = (data["target_language"], data.get("allow_stale", False), *identity)
                entry = self.lookup_cache.get(cache_key)
                if entry and entry[0] == version and time.monotonic() - entry[1] < 2:
                    cached_fields[identity] = entry[2]
                    self.lookup_cache.move_to_end(cache_key)
                else:
                    missing.append(field)
            if missing:
                needed.append(dict(item, fields=missing))
        request["traffic_cache_hit"] = not needed
        results = []
        keys = list({resource_key(item["resource"]) for item in needed})
        source_rows = await self.store.rows(
            "SELECT r.key,l.data AS snapshot,r.checked FROM resource_lookup l JOIN resources r ON r.key=l.key WHERE r.key IN ("
            + ",".join("?" for _ in keys)
            + ")",
            keys,
        ) if keys else []
        sources = {
            r["key"]: dict(json.loads(r["snapshot"]), checked=r["checked"])
            for r in source_rows
        }
        identities = list(
            {
                (resource_key(i["resource"]), f["field"], f["source_hash"])
                for i in needed
                for f in i["fields"]
            }
        )
        identities = list(
            set(identities)
            | {
                (
                    resource_key(i["resource"]),
                    f["field"],
                    sources[resource_key(i["resource"])]["fields"][f["field"]][
                        "source_hash"
                    ],
                )
                for i in needed
                for f in i["fields"]
                if resource_key(i["resource"]) in sources
                and f["field"] in sources[resource_key(i["resource"])]["fields"]
            }
        )
        predicate = (
            "(key,field,source_hash) IN ("
            + ",".join("(?,?,?)" for _ in identities)
            + ")"
        )
        params = [v for identity in identities for v in identity] + [
            data["target_language"],
            POLICY,
        ]
        rows = await self.store.rows(
            "SELECT key,field,source_hash,language,context,policy,revision,quality,format,no_translation_reason,CASE WHEN field='body' THEN '' ELSE text END AS text,artifact,generated FROM translations WHERE "
            + predicate
            + " AND language=? AND policy=? ORDER BY generated DESC",
            params,
        ) if identities else []
        translations = {}
        for row in rows:
            translations.setdefault(
                (row["key"], row["field"], row["source_hash"]), []
            ).append(row)
        rows = await self.store.rows(
            "SELECT * FROM tasks WHERE "
            + predicate
            + " AND language=? ORDER BY created DESC",
            params[:-1],
        ) if identities else []
        pending = {}
        for row in rows:
            pending.setdefault((row["key"], row["field"], row["source_hash"]), row)
        previous = {
            (r["key"], r["field"])
            for r in (await self.store.rows(
                "SELECT key,field FROM translations WHERE key IN ("
                + ",".join("?" for _ in keys)
                + ") AND language=? GROUP BY key,field",
                keys + [data["target_language"]],
            ) if keys else [])
        }
        for item in data["items"]:
            source = sources.get(resource_key(item["resource"]), {})
            fields = []
            for field in item["fields"]:
                identity = (resource_key(item["resource"]), field["field"], field["source_hash"])
                value = cached_fields.get(identity)
                if value is None:
                    value = await self.field_result(
                        item["resource"],
                        field,
                        data["target_language"],
                        data.get("allow_stale", False),
                        source,
                        (translations, pending, previous),
                    )
                    # A worker may publish while the read awaits SQLite. Never
                    # tag a mixed snapshot with the worker's newer version.
                    if version == self.store.version:
                        cache_key = (data["target_language"], data.get("allow_stale", False), *identity)
                        size = len(canonical(value))
                        old_field = self.lookup_cache.pop(cache_key, None)
                        if old_field:
                            self.lookup_cache_bytes -= old_field[3]
                        self.lookup_cache[cache_key] = (version, time.monotonic(), value, size)
                        self.lookup_cache_bytes += size
                        while len(self.lookup_cache) > 8192 or self.lookup_cache_bytes > 16 * 1024 * 1024:
                            _, removed_field = self.lookup_cache.popitem(last=False)
                            self.lookup_cache_bytes -= removed_field[3]
                fields.append(value)
            results.append(
                dict(
                    request_id=item["request_id"],
                    resource=item["resource"],
                    fields=fields,
                )
            )
        body = canonical(dict(request_id=secrets.token_hex(8), results=results))
        measured_fields = [field for result in results for field in result["fields"]]
        counts = dict(fields=len(measured_fields), ready_fields=sum(field["state"] == "ready" for field in measured_fields), missing_fields=sum(field["state"] == "missing" for field in measured_fields), pending_fields=sum(field["state"] not in ("ready", "missing") for field in measured_fields), stale_fields=sum(field["is_current"] is False for field in measured_fields))
        request["traffic_fields"] = counts
        old = self.cache.pop(key, None)
        if old:
            self.cache_bytes -= len(old[2])
        self.cache[key] = (version, time.monotonic(), body, counts)
        self.cache_bytes += len(body)
        while len(self.cache) > 1024 or self.cache_bytes > 16 * 1024 * 1024:
            _, removed = self.cache.popitem(last=False)
            self.cache_bytes -= len(removed[2])
        return web.Response(body=body, content_type="application/json")

    def ticket_json(self, task):
        value = dict(
            job_id=task["job_id"],
            resource=json.loads(task["ref"]),
            field=task["field"],
            requested_source_hash=task["source_hash"],
            state=task["state"],
            current_source_hash=task["current_hash"],
            completed_segments=task["completed"],
            total_segments=task["total"],
            created_at=utc(task["created"]),
            updated_at=utc(task["updated"]),
        )
        if task["state"] == "deferred":
            value["retry_after_seconds"] = max(
                1, int(task["next_attempt"] - time.time())
            )
        if task["error"] or task["state"] == "source_mismatch":
            value["error"] = error(
                json.loads(task["error"])["code"]
                if task["error"]
                else "source_mismatch"
            )
        return value

    async def ensure(self, request):
        self.rate(request, "ensure", 60)
        owner = await self.owner(request)
        data = await self.body(request, "EnsureRequest")
        identity = request.headers.get("Idempotency-Key", "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", identity):
            raise ApiError("idempotency_key_required")
        body_hash = digest(data)
        async with self.ensure_lock:
            prior = await self.store.one(
                "SELECT * FROM idempotency WHERE owner=? AND key=? AND expires>?",
                (owner, identity, time.time()),
            )
            if prior:
                if prior["body_hash"] != body_hash:
                    raise ApiError("idempotency_conflict", 409)
                return web.Response(
                    text=prior["response"],
                    content_type="application/json",
                    status=prior["status"],
                )
            count = sum(len(item["fields"]) for item in data["items"])
            client = request.get("client_key")
            if not await self.store.quota(
                "ensure-ip:" + self.ip(request),
                max(2000, self.config.session_fields_per_hour)
                if client
                else self.config.session_fields_per_hour,
                count,
            ) or not await self.store.quota(
                "ensure-owner:" + owner,
                client["fields_per_hour"]
                if client
                else self.config.session_fields_per_hour,
                count,
            ):
                raise ApiError("generation_quota_exceeded", 429, 3600)
            results = []
            for item in data["items"]:
                existing, tickets = [], []
                for field in item["fields"]:
                    found = await self.field_result(
                        item["resource"], field, data["target_language"]
                    )
                    if found["state"] == "ready":
                        existing.append(found)
                    else:
                        try:
                            job = await self.engine.enqueue(
                                item["resource"],
                                field["field"],
                                field["source_hash"],
                                data["target_language"],
                                data["purpose"],
                                owner,
                            )
                        except OverflowError:
                            raise ApiError("queue_full", 503, 30)
                        tickets.append(
                            self.ticket_json(await self.store.ticket(job, owner))
                        )
                results.append(
                    dict(
                        request_id=item["request_id"],
                        existing=existing,
                        tickets=tickets,
                    )
                )
            response = json.dumps(
                dict(request_id=secrets.token_hex(8), results=results),
                ensure_ascii=False,
            )
            async with self.store.transaction() as db:
                await db.execute(
                    "INSERT OR REPLACE INTO idempotency VALUES(?,?,?,?,?,?)",
                    (owner, identity, body_hash, response, 202, time.time() + 86400),
                )
            return web.Response(
                text=response, status=202, content_type="application/json"
            )

    async def job(self, request):
        self.rate(request, "read")
        owner = await self.owner(request)
        task = await self.store.ticket(request.match_info["job_id"], owner)
        if not task:
            raise ApiError("job_not_found", 404)
        return web.json_response(self.ticket_json(task))

    async def artifact(self, request):
        self.rate(request, "artifact", 1200)
        identity = request.match_info["artifact_sha256"]
        if not re.fullmatch(r"[0-9a-f]{64}", identity):
            raise ApiError("artifact_not_found", 404)
        row = await self.store.one(
            "SELECT data FROM artifacts WHERE hash=?", (identity,)
        )
        if not row:
            raise ApiError("artifact_not_found", 404)
        headers = {
            "ETag": '"' + identity + '"',
            "Cache-Control": "public, max-age=31536000, immutable",
        }
        if request.headers.get("If-None-Match") == headers["ETag"]:
            return web.Response(status=304, headers=headers)
        return web.Response(
            body=row["data"], content_type="application/json", headers=headers
        )

    async def manifest(self, request):
        self.rate(request, "read")
        try:
            language = normalize_language(request.query.get("target_language", DEFAULT_LANGUAGE))
        except ValueError:
            raise ApiError("unsupported_language_or_profile")
        resource = resource_ref(
            request.match_info["provider"], request.match_info["project_id"]
        )
        source = await self.store.source(resource_key(resource))
        if not source:
            raise ApiError("project_not_indexed", 404)
        fields = []
        for name, value in source["fields"].items():
            result = await self.field_result(
                resource,
                dict(field=name, source_hash=value["source_hash"]),
                language,
                source=source,
            )
            fields.append(
                dict(
                    field=name,
                    source_hash=value["source_hash"],
                    format=value["format"],
                    source_language=value.get("source_language", "und"),
                    state=result["state"],
                    translation_required=result.get("translation", {}).get("translation_required", True),
                    no_translation_reason=result.get("translation", {}).get("no_translation_reason"),
                )
            )
        value = dict(
            resource=resource,
            resource_id=resource["provider"] + "_" + resource["project_id"],
            kind=source["kind"],
            **source["metadata"],
            snapshot_hash=source["snapshot_hash"],
            metadata_hash=source["metadata_hash"],
            fields_complete=True,
            fields=fields,
            metadata_checked_at=utc(source["checked"]),
            body_checked_at=utc(source["checked"]),
            freshness="fresh"
            if time.time() - source["checked"] < self.config.source_interval
            else "stale",
            next_check_at=utc(source["checked"] + self.config.source_interval),
            withdrawn=False,
        )
        value.pop("slug", None)
        return web.json_response(value)

    async def refresh(self, request):
        self.rate(request, "import", 10, 3600)
        owner = await self.owner(request)
        if not await self.store.quota("import:" + self.ip(request), 10):
            raise ApiError("rate_limited", 429, 3600)
        resource = resource_ref(
            request.match_info["provider"], request.match_info["project_id"]
        )
        resource_key(resource)
        # Short coalescing window permits later explicit update checks.
        stamp = int(time.time()) // 30
        job = await self.engine.enqueue(
            resource, "__refresh__", digest(str(stamp)), "zh-Hans", "detail", owner
        )
        return web.json_response(dict(job_id=job, state="queued"), status=202)

    async def catalogue(self, request):
        self.rate(request, "read")
        limit = min(50, max(1, int(request.query.get("limit", "20"))))
        query = request.query.get("q", "").lower()[:200]
        kind = request.query.get("kind", "")
        try:
            language = normalize_language(request.query.get("target_language", DEFAULT_LANGUAGE))
        except ValueError:
            raise ApiError("unsupported_language_or_profile")
        if kind and kind not in ("mod", "resourcepack", "shader", "modpack"):
            raise ApiError("invalid_request")
        filters = dict(q=query, kind=kind, limit=limit, target_language=language)
        cursor = self.read_cursor(request.query.get("cursor", ""), filters)
        params = [cursor]
        where = "c.key>?"
        if kind:
            where += " AND c.kind=?"
            params.append(kind)
        if len(query) >= 3:
            where += " AND c.key IN (SELECT key FROM catalogue_fts WHERE catalogue_fts MATCH ? UNION SELECT key FROM localized_catalogue_fts WHERE language=? AND localized_catalogue_fts MATCH ?)"
            phrase = '"' + query.replace('"', '""') + '"'
            params.extend([phrase, language, phrase])
        elif query:
            where += " AND (c.title LIKE ? ESCAPE '\\' OR c.summary LIKE ? ESCAPE '\\' OR c.key IN (SELECT key FROM localized_catalogue WHERE language=? AND text LIKE ? ESCAPE '\\'))"
            pattern = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            params.extend([pattern, pattern, language, pattern])
        rows = await self.store.rows(
            "SELECT c.*,r.ref,m.data AS metadata,l.data AS lookup FROM catalogue c JOIN resources r ON r.key=c.key LEFT JOIN project_metadata m ON m.key=c.key LEFT JOIN resource_lookup l ON l.key=c.key WHERE "
            + where
            + " ORDER BY c.key LIMIT ?",
            params + [limit + 1],
        )
        total = await self.store.one(
            "SELECT COUNT(*) AS count FROM catalogue c WHERE "
            + where.replace("c.key>?", "1=1", 1),
            params[1:],
        )
        page_keys = [r["key"] for r in rows[:limit]]
        previous = set()
        pending = {}
        ready = set()
        contexts = {
            r["key"]: self.store.context(json.loads(r["lookup"]))
            for r in rows[:limit]
            if r["lookup"]
        }
        if page_keys:
            marks = ",".join("?" for _ in page_keys)
            previous = {
                (r["key"], r["field"])
                for r in await self.store.rows(
                    "SELECT key,field FROM translations WHERE key IN ("
                    + marks
                    + ") AND field IN ('summary','body') AND language=? GROUP BY key,field",
                    page_keys + [language],
                )
            }
            tasks = await self.store.rows(
                "SELECT t.key,t.field,t.state,t.context FROM tasks t JOIN resource_lookup l ON l.key=t.key AND json_extract(l.data,'$.fields.'||t.field||'.source_hash')=t.source_hash WHERE t.key IN ("
                + marks
                + ") AND t.field IN ('summary','body') AND t.language=? ORDER BY t.created DESC",
                page_keys + [language],
            )
            for t in tasks:
                if t["context"] == contexts.get(t["key"]):
                    pending.setdefault((t["key"], t["field"]), t["state"])
            translations = await self.store.rows(
                "SELECT t.key,t.field,t.context,t.quality,t.no_translation_reason,CASE WHEN t.field='summary' THEN t.text ELSE '' END AS text FROM translations t JOIN resource_lookup l ON l.key=t.key AND json_extract(l.data,'$.fields.'||t.field||'.source_hash')=t.source_hash WHERE t.key IN ("
                + marks
                + ") AND t.field IN ('summary','body') AND t.language=? AND t.policy=?",
                page_keys + [language, POLICY],
            )
            ready = {
                (t["key"], t["field"]): t
                for t in translations
                if t["context"] == contexts.get(t["key"])
            }
        projects, last = [], None
        for row in rows[:limit]:
            from .hashing import text_hash

            field_states = {
                name: "ready"
                if (row["key"], name) in ready
                else pending.get((row["key"], name), "missing")
                for name in ("summary", "body")
            }
            field_states = {
                name: "missing"
                if state in ("ready", "source_mismatch")
                and (row["key"], name) not in ready
                else "running"
                if state in ("verifying", "partial")
                else state
                for name, state in field_states.items()
            }
            full_ready = all(state == "ready" for state in field_states.values())
            aggregate = (
                "ready"
                if full_ready
                else "partial"
                if "ready" in field_states.values()
                else next(
                    (
                        state
                        for state in ("running", "queued", "deferred", "failed")
                        if state in field_states.values()
                    ),
                    "missing",
                )
            )
            value = dict(
                resource=json.loads(row["ref"]),
                kind=row["kind"],
                title=row["title"],
                summary=row["summary"],
                source_url=row["source_url"],
                title_source_hash=text_hash(row["title"], "title", "plain"),
                summary_source_hash=row["summary_hash"],
                metadata=json.loads(row["metadata"]) if row["metadata"] else {},
                translation_state=aggregate,
                field_states=field_states,
                target_language=language,
            )
            if field_states["summary"] == "ready":
                localized = ready[(row["key"], "summary")]
                value.update(
                    localized_summary=localized["text"],
                    translation_quality=localized["quality"],
                )
            changed = any(
                state != "ready" and (row["key"], name) in previous
                for name, state in field_states.items()
            )
            state = (
                field_states["body"]
                if field_states["body"] != "ready"
                else field_states["summary"]
            )
            code = "up_to_date" if full_ready else "translation_pending"
            notice = (
                ""
                if full_ready
                else "简介译文已就绪；正文译文正在准备"
                if field_states["summary"] == "ready"
                else "正在准备译文，暂时显示原文"
            )
            if full_ready and all(ready[(row["key"], name)]["quality"] == "original" for name in field_states):
                code, notice = "not_required", "无需翻译，保留原文"
            if changed:
                code = (
                    "update_running"
                    if state in ("running", "partial", "verifying")
                    else "update_queued"
                    if state in ("queued", "deferred")
                    else "update_failed"
                    if state == "failed"
                    else "update_pending"
                )
                notice = {
                    "update_running": "原文已更新，正在翻译新版本",
                    "update_queued": "原文已更新，新版本译文正在排队",
                    "update_failed": "原文已更新，新版翻译暂时失败",
                    "update_pending": "原文已更新，等待新版本翻译",
                }[code]
            value["update"] = dict(
                code=code,
                source_changed=changed,
                previous_translation_available=any(
                    (row["key"], name) in previous for name in field_states
                ),
                current_translation_state=aggregate,
                notice=notice,
                action="none" if full_ready else "ensure",
            )
            projects.append(value)
            last = row["key"]
        return web.json_response(
            dict(
                request_id=secrets.token_hex(8),
                projects=projects,
                next_cursor=self.page_cursor(last, filters)
                if len(rows) > limit
                else None,
                total_count=total["count"],
                target_language=language,
            )
        )

    async def source_text(self, request):
        self.rate(request, "read")
        resource = resource_ref(
            request.match_info["provider"], request.match_info["project_id"]
        )
        source = await self.store.source(resource_key(resource))
        if not source:
            raise ApiError("project_not_indexed", 404)
        return web.json_response(
            {k: v for k, v in source.items() if k not in ("checked", "accessed")}
        )

    async def detail_data(self, resource, language):
        """Read source and usable translations together, without upstream/model I/O."""
        key = resource_key(resource)
        for _ in range(3):
            version = self.store.version
            source = await self.store.source(key)
            if not source:
                raise ApiError("project_not_indexed", 404)
            fields = []
            for name in ("summary", "body"):
                value = await self.field_result(resource, dict(field=name, source_hash=source["fields"][name]["source_hash"]), language, source=source)
                if value["state"] == "ready":
                    row = await self.store.translation(key, name, source["fields"][name]["source_hash"], language, self.store.context(source))
                    if not row:
                        break
                    value["translation"]["text"] = row["text"]
                fields.append(value)
            if version == self.store.version and len(fields) == 2:
                return dict(source={k: v for k, v in source.items() if k not in ("checked", "accessed")}, fields=fields, target_language=language)
        raise ApiError("project_updating", 503, 1)

    async def detail(self, request):
        self.rate(request, "read")
        language = normalize_language(request.query.get("target_language", DEFAULT_LANGUAGE))
        value = await self.detail_data(resource_ref(request.match_info["provider"], request.match_info["project_id"]), language)
        response = web.json_response(value)
        response.enable_compression()
        return response

    async def history(self, request):
        self.rate(request, "read")
        resource = resource_ref(
            request.match_info["provider"], request.match_info["project_id"]
        )
        key = resource_key(resource)
        cursor = int(request.query.get("cursor", "0"))
        rows = await self.store.rows(
            "SELECT * FROM history WHERE key=? AND id>? ORDER BY id LIMIT 100",
            (key, cursor),
        )
        events = [
            dict(
                event_id=str(r["id"]),
                type=r["type"],
                observed_at=utc(r["observed"]),
                **json.loads(r["data"]),
            )
            for r in rows
        ]
        return web.json_response(
            dict(
                resource=resource,
                events=events,
                next_cursor=str(rows[-1]["id"]) if len(rows) == 100 else None,
            )
        )

    async def feedback(self, request):
        self.rate(request, "feedback", 10, 3600)
        owner = await self.owner(request)
        data = await self.body(request, "FeedbackRequest")
        key = resource_key(data["resource"])
        row = await self.store.one(
            "SELECT revision FROM translations WHERE key=? AND field=? AND source_hash=? AND revision=?",
            (key, data["field"], data["source_hash"], data["translation_revision"]),
        )
        if not row:
            raise ApiError("translation_not_found", 404)
        identity = "fb_" + secrets.token_hex(12)
        async with self.store.transaction() as db:
            await db.execute(
                "INSERT INTO feedback(id,owner,data,created) VALUES(?,?,?,?)",
                (identity, owner, json.dumps(data, ensure_ascii=False), time.time()),
            )
        return web.json_response(
            dict(feedback_id=identity, state="pending_review"), status=202
        )

    async def events(self, request):
        self.rate(request, "events", 30)
        owner = await self.owner(request)
        ids = list(dict.fromkeys(request.query.get("job_ids", "").split(",")))
        if not 1 <= len(ids) <= 50 or self.event_streams >= 100:
            raise ApiError("event_limit", 429, 10)
        for identity in ids:
            if not await self.store.ticket(identity, owner):
                raise ApiError("job_not_found", 404)
        response = web.StreamResponse(
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-store",
                "X-Accel-Buffering": "no",
                "Access-Control-Allow-Origin": "*",
            }
        )
        await response.prepare(request)
        self.event_streams += 1
        try:
            await response.write(
                b'event: reset\ndata: {"type":"reset","event_id":"snapshot"}\n\n'
            )
            seen = {}
            for tick in range(120):
                terminal = True
                for identity in ids:
                    task = await self.store.ticket(identity, owner)
                    if task["state"] not in TERMINAL:
                        terminal = False
                    marker = (task["state"], task["updated"])
                    if seen.get(identity) != marker:
                        seen[identity] = marker
                        event = dict(
                            event_id=str(task["updated"]),
                            type="job.updated",
                            job_id=identity,
                            state=task["state"],
                        )
                        await response.write(
                            (
                                "id: "
                                + event["event_id"]
                                + "\nevent: job.updated\ndata: "
                                + json.dumps(event)
                                + "\n\n"
                            ).encode()
                        )
                if terminal:
                    break
                if tick % 15 == 0:
                    await response.write(b": keep-alive\n\n")
                await asyncio.sleep(1)
        except (ConnectionResetError, ConnectionError):
            pass
        finally:
            self.event_streams -= 1
        return response

    async def admin(self, request):
        self.require_admin(request)
        if request.method == "POST":
            data = await request.json()
            if set(data) != {"feedback_id", "state"} or data["state"] not in (
                "acknowledged",
                "rejected",
                "resolved",
            ):
                raise ApiError("invalid_request")
            async with self.store.transaction() as db:
                await db.execute(
                    "UPDATE feedback SET state=? WHERE id=?",
                    (data["state"], data["feedback_id"]),
                )
        tasks = await self.store.rows(
            "SELECT state,COUNT(*) AS count FROM tasks GROUP BY state"
        )
        day, month = billing_period()
        usage = await self.store.one(
            "SELECT COALESCE(SUM(tokens),0) AS tokens,COUNT(*) AS calls,SUM(CASE WHEN status='succeeded' THEN 1 ELSE 0 END) AS succeeded,SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed,SUM(CASE WHEN status IN ('pending','unknown') THEN 1 ELSE 0 END) AS unconfirmed,COALESCE(SUM(cost),0) AS cost,SUM(CASE WHEN cost IS NULL THEN 1 ELSE 0 END) AS cost_unknown FROM usage WHERE day=?",
            (day,),
        )
        monthly = await self.store.one("SELECT COALESCE(SUM(tokens),0) AS tokens,COUNT(*) AS calls,COALESCE(SUM(cost),0) AS cost FROM usage WHERE month=?", (month,))
        pause = await self.store.one("SELECT resume_at,reason FROM budget_pause WHERE id=1 AND resume_at>?", (time.time(),))
        feedback = await self.store.rows(
            "SELECT id,data,state,created FROM feedback ORDER BY created DESC LIMIT 50"
        )
        return web.json_response(
            dict(
                tasks=tasks,
                usage=dict(usage, day=day, month=month, monthly=dict(monthly), currency="USD"),
                budgets=dict(
                    daily_tokens=self.config.daily_token_limit,
                    monthly_tokens=self.config.monthly_token_limit,
                    daily_calls=self.config.daily_call_limit,
                ),
                pause=pause,
                feedback=[dict(row, data=json.loads(row["data"])) for row in feedback],
            )
        )

    async def admin_tasks(self, request):
        self.require_admin(request)
        state = request.query.get("state")
        if state not in (None, "", "queued", "running", "verifying", "partial", "deferred", "ready", "failed", "blocked"):
            raise ApiError("invalid_request")
        filters = {"state": state or ""}
        cursor = self.read_cursor(request.query.get("cursor", ""), filters)
        params = []
        where = ""
        if state:
            where = " WHERE state=?"
            params.append(state)
        if cursor:
            where += (" AND " if where else " WHERE ") + "(updated<? OR (updated=? AND id<?))"
            params.extend((cursor["updated"], cursor["updated"], cursor["id"]))
        rows = await self.store.rows("SELECT t.id,t.key,t.field,t.language,t.state,t.attempt,t.completed,t.total,t.created,t.updated,t.next_attempt,t.error,tr.quality AS result_quality,tr.no_translation_reason,COALESCE(c.title,t.key) AS title FROM tasks t LEFT JOIN catalogue c ON c.key=t.key LEFT JOIN translations tr ON tr.key=t.key AND tr.field=t.field AND tr.source_hash=t.source_hash AND tr.language=t.language AND tr.context=t.context AND tr.policy=?" + where.replace("state", "t.state") + " ORDER BY t.updated DESC,t.id DESC LIMIT 101", [POLICY] + params)
        pause = await self.store.one("SELECT resume_at,reason FROM budget_pause WHERE id=1 AND resume_at>?", (time.time(),))
        next_cursor = self.page_cursor({"updated": rows[99]["updated"], "id": rows[99]["id"]}, filters) if len(rows) > 100 else None
        return web.json_response({"tasks": [dict(row, error=json.loads(row["error"]) if row["error"] else None) for row in rows[:100]], "next_cursor":next_cursor, "pause":pause})

    async def admin_retry_task(self, request):
        self.require_admin(request)
        try:
            task = await self.store.retry_task(request.match_info["task_id"], self.config.max_queue)
        except OverflowError:
            raise ApiError("queue_full", 503, 30)
        if not task:
            raise ApiError("task_not_found", 404)
        if task["state"] not in ("queued", "running", "verifying", "partial", "deferred"):
            raise ApiError("task_not_retryable", 409)
        self.engine.wake.set()
        return web.json_response(dict(task_id=task["id"], state=task["state"], retry_after_seconds=max(0, math.ceil(task["next_attempt"] - time.time()))), status=202)

    async def admin_usage(self, request):
        self.require_admin(request)
        day, month = billing_period()
        period = request.query.get("period", "day")
        if period not in ("day", "month"):
            raise ApiError("invalid_request")
        status = request.query.get("status", "")
        if status not in ("", "succeeded", "failed", "pending", "unknown"):
            raise ApiError("invalid_request")
        key = month if period == "month" else day
        column = "month" if period == "month" else "day"
        filters = {"period":period,"key":key,"status":status}
        cursor = self.read_cursor(request.query.get("cursor", ""), filters)
        where = column + "=?"
        params = [key]
        if status:
            where += " AND status=?"
            params.append(status)
        if cursor:
            where += " AND (created<? OR (created=? AND id<?))"
            params.extend((cursor["created"], cursor["created"], cursor["id"]))
        rows = await self.store.rows("SELECT id,model,status,tokens,input_tokens,cached_tokens,output_tokens,cost,currency,created,actual,details_known FROM usage WHERE " + where + " ORDER BY created DESC,id DESC LIMIT 101", params)
        summary = await self.store.one("SELECT COALESCE(SUM(tokens),0) AS tokens,COUNT(*) AS calls,SUM(CASE WHEN status='succeeded' THEN 1 ELSE 0 END) AS succeeded,SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed,SUM(CASE WHEN status IN ('pending','unknown') THEN 1 ELSE 0 END) AS unconfirmed,COALESCE(SUM(cost),0) AS cost,SUM(CASE WHEN cost IS NULL THEN 1 ELSE 0 END) AS cost_unknown FROM usage WHERE " + column + "=?", (key,))
        next_cursor = self.page_cursor({"created":rows[99]["created"],"id":rows[99]["id"]},filters) if len(rows)>100 else None
        return web.json_response({"period":period,"key":key,"calls":[dict(row) for row in rows[:100]],"next_cursor":next_cursor,"summary":summary,"currency":"USD"})

    async def admin_traffic(self, request):
        self.require_admin(request)
        days = int(request.query.get("days", "7"))
        if days not in (1, 7, 30):
            raise ApiError("invalid_request")
        return web.json_response(await self.app["traffic"].snapshot(days))


async def create_app(
    config=None,
    *,
    model_factory=None,
    sources_factory=None,
    workers=True,
    mail_sender=None,
):
    config = config or Config.from_env()
    app = web.Application(middlewares=[observe, boundary], client_max_size=65536)
    store = await Store(config.database).open()
    traffic = await Traffic(store).open()
    app["traffic"] = traffic
    federation = await Federation(app, config, store).open()
    app["federation"] = federation
    session = ClientSession(
        connector=TCPConnector(limit=24, limit_per_host=8),
        headers={
            "User-Agent": "Buggcraft-Translate/0.1 (Minecraft resource translation)"
        },
    )
    sources = (sources_factory or Sources)(config, session, store)
    model = (model_factory or OpenAIModel)(config, session, store)
    engine = Engine(config, store, sources, model)
    api = API(app, config, store, engine)
    mail = MailService(
        config, store, **({"sender": mail_sender} if mail_sender else {})
    )
    app["mail"] = mail
    app["api"] = api
    app["engine"] = engine
    app["store"] = store
    app["http"] = session
    from .public_pages import PublicPages

    public_pages = PublicPages(api, ROOT / "web")
    app.router.add_get("/v1/capabilities", api.capabilities)
    app.router.add_post("/v1/sessions/anonymous", api.session)
    app.router.add_post("/v1/translations:resolve", api.resolve)
    app.router.add_post("/v1/translations:ensure", api.ensure)
    app.router.add_get("/v1/jobs/{job_id}", api.job)
    app.router.add_get("/v1/events", api.events)
    app.router.add_get("/v1/artifacts/{artifact_sha256}", api.artifact)
    base = "/v1/projects/{provider}/{project_id}"
    app.router.add_get(base + "/manifest", api.manifest)
    app.router.add_get(base + "/source", api.source_text)
    app.router.add_get(base + "/detail", api.detail)
    app.router.add_post(base + "/refresh", api.refresh)
    app.router.add_get(base + "/history", api.history)
    app.router.add_get("/v1/catalog/projects", api.catalogue)
    app.router.add_get("/v1/catalog/search", api.catalogue)
    app.router.add_post("/v1/feedback", api.feedback)
    app.router.add_get("/v1/admin/overview", api.admin)
    app.router.add_post("/v1/admin/overview", api.admin)
    app.router.add_get("/v1/admin/tasks", api.admin_tasks)
    app.router.add_post("/v1/admin/tasks/{task_id}/retry", api.admin_retry_task)
    app.router.add_get("/v1/admin/usage", api.admin_usage)
    app.router.add_get("/v1/admin/traffic", api.admin_traffic)
    app.router.add_get("/v1/admin/nodes", federation.admin)
    app.router.add_post("/v1/admin/nodes", federation.admin)
    app.router.add_get("/v1/federation/nodes", federation.discovery)
    app.router.add_post("/v1/federation/nodes", federation.registration)
    app.router.add_post("/v1/federation/heartbeat", federation.heartbeat)
    app.router.add_get("/v1/federation/health", federation.health)
    app.router.add_get("/v1/admin/settings", api.admin_settings)
    app.router.add_post("/v1/admin/settings", api.admin_settings)
    app.router.add_get("/v1/admin/models", api.admin_models)
    app.router.add_post("/v1/admin/models", api.admin_models)
    app.router.add_get("/v1/admin/mail", api.admin_mail)
    app.router.add_post("/v1/admin/mail", api.admin_mail)
    app.router.add_get("/v1/admin/clients", api.admin_clients)
    app.router.add_post("/v1/admin/clients", api.admin_clients)
    app.router.add_post("/v1/admin/session", api.admin_session)
    app.router.add_get("/v1/admin/session", api.admin_session)
    app.router.add_delete("/v1/admin/session", api.admin_session)
    app.router.add_post("/v1/clients/applications", api.client_apply)
    app.router.add_get("/v1/clients/application", api.client_status)
    app.router.add_get(
        "/openapi.json", lambda r: web.FileResponse(ROOT / "contracts/openapi.json")
    )
    app.router.add_get(
        "/hash-vectors.json",
        lambda r: web.FileResponse(ROOT / "contracts/hash-vectors.json"),
    )
    app.router.add_get(
        "/health", lambda r: web.json_response(dict(status="ok", version="0.1.0"))
    )
    app.router.add_route("OPTIONS", "/{path:.*}", lambda r: web.Response(status=204))
    for route, file in (
        ("/", "index.html"),
        ("/docs", "docs.html"),
        ("/admin", "admin.html"),
        ("/admin/model", "admin-model.html"),
        ("/admin/mail", "admin-mail.html"),
        ("/admin/clients", "admin-clients.html"),
        ("/admin/tasks", "admin-tasks.html"),
        ("/admin/usage", "admin-usage.html"),
        ("/admin/traffic", "admin-traffic.html"),
        ("/admin/nodes", "admin-nodes.html"),
        ("/admin/feedback", "admin-feedback.html"),
        ("/developers", "developers.html"),
        ("/network", "network.html"),
    ):

        async def page(request, file=file):
            return await public_pages.page(request, file)

        app.router.add_get(route, page)
    app.router.add_get("/robots.txt", public_pages.robots)
    app.router.add_get("/sitemap.xml", public_pages.sitemap_index)
    app.router.add_get("/sitemaps/pages.xml", public_pages.sitemap_pages)
    app.router.add_get("/sitemaps/projects-{page:\\d+}.xml", public_pages.sitemap_projects)
    app.router.add_get("/projects/{provider}/{project_id}", public_pages.project)
    app.router.add_static("/assets", ROOT / "web", show_index=False)

    async def startup(app):
        await traffic.start()
        await federation.start()
        await mail.start()
        if workers and config.federation_role != "mirror":
            await engine.start()

    async def cleanup(app):
        await mail.close()
        await engine.close()
        await federation.close()
        await session.close()
        await traffic.close()
        await store.close()

    app.on_startup.append(startup)
    app.on_cleanup.append(cleanup)
    return app
