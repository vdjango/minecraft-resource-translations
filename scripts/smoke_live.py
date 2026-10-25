"""Paid end-to-end smoke check against an explicitly selected live service."""

import argparse
import asyncio
import hashlib
import json
import time
import uuid

from aiohttp import ClientSession, ClientTimeout

from mc_translate.hashing import resource_ref, text_hash


async def main(base, all_types):
    async with ClientSession(
        timeout=ClientTimeout(total=40),
        headers={"User-Agent": "Buggcraft-Translate-Smoke/0.1"},
    ) as session:

        async def call(path, data=None, headers=None, root=None):
            async with session.request(
                "GET" if data is None else "POST",
                (root or base).rstrip("/") + path,
                json=data,
                headers=headers,
            ) as response:
                value = await response.json()
                if response.status >= 400:
                    raise RuntimeError(
                        str(response.status)
                        + " "
                        + str(value.get("code", "upstream_error"))
                    )
                return value

        capabilities = await call("/v1/capabilities")
        assert capabilities["generation_available"]
        auth = await call(
            "/v1/sessions/anonymous", {"client_id": "buggcraft-live-smoke"}
        )
        headers = {"Authorization": "Bearer " + auth["access_token"]}
        resources = [resource_ref("modrinth", "AANobbMI")]
        if all_types:
            for kind in ("resourcepack", "shader", "modpack"):
                query = await call(
                    "/search?limit=1&facets="
                    + __import__("urllib.parse", fromlist=["quote"]).quote(
                        json.dumps([["project_type:" + kind]])
                    ),
                    root="https://api.modrinth.com/v2",
                )
                resources.append(
                    resource_ref("modrinth", query["hits"][0]["project_id"])
                )
            for class_id in (6, 12, 6552, 4471):
                query = await call(
                    f"/mods/search?gameId=432&classId={class_id}&pageSize=1&sortField=2&sortOrder=desc",
                    root="https://download.bugg.icu/api/curseforge/v1",
                )
                resources.append(
                    resource_ref("curseforge", str(query["data"][0]["id"]))
                )
        jobs = []
        for ref in resources:
            result = await call(
                f"/v1/projects/{ref['provider']}/{ref['project_id']}/refresh",
                {},
                headers,
            )
            jobs.append(result["job_id"])

        async def wait(jobs):
            pending = set(jobs)
            start = time.monotonic()
            seen = {}
            while pending and time.monotonic() - start < 240:
                for identity in list(pending):
                    value = await call("/v1/jobs/" + identity, headers=headers)
                    if seen.get(identity) != value["state"]:
                        print(
                            json.dumps(
                                dict(
                                    job_id=identity,
                                    state=value["state"],
                                    field=value["field"],
                                    progress=[
                                        value.get("completed_segments"),
                                        value.get("total_segments"),
                                    ],
                                ),
                                ensure_ascii=False,
                            ),
                            flush=True,
                        )
                        seen[identity] = value["state"]
                    if value["state"] == "ready":
                        pending.remove(identity)
                    elif value["state"] in ("failed", "source_mismatch"):
                        raise RuntimeError(str(value.get("error")))
                if pending:
                    await asyncio.sleep(1)
            if pending:
                raise RuntimeError("Timed out; background jobs remain pending")

        await wait(jobs)
        items = []
        for index, ref in enumerate(resources):
            source = await call(
                f"/v1/projects/{ref['provider']}/{ref['project_id']}/source"
            )
            names = ["title", "summary"] + (["body"] if index == 0 else [])
            fields = []
            for name in names:
                value = source["fields"][name]
                assert (
                    text_hash(value["text"], name, value["format"])
                    == value["source_hash"]
                )
                fields.append(dict(field=name, source_hash=value["source_hash"]))
            items.append(dict(request_id=str(index), resource=ref, fields=fields))
            print(
                json.dumps(
                    dict(
                        project=ref["project_id"],
                        provider=ref["provider"],
                        kind=source["kind"],
                        title=source["fields"]["title"]["text"],
                    ),
                    ensure_ascii=False,
                ),
                flush=True,
            )
        payload = dict(target_language="zh-Hans", profile="minecraft-v1", items=items)
        await call("/v1/translations:resolve", payload)
        ensured = await call(
            "/v1/translations:ensure",
            dict(payload, purpose="detail"),
            dict(headers, **{"Idempotency-Key": uuid.uuid4().hex}),
        )
        jobs = [
            ticket["job_id"]
            for item in ensured["results"]
            for ticket in item["tickets"]
        ]
        await wait(jobs)
        resolved = await call("/v1/translations:resolve", payload)
        for item in resolved["results"]:
            for field in item["fields"]:
                assert field["state"] == "ready" and field["matches_request"]
                translated = field["translation"]
                if "text" not in translated:
                    identity = translated["artifact"]["artifact_sha256"]
                    async with session.get(
                        base + "/v1/artifacts/" + identity
                    ) as response:
                        raw = await response.read()
                    assert hashlib.sha256(raw).hexdigest() == identity
                    translated = json.loads(raw)
                if field["field"] != "title":
                    assert any("\u4e00" <= c <= "\u9fff" for c in translated["text"])
        print(
            json.dumps(
                dict(
                    ok=True,
                    projects=len(resources),
                    fields=sum(len(i["fields"]) for i in items),
                    base=base,
                )
            ),
            flush=True,
        )


parser = argparse.ArgumentParser()
parser.add_argument("--base-url", default="https://translate.bugg.icu")
parser.add_argument("--all-types", action="store_true")
args = parser.parse_args()
asyncio.run(main(args.base_url.rstrip("/"), args.all_types))
