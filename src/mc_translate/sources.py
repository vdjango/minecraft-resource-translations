import asyncio
import json
import time

import aiohttp

from .hashing import digest, resource_key, resource_ref, text_hash
from .metadata import curseforge_metadata, modrinth_metadata


class SourceError(Exception):
    def __init__(self, code, retryable=False):
        self.code, self.retryable = code, retryable


async def read_json(response, limit=1048576):
    data = bytearray()
    async for chunk in response.content.iter_chunked(65536):
        data.extend(chunk)
        if len(data) > limit:
            raise ValueError("response_too_large")
    return json.loads(data)


class Sources:
    def __init__(self, config, session, store):
        self.config, self.session, self.store = config, session, store
        self.locks = {}
        self.last_checked = {}
        self.semaphore = asyncio.Semaphore(4)

    async def get(self, url, headers=None):
        try:
            async with self.session.get(
                url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=35, connect=8),
                allow_redirects=False,
            ) as response:
                if response.status == 404:
                    raise SourceError("source_not_found")
                if response.status in (401, 403):
                    raise SourceError("source_unavailable")
                if response.status != 200:
                    raise SourceError(
                        "source_rate_limited"
                        if response.status == 429
                        else "source_unavailable",
                        True,
                    )
                return await read_json(response)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            raise SourceError("source_unavailable", True) from exc

    async def fetch(self, resource, expected=None, force=False):
        key = resource_key(resource)
        lock = self.locks.setdefault(key, asyncio.Lock())
        async with lock:
            old = await self.store.source(key)
            current = old and (
                expected is None
                or all(
                    old["fields"].get(f, {}).get("source_hash") == h
                    for f, h in expected.items()
                )
            )
            age = time.time() - old["checked"] if old else 1e9
            if old and not force and current and age < self.config.source_interval:
                return old
            # Random hash claims cannot force repeated upstream reads.
            if old and time.time() - self.last_checked.get(key, 0) < 10:
                if not current:
                    raise SourceError("source_check_deferred", True)
                return old
            self.last_checked[key] = time.time()
            async with self.semaphore:
                snapshot = await self.download(resource)
            await self.store.save_source(snapshot)
            return await self.store.source(key)

    async def download(self, resource):
        provider, identity = resource["provider"], resource["project_id"]
        if provider == "modrinth":
            data = await self.get(
                self.config.modrinth_url.rstrip("/") + "/project/" + identity
            )
            if not isinstance(data, dict) or data.get("id") != identity:
                raise SourceError("source_identity_mismatch")
            kind = data.get("project_type")
            if kind not in ("mod", "resourcepack", "shader", "modpack"):
                raise SourceError("unsupported_resource")
            title, summary, body, format = (
                data.get("title", ""),
                data.get("description", ""),
                data.get("body", ""),
                "markdown",
            )
            url = "https://modrinth.com/project/" + identity
            members, versions = await asyncio.gather(
                self.optional(
                    self.config.modrinth_url.rstrip("/")
                    + "/project/"
                    + identity
                    + "/members"
                ),
                self.optional(
                    self.config.modrinth_url.rstrip("/")
                    + "/project/"
                    + identity
                    + "/version?limit=12"
                ),
            )
            complete = isinstance(members, list) and isinstance(versions, list)
            metadata = modrinth_metadata(
                data,
                members if isinstance(members, list) else [],
                versions if isinstance(versions, list) else [],
            )
            metadata["metadata_complete"] = complete
            await self.dependency_names(metadata, provider)
        else:
            headers = (
                {"x-api-key": self.config.curseforge_key}
                if self.config.curseforge_key
                else {}
            )
            base = self.config.curseforge_url.rstrip("/")
            payload = await self.get(base + "/mods/" + identity, headers)
            data = payload.get("data", {}) if isinstance(payload, dict) else {}
            if (
                not isinstance(data, dict)
                or str(data.get("id")) != identity
                or data.get("gameId") != 432
            ):
                raise SourceError("source_identity_mismatch")
            kind = {6: "mod", 12: "resourcepack", 6552: "shader", 4471: "modpack"}.get(
                data.get("classId")
            )
            if not kind:
                raise SourceError("unsupported_resource")
            description = await self.get(
                base + "/mods/" + identity + "/description", headers
            )
            title, summary, body, format = (
                data.get("name", ""),
                data.get("summary", ""),
                description.get("data"),
                "html",
            )
            url = (
                "https://www.curseforge.com/minecraft/"
                + {
                    "mod": "mc-mods",
                    "resourcepack": "texture-packs",
                    "shader": "shaders",
                    "modpack": "modpacks",
                }[kind]
                + "/"
                + str(data.get("slug", identity))
            )
            files = await self.optional(
                base + "/mods/" + identity + "/files?pageSize=12&index=0", headers
            )
            release_data = files.get("data") if isinstance(files, dict) else None
            metadata = curseforge_metadata(
                data, release_data if isinstance(release_data, list) else []
            )
            metadata["metadata_complete"] = isinstance(release_data, list)
            await self.dependency_names(metadata, provider, headers)
        if (
            not all(isinstance(value, str) for value in (title, summary, body))
            or len(body) > 200000
            or len(title) > 512
            or len(summary) > 8192
        ):
            raise SourceError("invalid_source")
        fields = {}
        for name, text, fmt in (
            ("title", title, "plain"),
            ("summary", summary, "plain"),
            ("body", body, format),
        ):
            fields[name] = dict(
                text=text,
                format=fmt,
                source_language="und",
                source_hash=text_hash(text, name, fmt),
            )
        snapshot_hash = digest(
            dict(
                schema="mc-snapshot-v1",
                fields={name: f["source_hash"] for name, f in fields.items()},
            )
        )
        return dict(
            resource=resource_ref(provider, identity),
            kind=kind,
            source_url=url,
            metadata=metadata,
            fields=fields,
            snapshot_hash=snapshot_hash,
            metadata_hash=digest(dict(kind=kind, **metadata)),
        )

    async def optional(self, url, headers=None):
        try:
            return await self.get(url, headers)
        except SourceError:
            return None

    async def dependency_names(self, metadata, provider, headers=None):
        dependencies = [d for r in metadata["releases"] for d in r["dependencies"]]
        identities = list(
            dict.fromkeys(
                str(d["project_id"]) for d in dependencies if d.get("project_id")
            )
        )[:30]
        if not identities:
            return
        # This enrichment only runs in source workers. Public catalogue reads
        # never fan out to external providers, even when dependencies are absent.
        gate = asyncio.Semaphore(4)

        async def lookup(identity):
            try:
                resource_key(resource_ref(provider, identity))
            except ValueError:
                return identity, None
            async with gate:
                base = (
                    self.config.modrinth_url
                    if provider == "modrinth"
                    else self.config.curseforge_url
                )
                data = await self.optional(
                    base.rstrip("/")
                    + ("/project/" if provider == "modrinth" else "/mods/")
                    + identity,
                    headers,
                )
            if provider == "curseforge" and isinstance(data, dict):
                data = data.get("data")
            return identity, data

        projects = dict(await asyncio.gather(*(lookup(i) for i in identities)))
        for dependency in dependencies:
            data = projects.get(str(dependency.get("project_id")))
            if isinstance(data, dict):
                dependency["name"] = str(
                    data.get("title") or data.get("name") or dependency["name"]
                )[:512]
                if provider == "modrinth":
                    dependency["url"] = "https://modrinth.com/project/" + str(
                        dependency["project_id"]
                    )
                else:
                    # Parent data has been verified by the canonical API/gateway.
                    dependency["url"] = (
                        "https://www.curseforge.com/minecraft/mc-mods/"
                        + str(data.get("slug", dependency["project_id"]))
                    )
