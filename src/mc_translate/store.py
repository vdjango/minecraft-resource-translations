import asyncio
import hashlib
import json
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiosqlite

from .hashing import canonical, digest, resource_key
from .metadata import catalogue_metadata
from .languages import DEFAULT_LANGUAGE

POLICY = "minecraft-zh-v2"
LOCAL_TZ = timezone(timedelta(hours=8))


def billing_period(stamp=None):
    local = datetime.fromtimestamp(time.time() if stamp is None else stamp, LOCAL_TZ)
    return local.strftime("%Y-%m-%d"), local.strftime("%Y-%m")


def utc(stamp=None):
    return (
        datetime.fromtimestamp(time.time() if stamp is None else stamp, timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


class Store:
    """One durable writer and one independent reader, WAL and bounded hot cache.

    The service intentionally runs as one process per SQLite database. Queue
    claims, reservations and output commits are transactional; restart recovers
    in-flight tasks. No model work occurs inside a database transaction.
    """

    def __init__(self, path):
        self.path = path
        self.lock = asyncio.Lock()
        self.version = 0

    async def open(self):
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.db = await aiosqlite.connect(self.path, isolation_level=None)
        self.db.row_factory = aiosqlite.Row
        await self.db.executescript("""
        PRAGMA journal_mode=WAL;
        PRAGMA busy_timeout=5000;
        PRAGMA foreign_keys=ON;
        CREATE TABLE IF NOT EXISTS resources(
          key TEXT PRIMARY KEY, ref TEXT NOT NULL, snapshot TEXT NOT NULL,
          snapshot_hash TEXT NOT NULL, checked REAL NOT NULL, accessed REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS snapshots(
          key TEXT NOT NULL, hash TEXT NOT NULL, data TEXT NOT NULL, observed REAL NOT NULL,
          PRIMARY KEY(key,hash));
        CREATE TABLE IF NOT EXISTS translations(
          key TEXT NOT NULL, field TEXT NOT NULL, source_hash TEXT NOT NULL,
          language TEXT NOT NULL, context TEXT NOT NULL, policy TEXT NOT NULL,
          revision TEXT NOT NULL, quality TEXT NOT NULL, text TEXT NOT NULL,
          artifact TEXT NOT NULL, generated REAL NOT NULL,
          PRIMARY KEY(key,field,source_hash,language,context,policy));
        CREATE TABLE IF NOT EXISTS artifacts(hash TEXT PRIMARY KEY, data BLOB NOT NULL);
        CREATE TABLE IF NOT EXISTS segments(
          hash TEXT NOT NULL, language TEXT NOT NULL, policy TEXT NOT NULL,
          text TEXT NOT NULL, PRIMARY KEY(hash,language,policy));
        CREATE TABLE IF NOT EXISTS tasks(
          id TEXT PRIMARY KEY, dedup TEXT UNIQUE NOT NULL, key TEXT NOT NULL,
          ref TEXT NOT NULL, field TEXT NOT NULL, source_hash TEXT NOT NULL,
          language TEXT NOT NULL, context TEXT NOT NULL, state TEXT NOT NULL,
          purpose TEXT NOT NULL, attempt INTEGER NOT NULL DEFAULT 0,
          completed INTEGER NOT NULL DEFAULT 0, total INTEGER,
          created REAL NOT NULL, updated REAL NOT NULL, next_attempt REAL NOT NULL DEFAULT 0,
          error TEXT, current_hash TEXT, lease TEXT);
        CREATE INDEX IF NOT EXISTS task_queue ON tasks(state,next_attempt,purpose,created);
        CREATE INDEX IF NOT EXISTS task_lookup ON tasks(key,field,source_hash,language,state);
        CREATE TABLE IF NOT EXISTS tickets(
          id TEXT PRIMARY KEY, owner TEXT NOT NULL, task TEXT NOT NULL,
          UNIQUE(owner,task));
        CREATE TABLE IF NOT EXISTS sessions(hash TEXT PRIMARY KEY, expires REAL NOT NULL, ip TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS idempotency(
          owner TEXT NOT NULL, key TEXT NOT NULL, body_hash TEXT NOT NULL,
          response TEXT NOT NULL, status INTEGER NOT NULL, expires REAL NOT NULL,
          PRIMARY KEY(owner,key));
        CREATE TABLE IF NOT EXISTS usage(
          id TEXT PRIMARY KEY, day TEXT NOT NULL, month TEXT NOT NULL,
          tokens INTEGER NOT NULL, actual INTEGER NOT NULL DEFAULT 0,
          created REAL NOT NULL);
        CREATE INDEX IF NOT EXISTS usage_day ON usage(day);
        CREATE INDEX IF NOT EXISTS usage_month ON usage(month);
        CREATE TABLE IF NOT EXISTS budget_pause(id INTEGER PRIMARY KEY CHECK(id=1), resume_at REAL NOT NULL, reason TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS application_attempts(device TEXT NOT NULL, day TEXT NOT NULL, count INTEGER NOT NULL, PRIMARY KEY(device,day));
        CREATE TABLE IF NOT EXISTS quotas(
          bucket TEXT NOT NULL, window INTEGER NOT NULL, count INTEGER NOT NULL,
          PRIMARY KEY(bucket,window));
        CREATE TABLE IF NOT EXISTS history(
          id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT NOT NULL, type TEXT NOT NULL,
          data TEXT NOT NULL, observed REAL NOT NULL);
        CREATE INDEX IF NOT EXISTS history_project ON history(key,id);
        CREATE TABLE IF NOT EXISTS feedback(
          id TEXT PRIMARY KEY, owner TEXT NOT NULL, data TEXT NOT NULL,
          state TEXT NOT NULL DEFAULT 'pending_review', created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS catalogue(
          key TEXT PRIMARY KEY, kind TEXT NOT NULL, title TEXT NOT NULL,
          summary TEXT NOT NULL, localized TEXT, quality TEXT, source_url TEXT NOT NULL,
          summary_hash TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS catalogue_kind ON catalogue(kind,key);
        CREATE TABLE IF NOT EXISTS project_metadata(key TEXT PRIMARY KEY, data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS resource_lookup(key TEXT PRIMARY KEY, data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS client_keys(
          id TEXT PRIMARY KEY, hash TEXT UNIQUE NOT NULL, prefix TEXT NOT NULL,
          name TEXT NOT NULL, contact TEXT NOT NULL, website TEXT NOT NULL, purpose TEXT NOT NULL,
          state TEXT NOT NULL DEFAULT 'pending', fields_per_hour INTEGER NOT NULL DEFAULT 1000,
          requests_per_minute INTEGER NOT NULL DEFAULT 600, created REAL NOT NULL, updated REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS mail_outbox(
          id TEXT PRIMARY KEY, event TEXT UNIQUE NOT NULL, payload TEXT NOT NULL,
          state TEXT NOT NULL DEFAULT 'pending', attempt INTEGER NOT NULL DEFAULT 0,
          next_attempt REAL NOT NULL DEFAULT 0, created REAL NOT NULL, updated REAL NOT NULL,
          error TEXT, sent_at REAL);
        CREATE INDEX IF NOT EXISTS mail_queue ON mail_outbox(state,next_attempt,created);
        CREATE VIRTUAL TABLE IF NOT EXISTS catalogue_fts USING fts5(key UNINDEXED,text,tokenize='trigram');
        """)
        usage_columns = {row[1] for row in await (await self.db.execute("PRAGMA table_info(usage)")).fetchall()}
        for name, declaration in (("model", "TEXT NOT NULL DEFAULT ''"), ("status", "TEXT NOT NULL DEFAULT 'unknown'"), ("input_tokens", "INTEGER NOT NULL DEFAULT 0"), ("cached_tokens", "INTEGER NOT NULL DEFAULT 0"), ("output_tokens", "INTEGER NOT NULL DEFAULT 0"), ("cost", "REAL"), ("currency", "TEXT NOT NULL DEFAULT 'USD'"), ("details_known", "INTEGER NOT NULL DEFAULT 0"), ("price_snapshot", "TEXT")):
            if name not in usage_columns:
                await self.db.execute(f"ALTER TABLE usage ADD COLUMN {name} {declaration}")
        if not await (await self.db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='billing_period_migration'")).fetchone():
            await self.db.execute("UPDATE usage SET day=strftime('%Y-%m-%d',created,'unixepoch','+8 hours'), month=strftime('%Y-%m',created,'unixepoch','+8 hours')")
            await self.db.execute("CREATE TABLE billing_period_migration(done INTEGER PRIMARY KEY CHECK(done=1))")
            await self.db.execute("INSERT INTO billing_period_migration VALUES(1)")
        await self.db.execute("CREATE INDEX IF NOT EXISTS usage_history ON usage(day,created DESC,id DESC)")
        await self.db.execute("CREATE INDEX IF NOT EXISTS usage_month_history ON usage(month,created DESC,id DESC)")
        await self.db.execute("CREATE INDEX IF NOT EXISTS task_admin_history ON tasks(updated DESC,id DESC)")
        await self.db.execute("UPDATE usage SET status='unknown' WHERE status='pending'")
        # Single-process ownership: interrupted work is retried after restart.
        await self.db.execute(
            "UPDATE tasks SET state='queued',lease=NULL WHERE state IN ('running','verifying','partial')"
        )
        await self.db.execute("UPDATE mail_outbox SET state='pending' WHERE state='sending'")
        self.read = (
            self.db
            if self.path == ":memory:"
            else await aiosqlite.connect(self.path, isolation_level=None)
        )
        self.read.row_factory = aiosqlite.Row
        columns = await self.rows("PRAGMA table_info(translations)")
        if "format" not in {c["name"] for c in columns}:
            await self.db.execute(
                "ALTER TABLE translations ADD COLUMN format TEXT NOT NULL DEFAULT 'markdown'"
            )
        if "no_translation_reason" not in {c["name"] for c in columns}:
            await self.db.execute("ALTER TABLE translations ADD COLUMN no_translation_reason TEXT")
        # One indexed row per current project/language summary. No body text is
        # indexed or loaded for catalogue requests.
        if not await (await self.db.execute("SELECT 1 FROM sqlite_master WHERE name='multilingual_catalogue_migration'")).fetchone():
            await self.db.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS localized_catalogue(key TEXT NOT NULL, language TEXT NOT NULL,
                    text TEXT NOT NULL, PRIMARY KEY(key,language));
                CREATE VIRTUAL TABLE IF NOT EXISTS localized_catalogue_fts USING fts5(key UNINDEXED,language UNINDEXED,text,tokenize='trigram');
                CREATE TRIGGER IF NOT EXISTS localized_insert AFTER INSERT ON localized_catalogue BEGIN
                    INSERT INTO localized_catalogue_fts VALUES(new.key,new.language,new.text); END;
                CREATE TRIGGER IF NOT EXISTS localized_delete AFTER DELETE ON localized_catalogue BEGIN
                    DELETE FROM localized_catalogue_fts WHERE key=old.key AND language=old.language; END;
                CREATE TRIGGER IF NOT EXISTS localized_update AFTER UPDATE ON localized_catalogue BEGIN
                    DELETE FROM localized_catalogue_fts WHERE key=old.key AND language=old.language;
                    INSERT INTO localized_catalogue_fts VALUES(new.key,new.language,new.text); END;
            """)
            candidates = await (await self.db.execute("SELECT t.key,t.language,t.text,t.context,l.data FROM translations t JOIN catalogue c ON c.key=t.key AND c.summary_hash=t.source_hash JOIN resource_lookup l ON l.key=t.key WHERE t.field='summary' AND t.policy=? ORDER BY t.generated DESC", (POLICY,))).fetchall()
            for row in candidates:
                if row["context"] == self.context(json.loads(row["data"])):
                    await self.db.execute("INSERT OR IGNORE INTO localized_catalogue VALUES(?,?,?)", (row["key"], row["language"], row["text"]))
            await self.db.execute("DELETE FROM catalogue_fts")
            await self.db.execute("INSERT INTO catalogue_fts SELECT key,title||' '||summary FROM catalogue")
            await self.db.execute("CREATE TABLE multilingual_catalogue_migration(done INTEGER PRIMARY KEY CHECK(done=1))")
            await self.db.execute("INSERT INTO multilingual_catalogue_migration VALUES(1)")
            await self.db.execute("COMMIT")
        await self.db.execute(
            "INSERT OR IGNORE INTO resource_lookup SELECT key,json_remove(snapshot,'$.fields.body.text','$.metadata') FROM resources"
        )
        # Backfill only the small metadata object, never large body text.
        for row in await self.rows(
            "SELECT r.key,json_extract(r.snapshot,'$.metadata') AS metadata FROM resources r LEFT JOIN project_metadata m ON m.key=r.key WHERE m.key IS NULL"
        ):
            await self.db.execute(
                "INSERT OR IGNORE INTO project_metadata VALUES(?,?)",
                (
                    row["key"],
                    json.dumps(
                        catalogue_metadata(json.loads(row["metadata"])),
                        ensure_ascii=False,
                    ),
                ),
            )
        return self

    async def close(self):
        if self.read is not self.db:
            await self.read.close()
        await self.db.close()

    @asynccontextmanager
    async def transaction(self, *, invalidate=True):
        async with self.lock:
            await self.db.execute("BEGIN IMMEDIATE")
            try:
                yield self.db
                await self.db.commit()
                if invalidate:
                    self.version += 1
            except BaseException:
                await self.db.rollback()
                raise

    async def rows(self, sql, params=()):
        # Execute + fetch on the SQLite worker in one hop, rather than enqueueing
        # execute, fetchall and cursor.close separately for every small lookup.
        return [dict(row) for row in await self.read.execute_fetchall(sql, params)]

    async def one(self, sql, params=()):
        values = await self.rows(sql, params)
        return values[0] if values else None

    async def source(self, key):
        value = await self.one("SELECT * FROM resources WHERE key=?", (key,))
        if value:
            return {
                **json.loads(value["snapshot"]),
                "checked": value["checked"],
                "accessed": value["accessed"],
            }

    async def save_source(self, snapshot):
        key = resource_key(snapshot["resource"])
        stamp = time.time()
        old = await self.source(key)
        content_hash = snapshot["snapshot_hash"]
        async with self.transaction() as db:
            await db.execute(
                "INSERT OR IGNORE INTO snapshots VALUES(?,?,?,?)",
                (key, content_hash, json.dumps(snapshot, ensure_ascii=False), stamp),
            )
            await db.execute(
                "INSERT INTO resources VALUES(?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET snapshot=excluded.snapshot,snapshot_hash=excluded.snapshot_hash,checked=excluded.checked,accessed=excluded.accessed",
                (
                    key,
                    json.dumps(snapshot["resource"]),
                    json.dumps(snapshot, ensure_ascii=False),
                    content_hash,
                    stamp,
                    stamp,
                ),
            )
            if not old or old["snapshot_hash"] != content_hash:
                changed = [
                    name
                    for name, value in snapshot["fields"].items()
                    if not old
                    or old["fields"].get(name, {}).get("source_hash")
                    != value["source_hash"]
                ]
                await db.execute(
                    "INSERT INTO history(key,type,data,observed) VALUES(?,?,?,?)",
                    (
                        key,
                        "source_observed",
                        json.dumps(
                            dict(snapshot_hash=content_hash, changed_fields=changed)
                        ),
                        stamp,
                    ),
                )
            if old and old.get("metadata_hash") != snapshot.get("metadata_hash"):
                await db.execute(
                    "INSERT INTO history(key,type,data,observed) VALUES(?,?,?,?)",
                    (
                        key,
                        "metadata_updated",
                        json.dumps(dict(metadata_hash=snapshot.get("metadata_hash"))),
                        stamp,
                    ),
                )
            await db.execute(
                "INSERT OR REPLACE INTO project_metadata VALUES(?,?)",
                (
                    key,
                    json.dumps(
                        catalogue_metadata(snapshot["metadata"]), ensure_ascii=False
                    ),
                ),
            )
            lookup = {k: v for k, v in snapshot.items() if k != "metadata"}
            lookup["fields"] = {
                name: {k: v for k, v in field.items() if name != "body" or k != "text"}
                for name, field in snapshot["fields"].items()
            }
            await db.execute(
                "INSERT OR REPLACE INTO resource_lookup VALUES(?,?)",
                (key, json.dumps(lookup, ensure_ascii=False)),
            )
            title = snapshot["fields"]["title"]["text"]
            summary = snapshot["fields"]["summary"]["text"]
            # Keep a materialized public catalogue so search never performs one
            # translation query per project or scans large body JSON blobs.
            keep = (
                old
                and self.context(old) == self.context(snapshot)
                and old["fields"]["summary"]["source_hash"]
                == snapshot["fields"]["summary"]["source_hash"]
            )
            if not keep:
                await db.execute("DELETE FROM localized_catalogue WHERE key=?", (key,))
            cursor = await db.execute(
                "SELECT localized,quality FROM catalogue WHERE key=?", (key,)
            )
            prior = await cursor.fetchone()
            localized, quality = (
                (prior[0], prior[1]) if prior and keep else (None, None)
            )
            await db.execute(
                "INSERT OR REPLACE INTO catalogue VALUES(?,?,?,?,?,?,?,?)",
                (
                    key,
                    snapshot["kind"],
                    title,
                    summary,
                    localized,
                    quality,
                    snapshot["source_url"],
                    snapshot["fields"]["summary"]["source_hash"],
                ),
            )
            await db.execute("DELETE FROM catalogue_fts WHERE key=?", (key,))
            await db.execute(
                "INSERT INTO catalogue_fts VALUES(?,?)",
                (key, title + " " + summary),
            )
        return old

    async def quota(self, bucket, limit, count=1, seconds=3600):
        window = int(time.time()) // seconds
        async with self.transaction() as db:
            cursor = await db.execute(
                "SELECT count FROM quotas WHERE bucket=? AND window=?", (bucket, window)
            )
            row = await cursor.fetchone()
            if (row[0] if row else 0) + count > limit:
                return False
            await db.execute(
                "INSERT INTO quotas VALUES(?,?,?) ON CONFLICT(bucket,window) DO UPDATE SET count=count+excluded.count",
                (bucket, window, count),
            )
            await db.execute(
                "DELETE FROM quotas WHERE window < ? AND bucket=?", (window - 2, bucket)
            )
            return True

    async def session(self, ip):
        token = secrets.token_urlsafe(32)
        async with self.transaction() as db:
            await db.execute("DELETE FROM sessions WHERE expires<?", (time.time(),))
            await db.execute(
                "INSERT INTO sessions VALUES(?,?,?)",
                (hashlib.sha256(token.encode()).hexdigest(), time.time() + 3600, ip),
            )
        return token

    async def owner(self, token):
        identity = hashlib.sha256(token.encode()).hexdigest()
        result = await self.one(
            "SELECT hash FROM sessions WHERE hash=? AND expires>?",
            (identity, time.time()),
        )
        return identity if result else None

    @staticmethod
    def context(source):
        return (
            digest(
                {
                    "title": source["fields"]["title"]["text"],
                    "resource": source["resource"],
                }
            )
            if source
            else ""
        )

    async def translation(self, key, field, source_hash, language, context):
        return await self.one(
            "SELECT * FROM translations WHERE key=? AND field=? AND source_hash=? AND language=? AND context=? AND policy=?",
            (key, field, source_hash, language, context, POLICY),
        )

    async def enqueue(
        self,
        resource,
        field,
        source_hash,
        language,
        purpose,
        owner="internal",
        max_queue=1000,
    ):
        key = resource_key(resource)
        context = self.context(await self.source(key))
        dedup = digest(
            dict(
                key=key,
                field=field,
                source_hash=source_hash,
                language=language,
                context=context,
                policy=POLICY,
            )
        )
        stamp = time.time()
        async with self.transaction() as db:
            cursor = await db.execute(
                "SELECT * FROM tasks WHERE key=? AND field=? AND source_hash=? AND language=? AND state IN ('queued','running','verifying','partial','deferred') ORDER BY created LIMIT 1",
                (key, field, source_hash, language),
            )
            task = await cursor.fetchone()
            if not task:
                cursor = await db.execute("SELECT * FROM tasks WHERE dedup=?", (dedup,))
                task = await cursor.fetchone()
            if not task:
                cursor = await db.execute(
                    "SELECT COUNT(*) FROM tasks WHERE state IN ('queued','running','verifying','partial','deferred')"
                )
                if (await cursor.fetchone())[0] >= max_queue:
                    raise OverflowError("queue_full")
                task_id = "task_" + secrets.token_hex(12)
                await db.execute(
                    "INSERT INTO tasks(id,dedup,key,ref,field,source_hash,language,context,state,purpose,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        task_id,
                        dedup,
                        key,
                        json.dumps(resource),
                        field,
                        source_hash,
                        language,
                        context,
                        "queued",
                        purpose,
                        stamp,
                        stamp,
                    ),
                )
            else:
                task_id = task["id"]
                if task["state"] == "failed":
                    await self.restart_failed(db, task, stamp, purpose, max_queue, cooldown=60)
                elif purpose == "detail" and task["purpose"] != "detail":
                    await db.execute(
                        "UPDATE tasks SET purpose='detail' WHERE id=?", (task_id,)
                    )
            await db.execute(
                "INSERT OR IGNORE INTO tickets VALUES(?,?,?)",
                ("job_" + secrets.token_hex(12), owner, task_id),
            )
            cursor = await db.execute(
                "SELECT id FROM tickets WHERE owner=? AND task=?", (owner, task_id)
            )
            return (await cursor.fetchone())[0]

    async def restart_failed(self, db, task, stamp, purpose, max_queue, cooldown=0):
        """Called under the writer lock; one task/lease survives concurrent retries."""
        count = await (await db.execute("SELECT COUNT(*) FROM tasks WHERE state IN ('queued','running','verifying','partial','deferred')")).fetchone()
        if count[0] >= max_queue:
            raise OverflowError("queue_full")
        pause = await (await db.execute("SELECT resume_at FROM budget_pause WHERE id=1 AND resume_at>?", (stamp,))).fetchone()
        resume_at = max(stamp, task["updated"] + cooldown, pause[0] if pause else 0)
        state = "deferred" if resume_at > stamp else "queued"
        await db.execute(
            "UPDATE tasks SET state=?,purpose=?,attempt=0,completed=0,total=NULL,error=NULL,current_hash=NULL,lease=NULL,updated=?,next_attempt=? WHERE id=? AND state='failed'",
            (state, purpose, stamp, resume_at, task["id"]),
        )

    async def retry_task(self, task_id, max_queue=1000):
        stamp = time.time()
        async with self.transaction() as db:
            task = await (await db.execute("SELECT * FROM tasks WHERE id=?", (task_id,))).fetchone()
            if not task:
                return None
            if task["state"] != "failed":
                return dict(task)
            await self.restart_failed(db, task, stamp, "detail", max_queue)
            row = await (await db.execute("SELECT * FROM tasks WHERE id=?", (task_id,))).fetchone()
            return dict(row)

    async def claim(self):
        stamp = time.time()
        # Model reservations enforce the pause before any paid HTTP request.
        # Local original-content results and source refreshes still complete.
        if not await self.one(
            "SELECT 1 FROM tasks WHERE state IN ('queued','deferred') AND next_attempt<=? LIMIT 1",
            (stamp,),
        ):
            return None
        async with self.transaction() as db:
            cursor = await db.execute(
                "SELECT * FROM tasks WHERE state IN ('queued','deferred') AND next_attempt<=? ORDER BY CASE WHEN created<? THEN 0 WHEN purpose='detail' THEN 1 ELSE 2 END,created LIMIT 1",
                (stamp, stamp - 120),
            )
            task = await cursor.fetchone()
            if not task:
                return None
            lease = secrets.token_hex(16)
            await db.execute(
                "UPDATE tasks SET state='verifying',attempt=attempt+1,lease=?,updated=? WHERE id=?",
                (lease, stamp, task["id"]),
            )
            return {**dict(task), "lease": lease, "attempt": task["attempt"] + 1}

    async def task_state(self, task, state, error=None, retry=0, **values):
        allowed = {"completed", "total", "current_hash", "context"}
        assert set(values) <= allowed
        updates = dict(
            state=state,
            error=json.dumps(error) if error else None,
            updated=time.time(),
            next_attempt=time.time() + retry,
            **values,
        )
        sql = ",".join(key + "=?" for key in updates)
        async with self.transaction() as db:
            cursor = await db.execute(
                "UPDATE tasks SET " + sql + " WHERE id=? AND lease=?",
                (*updates.values(), task["id"], task["lease"]),
            )
            return bool(cursor.rowcount)

    async def ticket(self, job_id, owner):
        return await self.one(
            "SELECT tasks.*,tickets.id AS job_id FROM tickets JOIN tasks ON tasks.id=tickets.task WHERE tickets.id=? AND tickets.owner=?",
            (job_id, owner),
        )

    async def pending(self, key, field, source_hash, language):
        return await self.one(
            "SELECT * FROM tasks WHERE key=? AND field=? AND source_hash=? AND language=? ORDER BY created DESC LIMIT 1",
            (key, field, source_hash, language),
        )

    async def segment(self, identity, language):
        result = await self.one(
            "SELECT text FROM segments WHERE hash=? AND language=? AND policy=?",
            (identity, language, POLICY),
        )
        return result["text"] if result else None

    async def save_segments(self, segments, language):
        async with self.transaction() as db:
            await db.executemany(
                "INSERT OR REPLACE INTO segments VALUES(?,?,?,?)",
                [
                    (identity, language, POLICY, text)
                    for identity, text in segments.items()
                ],
            )

    async def reserve(self, tokens, config):
        day, month = billing_period()
        async with self.transaction() as db:
            pause = await (await db.execute("SELECT resume_at FROM budget_pause WHERE id=1")).fetchone()
            if pause and pause[0] > time.time():
                return None
            await db.execute("DELETE FROM budget_pause WHERE id=1")
            cursor = await db.execute(
                "SELECT COALESCE(SUM(tokens),0),COUNT(*) FROM usage WHERE day=?", (day,)
            )
            used, calls = await cursor.fetchone()
            cursor = await db.execute(
                "SELECT COALESCE(SUM(tokens),0) FROM usage WHERE month=?", (month,)
            )
            monthly = (await cursor.fetchone())[0]
            if (
                used + tokens > config.daily_token_limit
                or monthly + tokens > config.monthly_token_limit
                or calls >= config.daily_call_limit
            ):
                now = datetime.now(LOCAL_TZ)
                tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
                reason = "daily_budget" if used + tokens > config.daily_token_limit or calls >= config.daily_call_limit else "monthly_budget"
                await db.execute("INSERT OR REPLACE INTO budget_pause VALUES(1,?,?)", (tomorrow.timestamp(), reason))
                return None
            identity = secrets.token_hex(16)
            await db.execute(
                "INSERT INTO usage(id,day,month,tokens,actual,created,model,status) VALUES(?,?,?,?,?,?,?,?)",
                (identity, day, month, tokens, 0, time.time(), config.model, "pending"),
            )
            return identity

    async def settle(self, reservation, usage, config=None, status="succeeded"):
        if not isinstance(usage, dict):
            if type(usage) is int and usage >= 0:
                async with self.transaction() as db:
                    await db.execute("UPDATE usage SET tokens=?,actual=1,status=? WHERE id=?", (usage, status, reservation))
                return
            usage = {}
        total = usage.get("total_tokens")
        input_tokens = usage.get("prompt_tokens")
        prompt_details = usage.get("prompt_tokens_details")
        cached_tokens = prompt_details.get("cached_tokens") if isinstance(prompt_details, dict) else None
        output_tokens = usage.get("completion_tokens")
        total_valid = type(total) is int and total >= 0
        details_valid = all(type(value) is int and value >= 0 for value in (input_tokens, cached_tokens, output_tokens))
        price = config.model_prices.get(config.model) if config else None
        cost = None
        details_valid = details_valid and cached_tokens <= input_tokens and input_tokens + output_tokens == total
        if total_valid and details_valid and price:
            cost = ((input_tokens - cached_tokens) * price["input"] + cached_tokens * price["cached_input"] + output_tokens * price["output"]) / 1_000_000
        async with self.transaction() as db:
            if total_valid:
                known = int(details_valid)
                await db.execute("UPDATE usage SET tokens=?,actual=1,input_tokens=?,cached_tokens=?,output_tokens=?,details_known=?,cost=?,price_snapshot=?,status=? WHERE id=?", (total, input_tokens if known else 0, cached_tokens if known else 0, output_tokens if known else 0, known, cost, json.dumps(price) if cost is not None else None, status, reservation))
            else:
                await db.execute("UPDATE usage SET status=? WHERE id=?", ("unknown" if status == "succeeded" else status, reservation))

    async def usage_failure(self, reservation, status="failed"):
        if reservation:
            async with self.transaction() as db:
                await db.execute("UPDATE usage SET status=? WHERE id=? AND status='pending'", (status, reservation))

    async def publish(self, task, source, text, format="markdown", quality="machine", no_translation_reason=None):
        stamp = time.time()
        revision = "tr_" + secrets.token_hex(12)
        artifact = dict(
            schema="mc-translation-artifact-v1",
            resource=source["resource"],
            field=task["field"],
            source_hash=task["source_hash"],
            target_language=task["language"],
            profile="minecraft-v1",
            translation_revision=revision,
            policy_revision=POLICY,
            quality=quality,
            generated_at=utc(stamp),
            format=format,
            text=text,
        )
        if no_translation_reason:
            artifact.update(translation_required=False, no_translation_reason=no_translation_reason)
        content = canonical(artifact)
        output_hash = hashlib.sha256(content).hexdigest()
        context = self.context(source)
        async with self.transaction() as db:
            cursor = await db.execute(
                "SELECT lease FROM tasks WHERE id=?", (task["id"],)
            )
            active = await cursor.fetchone()
            if not active or active[0] != task["lease"]:
                return False
            await db.execute(
                "INSERT OR IGNORE INTO artifacts VALUES(?,?)", (output_hash, content)
            )
            await db.execute(
                "INSERT OR REPLACE INTO translations VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    task["key"],
                    task["field"],
                    task["source_hash"],
                    task["language"],
                    context,
                    POLICY,
                    revision,
                    quality,
                    text,
                    output_hash,
                    stamp,
                    format,
                    no_translation_reason,
                ),
            )
            await db.execute(
                "UPDATE tasks SET state='ready',context=?,updated=?,completed=COALESCE(total,0),error=NULL WHERE id=? AND lease=?",
                (context, stamp, task["id"], task["lease"]),
            )
            await db.execute(
                "INSERT INTO history(key,type,data,observed) VALUES(?,?,?,?)",
                (
                    task["key"],
                    "translation_published",
                    json.dumps(
                        dict(
                            field=task["field"],
                            source_hash=task["source_hash"],
                            translation_revision=revision,
                            quality=quality,
                        )
                    ),
                    stamp,
                ),
            )
            current = await db.execute(
                "SELECT snapshot FROM resources WHERE key=?", (task["key"],)
            )
            current = await current.fetchone()
            current = json.loads(current[0]) if current else None
            if (
                task["field"] == "summary"
                and current
                and current["fields"]["summary"]["source_hash"] == task["source_hash"]
                and self.context(current) == context
            ):
                await db.execute(
                    "INSERT INTO localized_catalogue VALUES(?,?,?) ON CONFLICT(key,language) DO UPDATE SET text=excluded.text",
                    (task["key"], task["language"], text),
                )
                # Keep the old default-language catalogue compatible with
                # already released launchers. Other languages never overwrite it.
                if task["language"] == DEFAULT_LANGUAGE:
                    await db.execute("UPDATE catalogue SET localized=?,quality=? WHERE key=?", (text, quality, task["key"]))
        return True

    async def maintenance(self):
        async with self.transaction() as db:
            await db.execute("DELETE FROM idempotency WHERE expires<?", (time.time(),))
            await db.execute("DELETE FROM sessions WHERE expires<?", (time.time(),))
            await db.execute(
                "DELETE FROM quotas WHERE window<?", (int(time.time()) // 3600 - 48,)
            )
