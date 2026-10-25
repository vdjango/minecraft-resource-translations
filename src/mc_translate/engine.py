import asyncio
import json
import logging
import time

from .content import assemble, prepare, validate_translation, no_translation_reason
from .hashing import resource_key
from .model import ModelError
from .sources import SourceError

LOG = logging.getLogger(__name__)
TERMINAL = {"ready", "failed", "source_mismatch"}


class Engine:
    def __init__(self, config, store, sources, model):
        self.config, self.store, self.sources, self.model = (
            config,
            store,
            sources,
            model,
        )
        self.wake = asyncio.Event()
        self.tasks = []

    async def start(self):
        self.tasks = [
            asyncio.create_task(self.worker()) for _ in range(self.config.workers)
        ]
        self.tasks.append(asyncio.create_task(self.synchronize()))

    async def close(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    async def enqueue(self, *args, **kwargs):
        result = await self.store.enqueue(
            *args, max_queue=self.config.max_queue, **kwargs
        )
        self.wake.set()
        return result

    async def worker(self):
        while True:
            task = await self.store.claim()
            if not task:
                self.wake.clear()
                try:
                    await asyncio.wait_for(self.wake.wait(), 1)
                except asyncio.TimeoutError:
                    pass
                continue
            try:
                await self.run(task)
            except (SourceError, ModelError) as exc:
                if exc.code == "budget_exhausted":
                    pause = await self.store.one("SELECT resume_at FROM budget_pause WHERE id=1")
                    await self.store.task_state(
                        task, "deferred", dict(code=exc.code), retry=max(1, pause["resume_at"] - time.time()) if pause else 60
                    )
                    async with self.store.transaction() as db:
                        await db.execute("UPDATE tasks SET attempt=MAX(0,attempt-1) WHERE id=? AND lease=?", (task["id"], task["lease"]))
                elif exc.retryable and task["attempt"] < 3:
                    await self.store.task_state(
                        task,
                        "deferred",
                        dict(code=exc.code),
                        retry=min(60, 5 * 2 ** task["attempt"]),
                    )
                else:
                    await self.store.task_state(task, "failed", dict(code=exc.code))
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.exception("Task failed: %s", task["id"])
                await self.store.task_state(task, "failed", dict(code="internal_error"))

    async def run(self, task):
        source = await self.sources.fetch(
            json.loads(task["ref"]),
            expected={task["field"]: task["source_hash"]}
            if task["field"] != "__refresh__"
            else None,
            force=task["field"] == "__refresh__",
        )
        if task["field"] == "__refresh__":
            await self.store.task_state(task, "ready", completed=1, total=1)
            return
        field = source["fields"].get(task["field"])
        if not field or field["source_hash"] != task["source_hash"]:
            await self.store.task_state(
                task,
                "source_mismatch",
                current_hash=field["source_hash"] if field else None,
            )
            return
        cached = await self.store.translation(
            task["key"],
            task["field"],
            task["source_hash"],
            task["language"],
            self.store.context(source),
        )
        if cached:
            await self.store.task_state(task, "ready", completed=1, total=1)
            return
        title = source["fields"]["title"]["text"]
        segments, template, placeholders = prepare(field["text"], field["format"], title)
        reason = no_translation_reason(dict(field, field=task["field"]), task["language"], title, segments)
        if reason:
            await self.store.task_state(task, "running", total=1)
            await self.store.publish(task, source, field["text"], field["format"], "original", reason)
            return
        results = {}
        unique = {segment.id: segment for segment in segments}
        for identity in unique:
            cached = await self.store.segment(identity, task["language"])
            if cached is not None:
                results[identity] = cached
        await self.store.task_state(
            task,
            "running",
            completed=len(results),
            total=len(unique),
            context=self.store.context(source),
        )
        pending = [
            segment for identity, segment in unique.items() if identity not in results
        ]
        while pending:
            batch, size = [], 0
            while pending and (not batch or size + len(pending[0].text) <= 6500):
                segment = pending.pop(0)
                batch.append(segment)
                size += len(segment.text)
            translated = await self.model.translate(batch, task["language"])
            if set(translated) != {segment.id for segment in batch}:
                raise ModelError("invalid_model_output", True)
            for segment in batch:
                validate_translation(segment.text, translated[segment.id])
            await self.store.save_segments(translated, task["language"])
            results.update(translated)
            await self.store.task_state(
                task,
                "partial" if pending else "running",
                completed=len(results),
                total=len(unique),
            )
        translated = assemble(template, results, placeholders)
        await self.store.publish(
            task,
            source,
            translated,
            field["format"],
            "original" if translated == field["text"] else "machine",
            "unchanged" if translated == field["text"] else None,
        )

    async def synchronize(self):
        while True:
            await asyncio.sleep(max(10, min(60, self.config.source_interval)))
            try:
                await self.synchronize_once()
                await self.store.maintenance()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.exception("Source synchronization failed")

    async def synchronize_once(self):
        # Round-robin by check time. Also repair missing current translations
        # when a manual refresh or another worker already observed the change.
        resources = await self.store.rows(
            "SELECT ref FROM resources WHERE checked<? ORDER BY checked LIMIT 8",
            (time.time() - self.config.source_interval,),
        )
        for row in resources:
            try:
                new = await self.sources.fetch(json.loads(row["ref"]))
            except SourceError:
                continue
            key = resource_key(new["resource"])
            for field, value in new["fields"].items():
                languages = await self.store.rows(
                    "SELECT DISTINCT language FROM translations WHERE key=? AND field=?",
                    (key, field),
                )
                for row in languages:
                    language = row["language"]
                    current = await self.store.translation(
                        key, field, value["source_hash"], language, self.store.context(new)
                    )
                    if not current:
                        await self.enqueue(new["resource"], field, value["source_hash"], language, "background")
