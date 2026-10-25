import asyncio
import os
import unittest
from unittest.mock import patch
import httpx
from combat.planner import DeepSeekClient


class TestCancellableTransport(unittest.IsolatedAsyncioTestCase):
    async def test_cancellation_closes_active_request_without_worker_tail(self):
        entered = asyncio.Event(); released = asyncio.Event()
        async def slow(request):
            entered.set()
            try: await asyncio.sleep(60)
            finally: released.set()
        original = httpx.AsyncClient
        def factory(**kwargs): return original(transport=httpx.MockTransport(slow), **kwargs)
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "fixture", "DEEPSEEK_MODEL": "fixture"}), patch("combat.planner.httpx.AsyncClient", side_effect=factory):
            task = asyncio.create_task(DeepSeekClient().complete([]))
            await asyncio.wait_for(entered.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError): await task
            self.assertTrue(released.is_set())

    async def test_transient_retry_returns_valid_json(self):
        calls = []
        async def endpoint(request):
            calls.append(request)
            if len(calls) == 1: return httpx.Response(503)
            return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}], "usage": {}})
        original = httpx.AsyncClient
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "fixture", "DEEPSEEK_MODEL": "fixture"}), patch("combat.planner.httpx.AsyncClient", side_effect=lambda **kw: original(transport=httpx.MockTransport(endpoint), **kw)):
            content, usage = await DeepSeekClient().complete([])
        self.assertEqual(content, "{}"); self.assertEqual(len(calls), 2)
