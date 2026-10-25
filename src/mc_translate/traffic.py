"""Bounded, aggregated service telemetry; no per-request database writes.

Only public translation API traffic is measured. Personal addresses, user agents,
tokens and supplied installation IDs never enter the telemetry tables. A daily
HMAC identifies installations; clients without an installation ID are explicitly
counted as estimates. Counters flush every five seconds and on clean shutdown.
"""

import asyncio
import bisect
import hashlib
import hmac
import logging
import secrets
import time
from collections import Counter
from datetime import datetime, timedelta

from .store import LOCAL_TZ, billing_period, utc

LOG = logging.getLogger(__name__)
BUCKETS_MS = (1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 5000)
COUNTERS = (
    "requests", "errors", "throttled", "response_bytes", "duration_us",
    "fields", "ready_fields", "missing_fields", "pending_fields", "stale_fields",
    "cache_hits", "cache_misses",
) + tuple("latency_" + str(i) for i in range(len(BUCKETS_MS) + 1))
ENDPOINTS = {
    "/v1/translations:resolve": "resolve",
    "/v1/translations:ensure": "ensure",
    "/v1/catalog/projects": "catalogue",
    "/v1/catalog/search": "catalogue",
    "/v1/events": "events",
}


def endpoint(path):
    if path in ENDPOINTS:
        return ENDPOINTS[path]
    if path.startswith("/v1/artifacts/"):
        return "artifact"
    if path.startswith("/v1/jobs/"):
        return "job"
    if path.startswith("/v1/projects/"):
        suffix = path.rsplit("/", 1)[-1]
        return suffix if suffix in ("manifest", "source", "refresh", "history") else None
    return None


def latency(counters):
    total = counters.get("requests", 0)
    result = {"samples": total, "average_ms": round(counters.get("duration_us", 0) / max(1, total) / 1000, 3)}
    for label, fraction in (("p50_le_ms", .5), ("p95_le_ms", .95), ("p99_le_ms", .99)):
        cumulative = 0
        result[label] = None
        for i, bound in enumerate(BUCKETS_MS):
            cumulative += counters.get("latency_" + str(i), 0)
            if total and cumulative >= total * fraction:
                result[label] = bound
                break
    result["within_20ms_ratio"] = round(sum(counters.get("latency_" + str(i), 0) for i in range(5)) / total, 4) if total else None
    return result


