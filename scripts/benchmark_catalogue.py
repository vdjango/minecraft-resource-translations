"""Synthetic catalogue first/deep page benchmark, no upstream/model requests."""

import argparse
import asyncio
import json
import statistics
import tempfile
import time
from pathlib import Path

from aiohttp import ClientSession, web

from mc_translate.api import create_app
from mc_translate.config import Config
from mc_translate.hashing import resource_ref


async def main(args):
    with tempfile.TemporaryDirectory() as directory:
        app = await create_app(
            Config(database=str(Path(directory) / "bench.sqlite")), workers=False
        )
        store = app["store"]
        # Populate the materialized read tables directly; setup is excluded.
        # This tests catalogue SQL/JSON/HTTP, not source ingestion or FTS search.
        refs, cards = [], []
        for i in range(args.projects):
            key = f"modrinth:Bench{i:07d}"
            refs.append(
                (
                    key,
                    json.dumps(resource_ref("modrinth", f"Bench{i:07d}")),
                    "{}",
                    "fixture",
                    0,
                    0,
                )
            )
            cards.append(
                (
                    key,
                    "mod",
                    f"Bench {i}",
                    "Original summary",
                    "中文简介",
                    "ai",
                    "https://modrinth.com/project/fixture",
                    "sha256:fixture",
                )
            )
        async with store.transaction() as db:
            await db.executemany("INSERT INTO resources VALUES(?,?,?,?,?,?)", refs)
            await db.executemany("INSERT INTO catalogue VALUES(?,?,?,?,?,?,?,?)", cards)
        app["api"].rate = lambda *args: None
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        result = {}
        try:
            async with ClientSession() as client:
                for name, offset in (
                    ("first_page", 0),
                    ("deep_page", max(0, args.projects - 100)),
                ):
                    cursor = (
                        app["api"].page_cursor(
                            f"modrinth:Bench{offset - 1:07d}",
                            dict(q="", kind="", limit=20, target_language="zh-Hans"),
                        )
                        if offset
                        else ""
                    )
                    latencies = []
                    for _ in range(args.requests):
                        start = time.perf_counter()
                        async with client.get(
                            f"http://127.0.0.1:{port}/v1/catalog/projects",
                            params=dict(limit="20", cursor=cursor),
                        ) as response:
                            body = await response.json()
                            assert (
                                response.status == 200
                                and body["total_count"] == args.projects
                            )
                            assert len(body["projects"]) == 20
                            assert (
                                body["projects"][0]["resource"]["project_id"]
                                == f"Bench{offset:07d}"
                            )
                        latencies.append((time.perf_counter() - start) * 1000)
                    latencies.sort()
                    result[name] = dict(
                        p50_ms=round(statistics.median(latencies), 2),
                        p95_ms=round(latencies[int(len(latencies) * 0.95) - 1], 2),
                    )
            print(
                json.dumps(
                    dict(
                        projects=args.projects,
                        requests_per_case=args.requests,
                        rows_per_page=20,
                        errors=0,
                        **result,
                    )
                )
            )
        finally:
            await runner.cleanup()


parser = argparse.ArgumentParser()
parser.add_argument("--projects", type=int, default=100000)
parser.add_argument("--requests", type=int, default=100)
asyncio.run(main(parser.parse_args()))
