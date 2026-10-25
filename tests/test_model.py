import json
import tempfile
import unittest
from pathlib import Path

from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer

from mc_translate.config import Config
from mc_translate.content import prepare
from mc_translate.hashing import canonical, digest, text_hash
from mc_translate.model import ModelError, OpenAIModel
from mc_translate.store import Store


class ModelTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = await Store(str(Path(self.temp.name) / "model.sqlite")).open()
        self.config = Config(
            api_key="private-test-key", model="gpt-6.1-sol", reasoning_effort="medium"
        )
        self.requests = []
        self.mode = "valid"

        async def upstream(request):
            self.assertEqual(
                request.headers["Authorization"], "Bearer private-test-key"
            )
            data = await request.json()
            self.requests.append(data)
            segments = json.loads(data["messages"][1]["content"])
            translations = [
                dict(id=s["id"], text="中文 " + s["text"]) for s in segments
            ]
            if self.mode == "bad_id":
                translations[0]["id"] = "invented"
            if self.mode == "broken_placeholder":
                translations[0]["text"] = "占位符被丢失"
            if self.mode == "429":
                return web.json_response({"error": "private-test-key"}, status=429)
            return web.json_response(
                dict(
                    choices=[
                        dict(
                            finish_reason="length"
                            if self.mode == "truncated"
                            else "stop",
                            message=dict(
                                content=json.dumps(
                                    dict(translations=translations), ensure_ascii=False
                                )
                            ),
                        )
                    ],
                    usage=dict(total_tokens=42),
                )
            )

        app = web.Application()
        app.router.add_post("/v1/chat/completions", upstream)
        self.server = TestServer(app)
        await self.server.start_server()
        self.config.base_url = str(self.server.make_url("/v1")).rstrip("/")
        self.session = ClientSession()
        self.model = OpenAIModel(self.config, self.session, self.store)

    async def asyncTearDown(self):
        await self.session.close()
        await self.server.close()
        await self.store.close()
        self.temp.cleanup()

    async def test_standard_openai_parameters_and_actual_usage(self):
        segments, _, _ = prepare("Install `fabric` for 1.21.", "markdown", "Example")
        translated = await self.model.translate(segments, "zh-Hans")
        self.assertIn("中文", translated[segments[0].id])
        request = self.requests[0]
        self.assertEqual(request["model"], "gpt-6.1-sol")
        self.assertEqual(request["reasoning_effort"], "medium")
        self.assertNotIn("temperature", request)
        self.assertNotIn("top_p", request)
        self.assertEqual(request["response_format"], {"type": "json_object"})
        usage = await self.store.one("SELECT tokens,actual FROM usage")
        self.assertEqual(usage, dict(tokens=42, actual=1))

    async def test_target_language_controls_prompt(self):
        segments, _, _ = prepare("Translate this paragraph.", "plain", "Example")
        for language, name in [('ja', 'Japanese'), ('zh-Hant', 'Traditional Chinese'), ('en-US', 'English')]:
            await self.model.translate(segments, language)
            self.assertIn('natural '+name, self.requests[-1]['messages'][0]['content'])
        count = len(self.requests)
        with self.assertRaises(ValueError):
            await self.model.translate(segments, 'zz')
        self.assertEqual(len(self.requests), count)

    async def test_invalid_outputs_not_publishable(self):
        segments, _, _ = prepare("Install `fabric` for 1.21.", "markdown", "Example")
        for mode, code in [
            ("bad_id", "invalid_model_output"),
            ("broken_placeholder", "invalid_model_output"),
            ("truncated", "model_incomplete"),
            ("429", "model_rate_limited"),
        ]:
            with self.subTest(mode=mode):
                self.mode = mode
                with self.assertRaises(ModelError) as raised:
                    await self.model.translate(segments, "zh-Hans")
                self.assertEqual(raised.exception.code, code)
                self.assertNotIn("private-test-key", str(raised.exception))
        # A request rejected before a billable success conservatively retains
        # the reservation; actual successful usage replaces the estimate.
        self.assertEqual(
            (await self.store.one("SELECT COUNT(*) AS n FROM usage WHERE actual=0"))[
                "n"
            ],
            1,
        )

    async def test_exhausted_budget_does_not_call_model(self):
        self.config.daily_token_limit = 1
        segments, _, _ = prepare("Translate this text.", "plain", "Example")
        with self.assertRaises(ModelError) as raised:
            await self.model.translate(segments, "zh-Hans")
        self.assertEqual(raised.exception.code, "budget_exhausted")
        self.assertEqual(self.requests, [])

    async def test_usage_cost_uses_cached_input_price_snapshot(self):
        self.config.model_prices = {"gpt-6.1-sol": {"input": 2, "cached_input": 0.5, "output": 8}}
        reservation = await self.store.reserve(500_000, self.config)
        await self.store.settle(
            reservation,
            dict(total_tokens=1_000_000, prompt_tokens=600_000, prompt_tokens_details={"cached_tokens": 100_000}, completion_tokens=400_000),
            self.config,
        )
        record = await self.store.one("SELECT tokens,input_tokens,cached_tokens,output_tokens,cost,details_known,price_snapshot FROM usage WHERE id=?", (reservation,))
        self.assertEqual(record["cost"], 4.25)
        self.assertEqual(record["details_known"], 1)
        self.assertEqual(json.loads(record["price_snapshot"])["cached_input"], 0.5)


class HashVectors(unittest.TestCase):
    def test_all_published_vectors(self):
        vectors = json.loads(
            (
                Path(__file__).parents[1]
                / "src/mc_translate/contracts/hash-vectors.json"
            ).read_text(encoding="utf-8")
        )
        for vector in vectors["vectors"]:
            with self.subTest(vector["name"]):
                self.assertEqual(text_hash(**vector["input"]), vector["source_hash"])
                obj = dict(schema="mc-text-v1", **vector["input"])
                obj["text"] = obj["text"].replace("\r\n", "\n").replace("\r", "\n")
                self.assertEqual(canonical(obj).decode(), vector["canonical_utf8"])
        self.assertEqual(
            digest(vectors["snapshot_vector"]["input"]),
            vectors["snapshot_vector"]["snapshot_hash"],
        )
        with self.assertRaises(UnicodeEncodeError):
            text_hash("\ud800")
