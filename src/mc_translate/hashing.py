"""mc-text-v1: a restricted JCS envelope with ASCII keys and string values."""

import hashlib
import json
import re

HASH = re.compile(r"sha256:[0-9a-f]{64}\Z")
ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
FIELDS = ("title", "summary", "body", "changelog")


def canonical(value):
    # Our protocol uses ASCII keys, strings, bools and dictionaries only;
    # no floating point numbers or surrogate keys from the general JCS domain.
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def digest(value):
    return "sha256:" + hashlib.sha256(canonical(value)).hexdigest()


def text_hash(text, field="summary", format="plain", source_language="und"):
    if (
        not isinstance(text, str)
        or field not in FIELDS
        or format not in ("plain", "markdown", "html")
    ):
        raise ValueError("Invalid text envelope")
    return digest(
        dict(
            schema="mc-text-v1",
            field=field,
            source_language=source_language,
            format=format,
            text=text.replace("\r\n", "\n").replace("\r", "\n"),
        )
    )


def resource_key(resource):
    if not isinstance(resource, dict) or set(resource) != {
        "provider",
        "project_id",
        "entity_type",
        "entity_id",
    }:
        raise ValueError("Invalid resource reference")
    if (
        resource["provider"] not in ("modrinth", "curseforge")
        or resource["entity_type"] != "project"
    ):
        raise ValueError("Unsupported provider/entity")
    if any(
        not isinstance(resource[k], str) or not ID.fullmatch(resource[k])
        for k in ("project_id", "entity_id")
    ):
        raise ValueError("Invalid resource ID")
    if resource["entity_id"] != resource["project_id"]:
        raise ValueError("Project entity ID must equal project ID")
    if resource["provider"] == "curseforge" and not re.fullmatch(
        r"[1-9][0-9]{0,9}", resource["project_id"]
    ):
        raise ValueError("Invalid CurseForge project ID")
    return resource["provider"] + ":" + resource["project_id"]


def resource_ref(provider, project_id):
    return dict(
        provider=provider,
        project_id=str(project_id),
        entity_type="project",
        entity_id=str(project_id),
    )
