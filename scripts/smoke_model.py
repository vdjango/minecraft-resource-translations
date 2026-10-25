"""Explicit paid integration check. Reads ignored .env, never prints credentials."""

import asyncio
import json
import time

from aiohttp import ClientSession

from mc_translate.config import Config, load_env
from mc_translate.content import assemble, prepare
from mc_translate.model import ModelError, OpenAIModel
from mc_translate.store import Store


async def main():
    load_env()
    config = Config.from_env()
    store = await Store(config.database).open()
    try:
        async with ClientSession() as session:
            model = OpenAIModel(config, session, store)
            text = "A modern rendering optimization mod for Minecraft. Requires Fabric Loader 0.16 or newer.\n\nInstall the `.jar` in your mods folder. [Documentation](https://example.com/docs)"
            segments, template, placeholders = prepare(
                text, "markdown", "Rendering Example"
            )
            started = time.monotonic()
            try:
                result = await model.translate(segments, "zh-Hans")
            except ModelError as exc:
                print(
                    json.dumps(
                        dict(
                            ok=False,
                            code=exc.code,
                            elapsed=round(time.monotonic() - started, 2),
                        )
                    )
                )
                raise SystemExit(1)
            translated = assemble(template, result, placeholders)
            assert any("\u4e00" <= c <= "\u9fff" for c in translated), (
                "No Chinese output"
            )
            assert (
                "0.16" in translated
                and "`.jar`" in translated
                and "[Documentation](https://example.com/docs)" in translated
            )
            print(
                json.dumps(
                    dict(
                        ok=True,
                        model=config.model,
                        reasoning_effort=config.reasoning_effort,
                        elapsed=round(time.monotonic() - started, 2),
                        text=translated,
                    ),
                    ensure_ascii=False,
                )
            )
    finally:
        await store.close()


asyncio.run(main())
