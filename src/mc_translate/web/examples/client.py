"""Python 3.11+, standard library only. python client.py [project_id] [target_language]"""

import hashlib
import json
import os
import sys
import time
import urllib.request
import uuid

BASE = os.getenv("TRANSLATION_API", "https://translate.bugg.icu/v1").rstrip("/")


def call(path, data=None, headers=None):
    request = urllib.request.Request(
        BASE + "/" + path,
        data=None if data is None else json.dumps(data, ensure_ascii=False).encode(),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "Buggcraft-Translation-SDK/1.0 Python",
            **(headers or {}),
        },
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.load(response)


def text_hash(text, field="summary", format="plain"):
    envelope = dict(
        field=field,
        format=format,
        schema="mc-text-v1",
        source_language="und",
        text=text.replace("\r\n", "\n").replace("\r", "\n"),
    )
    raw = json.dumps(
        envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def main():
    identity = sys.argv[1] if len(sys.argv) > 1 else "AANobbMI"
    # Your launcher already has upstream text. This demo reads the verified copy.
    source = call("projects/modrinth/" + identity + "/source")
    source_hash = text_hash(source["fields"]["summary"]["text"])
    assert source_hash == source["fields"]["summary"]["source_hash"]
    payload = dict(
        target_language=sys.argv[2] if len(sys.argv) > 2 else "zh-Hans",
        profile="minecraft-v1",
        items=[
            dict(
                request_id="demo",
                resource=source["resource"],
                fields=[dict(field="summary", source_hash=source_hash)],
            )
        ],
    )
    ensured = False
    for _ in range(60):
        field = call("translations:resolve", payload)["results"][0]["fields"][0]
        update = field["update"]
        if update["action"] == "refresh_source":
            raise RuntimeError("Source changed: reload the upstream text and restart")
        if (
            field["state"] == "ready"
            and field["matches_request"]
            and field["is_current"]
        ):
            print(field["translation"]["text"])
            return
        print(update["notice"], file=sys.stderr)
        if field["state"] in ("failed", "blocked"):
            raise RuntimeError(field.get("error", field["state"]))
        if not ensured:
            token = call("sessions/anonymous", dict(client_id="python-example"))[
                "access_token"
            ]
            call(
                "translations:ensure",
                dict(payload, purpose="visible_list"),
                {
                    "Authorization": "Bearer " + token,
                    "Idempotency-Key": str(uuid.uuid4()),
                },
            )
            ensured = True
        time.sleep(2)
    raise TimeoutError(
        "Translation continues in background; show original and retry later"
    )


if __name__ == "__main__":
    main()
