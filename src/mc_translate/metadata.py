"""Bounded, provider-neutral project information. Dependencies belong to versions."""

import re
from urllib.parse import urlsplit


def https_url(value):
    if not isinstance(value, str) or len(value) > 2048:
        return None
    parsed = urlsplit(value)
    return (
        value
        if parsed.scheme == "https"
        and parsed.hostname
        and not parsed.username
        and not parsed.password
        else None
    )


def strings(values, limit=200):
    return list(dict.fromkeys(v[:160] for v in values if isinstance(v, str) and v))[
        :limit
    ]


def modrinth_metadata(data, members, versions):
    authors = [
        dict(
            id=str(m["user"]["id"]),
            name=str(m["user"].get("name") or m["user"].get("username") or ""),
            role=str(m.get("role", "")),
            url="https://modrinth.com/user/" + str(m["user"]["id"]),
        )
        for m in members
        if isinstance(m, dict)
        and isinstance(m.get("user"), dict)
        and m["user"].get("id")
    ][:50]
    releases = []
    for v in versions[:12]:
        if not isinstance(v, dict) or str(v.get("project_id")) != str(data["id"]):
            continue
        dependencies = []
        for d in v.get("dependencies", [])[:100]:
            if not isinstance(d, dict):
                continue
            dependencies.append(
                dict(
                    project_id=d.get("project_id"),
                    version_id=d.get("version_id"),
                    name=str(
                        d.get("file_name")
                        or d.get("project_id")
                        or d.get("version_id")
                        or "外部依赖"
                    )[:512],
                    relation=d.get("dependency_type", "required"),
                )
            )
        releases.append(
            dict(
                id=str(v["id"]),
                name=str(v.get("name", v["id"]))[:512],
                version_number=str(v.get("version_number", ""))[:160],
                published_at=v.get("date_published"),
                channel=v.get("version_type", "release"),
                game_versions=strings(v.get("game_versions", [])),
                loaders=strings(v.get("loaders", []), 32),
                dependencies=dependencies,
                url="https://modrinth.com/project/"
                + str(data["id"])
                + "/version/"
                + str(v["id"]),
            )
        )
    return dict(
        created_at=data.get("published"),
        updated_at=data.get("updated"),
        author_ids=[a["id"] for a in authors],
        team_id=data.get("team"),
        authors=authors,
        slug=data.get("slug", str(data["id"])),
        tags=strings(
            data.get("categories", []) + data.get("additional_categories", []), 30
        ),
        game_versions=strings(data.get("game_versions", [])),
        loaders=strings(data.get("loaders", []), 32),
        icon_url=https_url(data.get("icon_url")),
        downloads=data.get("downloads", 0),
        license=str((data.get("license") or {}).get("id", ""))[:160],
        releases=releases,
    )


def curseforge_metadata(data, files):
    loaders = {
        1: "forge",
        2: "cauldron",
        3: "liteloader",
        4: "fabric",
        5: "quilt",
        6: "neoforge",
    }
    relations = {
        1: "embedded",
        2: "optional",
        3: "required",
        4: "tool",
        5: "incompatible",
        6: "embedded",
    }
    authors = [
        dict(
            id=str(a["id"]),
            name=str(a.get("name", a["id"]))[:160],
            url=https_url(a.get("url")),
        )
        for a in data.get("authors", [])[:50]
        if isinstance(a, dict) and a.get("id")
    ]
    indexes = data.get("latestFilesIndexes", [])
    releases = []
    for f in files[:12]:
        if not isinstance(f, dict) or str(f.get("modId")) != str(data["id"]):
            continue
        versions = strings(f.get("gameVersions", []))
        dependencies = [
            dict(
                project_id=str(d["modId"]),
                version_id=None,
                name=str(d["modId"]),
                relation=relations.get(d.get("relationType"), "required"),
            )
            for d in f.get("dependencies", [])[:100]
            if isinstance(d, dict) and d.get("modId")
        ]
        releases.append(
            dict(
                id=str(f["id"]),
                name=str(f.get("displayName", f["id"]))[:512],
                version_number=str(f.get("fileName", ""))[:512],
                published_at=f.get("fileDate"),
                channel={1: "release", 2: "beta", 3: "alpha"}.get(
                    f.get("releaseType"), "release"
                ),
                game_versions=[v for v in versions if re.match(r"^\d", v)],
                loaders=[
                    v.lower() for v in versions if v.lower() in set(loaders.values())
                ],
                dependencies=dependencies,
                url="https://www.curseforge.com/minecraft/"
                + {
                    6: "mc-mods",
                    12: "texture-packs",
                    6552: "shaders",
                    4471: "modpacks",
                }[data["classId"]]
                + "/"
                + str(data.get("slug", data["id"]))
                + "/files/"
                + str(f["id"]),
            )
        )
    return dict(
        created_at=data.get("dateCreated"),
        updated_at=data.get("dateModified"),
        author_ids=[a["id"] for a in authors],
        authors=authors,
        slug=data.get("slug", str(data["id"])),
        tags=strings(
            [c.get("name") for c in data.get("categories", []) if isinstance(c, dict)],
            30,
        ),
        game_versions=strings(
            [i.get("gameVersion") for i in indexes if isinstance(i, dict)]
            + [v for r in releases for v in r["game_versions"]]
        ),
        loaders=strings(
            [
                loaders[i["modLoader"]]
                for i in indexes
                if isinstance(i, dict) and i.get("modLoader") in loaders
            ]
            + [v for r in releases for v in r["loaders"]],
            32,
        ),
        icon_url=https_url(
            (data.get("logo") or {}).get("thumbnailUrl")
            or (data.get("logo") or {}).get("url")
        ),
        downloads=data.get("downloadCount", 0),
        license="",
        releases=releases,
    )


def catalogue_metadata(metadata):
    """Exclude version payloads from list queries and responses."""
    value = {
        k: v
        for k, v in metadata.items()
        if k not in ("releases", "slug", "author_ids", "team_id")
    }
    releases = metadata.get("releases", [])
    value["dependency_count"] = len(releases[0]["dependencies"]) if releases else None
    return value