class Traffic:
    interval = 5
    retention_days = 90
    max_buckets = 4096
    max_visitors = 50000

    def __init__(self, store):
        self.store = store
        self.pending = {}
        self.visitors = {}
        self.flush_lock = asyncio.Lock()
        self.task = None
        self.stopping = asyncio.Event()
        self.last_flush = None
        self.last_error = False
        self.pruned_day = None
        self.dropped_visitors = 0
        self.started_at = time.time()

    async def open(self):
        columns = ",".join(name + " INTEGER NOT NULL DEFAULT 0" for name in COUNTERS)
        await self.store.db.executescript(f"""
            CREATE TABLE IF NOT EXISTS traffic_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS traffic_hourly(
                hour INTEGER NOT NULL,endpoint TEXT NOT NULL,channel TEXT NOT NULL,
                client_id TEXT NOT NULL,{columns},
                PRIMARY KEY(hour,endpoint,channel,client_id));
            CREATE TABLE IF NOT EXISTS traffic_visitors(
                day TEXT NOT NULL,channel TEXT NOT NULL,identity TEXT NOT NULL,
                reported INTEGER NOT NULL,last_seen REAL NOT NULL,
                PRIMARY KEY(day,channel,identity));
        """)
        async with self.store.transaction(invalidate=False) as db:
            await db.execute("INSERT OR IGNORE INTO traffic_meta VALUES('salt',?)", (secrets.token_hex(32),))
            await db.execute("INSERT OR IGNORE INTO traffic_meta VALUES('started_at',?)", (str(self.started_at),))
        # On :memory: the reader is the writer; elsewhere the commit above is visible.
        row = await self.store.one("SELECT value FROM traffic_meta WHERE key='salt'")
        self.salt = bytes.fromhex(row["value"])
        row = await self.store.one("SELECT value FROM traffic_meta WHERE key='started_at'")
        self.started_at = float(row["value"])
        return self

    def record(self, request, response, duration_ms, category, stamp=None):
        stamp = time.time() if stamp is None else stamp
        client = request.get("client_key")
        client_id = client["id"] if client else ""
        agent = request.headers.get("User-Agent", "")[:256]
        instance = request.headers.get("X-Translation-Instance", "")
        valid_instance = 16 <= len(instance) <= 128 and all(c.isascii() and (c.isalnum() or c in "-_") for c in instance)
        channel = "node_origin" if request.get("federation_node") else "launcher" if client or "launcher" in agent.lower() or valid_instance else "web" if request.headers.get("Sec-Fetch-Site") else "api"
        key = (int(stamp // 3600) * 3600, category, channel, client_id)
        if key not in self.pending and len(self.pending) >= self.max_buckets:
            key = (key[0], "other", "api", "")
        counters = self.pending.setdefault(key, Counter())
        counters["requests"] += 1
        counters["errors"] += response.status >= 400
        counters["throttled"] += response.status == 429
        counters["response_bytes"] += response.content_length or 0
        counters["duration_us"] += max(0, round(duration_ms * 1000))
        counters["latency_" + str(bisect.bisect_left(BUCKETS_MS, duration_ms))] += 1
        if response.status < 400:
            counters.update(request.get("traffic_fields", {}))
            if "traffic_cache_hit" in request:
                counters["cache_hits" if request["traffic_cache_hit"] else "cache_misses"] += 1
            day = billing_period(stamp)[0]
            # Include application namespace so two launchers cannot share identities.
            raw = (day + "|" + client_id + "|" + (instance if valid_instance else request.app["api"].ip(request) + "|" + agent)).encode()
            identity = hmac.digest(self.salt, raw, hashlib.sha256).hex()
            visitor = (day, channel, identity)
            if visitor in self.visitors or len(self.visitors) < self.max_visitors:
                self.visitors[visitor] = (int(valid_instance), stamp)
            else:
                self.dropped_visitors += 1

    async def start(self):
        self.task = asyncio.create_task(self.run())

    async def run(self):
        while not self.stopping.is_set():
            try:
                await asyncio.wait_for(self.stopping.wait(), timeout=self.interval)
            except asyncio.TimeoutError:
                await self.flush()

    async def flush(self):
        async with self.flush_lock:
            pending, visitors = self.pending, self.visitors
            self.pending, self.visitors = {}, {}
            try:
                # Avoid invalidating translation response caches for analytics writes.
                async with self.store.transaction(invalidate=False) as db:
                    if pending:
                        names = ",".join(COUNTERS)
                        updates = ",".join(f"{n}={n}+excluded.{n}" for n in COUNTERS)
                        await db.executemany(
                            f"INSERT INTO traffic_hourly(hour,endpoint,channel,client_id,{names}) VALUES({','.join('?' for _ in range(4 + len(COUNTERS)))}) ON CONFLICT(hour,endpoint,channel,client_id) DO UPDATE SET {updates}",
                            [(*key, *(counters[name] for name in COUNTERS)) for key, counters in pending.items()],
                        )
                    if visitors:
                        await db.executemany(
                            "INSERT INTO traffic_visitors VALUES(?,?,?,?,?) ON CONFLICT(day,channel,identity) DO UPDATE SET reported=MAX(reported,excluded.reported),last_seen=MAX(last_seen,excluded.last_seen)",
                            [(*key, *value) for key, value in visitors.items()],
                        )
                    day = billing_period()[0]
                    if self.pruned_day != day:
                        cutoff = datetime.now(LOCAL_TZ).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=self.retention_days - 1)
                        await db.execute("DELETE FROM traffic_hourly WHERE hour<?", (cutoff.timestamp(),))
                        await db.execute("DELETE FROM traffic_visitors WHERE day<?", (cutoff.strftime("%Y-%m-%d"),))
                        self.pruned_day = day
                self.last_flush = time.time()
                self.last_error = False
            except Exception as exc:
                # Retry bounded aggregates later. Analytics failure must not fail reads.
                for key, value in pending.items():
                    target = key if key in self.pending or len(self.pending) < self.max_buckets else (key[0], "other", "api", "")
                    self.pending.setdefault(target, Counter()).update(value)
                for key, value in visitors.items():
                    if key in self.visitors or len(self.visitors) < self.max_visitors:
                        current = self.visitors.get(key, (0, 0))
                        self.visitors[key] = (max(current[0], value[0]), max(current[1], value[1]))
                    else:
                        self.dropped_visitors += 1
                self.last_error = True
                LOG.warning("Traffic aggregate flush failed (%s); bounded counters retained for retry", type(exc).__name__)

    async def close(self):
        if self.task:
            self.stopping.set()
            await self.task
        await self.flush()

    async def snapshot(self, days=7):
        await self.flush()
        today = datetime.now(LOCAL_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
        start = today - timedelta(days=days - 1)
        day = today.strftime("%Y-%m-%d")
        sums = ",".join(f"COALESCE(SUM({name}),0) AS {name}" for name in COUNTERS)
        summary = await self.store.one(f"SELECT {sums} FROM traffic_hourly WHERE hour>=?", (today.timestamp(),))
        identities = await self.store.rows("SELECT channel,reported,COUNT(*) AS count FROM traffic_visitors WHERE day=? GROUP BY channel,reported", (day,))
        visitors = {(row["channel"], row["reported"]): row["count"] for row in identities}
        reported = visitors.get(("launcher", 1), 0)
        estimated = visitors.get(("launcher", 0), 0)
        active = await self.store.one("SELECT COUNT(DISTINCT client_id) AS count FROM traffic_hourly WHERE hour>=? AND client_id<>''", (today.timestamp(),))
        hourly = await self.store.rows(f"SELECT hour,{sums} FROM traffic_hourly WHERE hour>=? GROUP BY hour ORDER BY hour", (today.timestamp(),))
        daily = await self.store.rows(f"SELECT strftime('%Y-%m-%d',hour,'unixepoch','+8 hours') AS day,{sums} FROM traffic_hourly WHERE hour>=? GROUP BY day ORDER BY day", (start.timestamp(),))
        endpoints = await self.store.rows(f"SELECT endpoint,{sums} FROM traffic_hourly WHERE hour>=? GROUP BY endpoint ORDER BY requests DESC", (today.timestamp(),))
        clients = await self.store.rows("SELECT t.client_id,COALESCE(c.name,'已移除的接入应用') AS name,SUM(t.requests) AS requests,SUM(t.errors) AS errors,SUM(t.fields) AS fields,SUM(t.ready_fields) AS ready_fields FROM traffic_hourly t LEFT JOIN client_keys c ON c.id=t.client_id WHERE t.hour>=? AND t.client_id<>'' GROUP BY t.client_id ORDER BY requests DESC,t.client_id LIMIT 10", (today.timestamp(),))
        model = await self.store.one("SELECT COUNT(*) AS calls,COALESCE(SUM(status='succeeded'),0) AS succeeded,COALESCE(SUM(status='failed'),0) AS failed,COALESCE(SUM(tokens),0) AS tokens FROM usage WHERE day=?", (day,))
        def report(row):
            return {**{key: value for key, value in row.items() if key != "duration_us" and not key.startswith("latency_")}, "latency": latency(row)}
        return dict(
            day=day, timezone="Asia/Shanghai", days=days, generated_at=utc(),
            collection_started_at=utc(self.started_at), last_flushed_at=utc(self.last_flush) if self.last_flush else None,
            aggregation=dict(interval_seconds=self.interval, retention_days=self.retention_days, degraded=self.last_error, omitted_visitor_events=self.dropped_visitors),
            today={**report(summary), "launcher_instances": reported + estimated, "reported_instances": reported, "estimated_instances": estimated, "active_integrations": active["count"], "web_visitors_estimated": sum(value for (channel, _), value in visitors.items() if channel == "web"), "api_visitors_estimated": sum(value for (channel, _), value in visitors.items() if channel == "api"), "field_hit_ratio": round(summary["ready_fields"] / summary["fields"], 4) if summary["fields"] else None},
            hourly=[report(row) for row in hourly], daily=[report(row) for row in daily],
            endpoints=[report(row) for row in endpoints], top_clients=clients, model=model,
        )
