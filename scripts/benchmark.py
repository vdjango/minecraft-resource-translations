"""Synthetic read-path benchmark; never calls the model or an external source."""

import argparse
import asyncio
import json
import statistics
import tempfile
import time
from collections import OrderedDict
from pathlib import Path

from aiohttp import ClientSession, web

from mc_translate.api import create_app
from mc_translate.config import Config
from mc_translate.hashing import digest, resource_ref, text_hash


class BypassCache(OrderedDict):
    def get(self, key, default=None):
        return default


async def main(count, concurrency):
    with tempfile.TemporaryDirectory() as directory:
        app = await create_app(
            Config(database=str(Path(directory) / "bench.sqlite")), workers=False
        )
        store = app["store"]
        items = []
        for i in range(20):
            ref = resource_ref("modrinth", f"Bench{i:04d}")
            fields = {
                name: dict(
                    text=text,
                    format=fmt,
                    source_language="und",
                    source_hash=text_hash(text, name, fmt),
                )
                for name, text, fmt in [
                    ("title", f"Bench {i}", "plain"),
                    ("summary", f"English description {i}", "plain"),
                    ("body", "# Full body", "markdown"),
                ]
            }
            source = dict(
                resource=ref,
                kind="mod",
                source_url="https://modrinth.com/project/" + ref["project_id"],
                metadata=dict(created_at=None, author_ids=[]),
                fields=fields,
                snapshot_hash=digest(
                    dict(
                        schema="mc-snapshot-v1",
                        fields={n: f["source_hash"] for n, f in fields.items()},
                    )
                ),
                metadata_hash=digest("metadata"),
            )
            await store.save_source(source)
            await store.enqueue(
                ref,
                "summary",
                fields["summary"]["source_hash"],
                "zh-Hans",
                "visible_list",
            )
            task = await store.claim()
            await store.publish(task, source, "中文性能测试简介 " + str(i), "plain")
            items.append(
                dict(
                    request_id=str(i),
                    resource=ref,
                    fields=[
                        dict(
                            field="summary",
                            source_hash=fields["summary"]["source_hash"],
                        )
                    ],
                )
            )
        # Raise only the in-process synthetic benchmark limiter; production
        # protections are not disabled by an environment switch.
        app["api"].rate = lambda *args, **kwargs: None
        if args.cold:
            # Bypass both raw-response and semantic field caches. Merely changing
            # correlation IDs is now a warm semantic-cache scenario.
            app["api"].cache = BypassCache()
            app["api"].lookup_cache = BypassCache()
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        payload = dict(target_language="zh-Hans", profile="minecraft-v1", items=items)
        latencies = []
        errors = 0
        async with ClientSession() as client:
            url = f"http://127.0.0.1:{port}/v1/translations:resolve"
            async with client.post(url, json=payload) as response:
                await response.read()
            start = time.perf_counter()
            counter = 0

            async def worker():
                nonlocal counter, errors
                while counter < count:
                    index = counter
                    counter += 1
                    before = time.perf_counter()
                    body = (
                        dict(
                            payload,
                            items=[
                                dict(item, request_id=f"{index}-{item['request_id']}")
                                for item in items
                            ],
                        )
                        if args.cold or args.unique_ids
                        else payload
                    )
                    async with client.post(url, json=body) as response:
                        data = await response.json()
                        if response.status != 200 or any(
                            f["state"] != "ready"
                            for r in data.get("results", [])
                            for f in r["fields"]
                        ):
                            errors += 1
                    latencies.append((time.perf_counter() - before) * 1000)

            try:
                await asyncio.gather(*(worker() for _ in range(concurrency)))
            finally:
                await runner.cleanup()
            elapsed = time.perf_counter() - start
        latencies.sort()
        result = dict(
            requests=count,
            items_per_request=20,
            concurrency=concurrency,
            cache="database" if args.cold else "unique_ids" if args.unique_ids else "hot",
            seconds=round(elapsed, 3),
            requests_per_second=round(count / elapsed, 1),
            fields_per_second=round(count * 20 / elapsed),
            p50_ms=round(statistics.median(latencies), 2),
            p95_ms=round(latencies[int(len(latencies) * 0.95) - 1], 2),
            p99_ms=round(latencies[int(len(latencies) * 0.99) - 1], 2),
            errors=errors,
        )
        print(json.dumps(result))


parser = argparse.ArgumentParser()
parser.add_argument("--requests", type=int, default=1000)
parser.add_argument("--concurrency", type=int, default=20)
parser.add_argument("--cold", action="store_true")
parser.add_argument("--unique-ids", action="store_true")
args = parser.parse_args()
asyncio.run(main(args.requests, args.concurrency))
