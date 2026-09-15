"""Contract tests for the shared-process web boundary.

These tests use a deliberately tiny manager double.  They assert the security
properties of the HTTP boundary without starting iLink, systemd, or a bot.
"""
import re
import unittest
import asyncio

from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase

from shared_web import build_web_app


class FakeState:
    def __init__(self):
        self.current_qr_png = b"png"
        self.status = "qr_pending"
        self.qr_seq = 1
        self.verify_prompt = "code"
        self.submitted = []

    def submit_verify_code(self, code):
        self.submitted.append(code)

    def to_state_dict(self):
        return {"status": self.status, "qr_seq": self.qr_seq,
                "has_qr_png": True, "verify_prompt": self.verify_prompt}


class FakeSession:
    def __init__(self):
        self.qr_state = FakeState()
        self.relogin_count = 0

    async def request_relogin(self, reason):
        self.relogin_count += 1


class FakeManager:
    def __init__(self):
        self.sessions = {}

    async def get_or_create(self, user_id, config=None):
        return self.sessions.setdefault(user_id, FakeSession())

    def get(self, user_id):
        return self.sessions.get(user_id)

    async def stop(self, user_id):
        self.sessions.pop(user_id, None)

    async def touch(self, user_id):
        return None


class BlockingManager(FakeManager):
    def __init__(self):
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def get_or_create(self, user_id, config=None):
        self.started.set()
        await self.release.wait()
        return self.sessions.setdefault(user_id, FakeSession())


class SharedWebTests(AioHTTPTestCase):
    async def get_application(self):
        self.manager = FakeManager()
        return build_web_app(self.manager, prefix="/clawbot",
                             config={"rate_limit": 1, "rate_window": 60,
                                     "ephemeral_limit": 2})

    def _cookie(self, response):
        value = response.headers["Set-Cookie"]
        return re.search(r"clawbot_session=([^;]+)", value).group(1)

    async def test_raw_user_id_cookie_cannot_cross_tenant(self):
        response = await self.client.get("/clawbot/state",
                                         headers={"Cookie": "clawbot_session=alice"})
        self.assertEqual(response.status, 401)

        response = await self.client.post("/clawbot/ephemeral/start",
                                          allow_redirects=False)
        self.assertEqual(response.status, 302)
        sid = self._cookie(response)
        self.assertNotEqual(sid, next(iter(self.manager.sessions)))
        self.assertNotIn("Secure", response.headers["Set-Cookie"])
        self.assertIn("HttpOnly", response.headers["Set-Cookie"])
        self.assertIn("SameSite=Lax", response.headers["Set-Cookie"])

        response = await self.client.get("/clawbot/state",
                                         headers={"Cookie": f"clawbot_session={sid}"})
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())["status"], "qr_pending")

    async def test_ephemeral_rate_limit_and_prefix(self):
        response = await self.client.get("/clawbot/")
        self.assertEqual(response.status, 200)
        self.assertIn("/clawbot/ephemeral/start", await response.text())
        response = await self.client.post("/clawbot/ephemeral/start",
                                          allow_redirects=False)
        self.assertEqual(response.status, 302)
        response = await self.client.post("/clawbot/ephemeral/start",
                                          allow_redirects=False)
        self.assertEqual(response.status, 429)

    async def test_trusted_https_proxy_sets_secure_cookie_and_rejects_prefix_xss(self):
        manager = FakeManager()
        app = build_web_app(
            manager, prefix="/clawbot",
            config={"rate_limit": 5, "trust_proxy": True},
        )
        from aiohttp.test_utils import TestServer, TestClient
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            response = await client.post(
                "/clawbot/ephemeral/start",
                headers={"X-Forwarded-Proto": "https"},
                allow_redirects=False,
            )
            self.assertIn("Secure", response.headers["Set-Cookie"])
            response = await client.get(
                "/clawbot/",
                headers={"X-Forwarded-Prefix": "/x</script><script>alert(1)</script>"},
            )
            self.assertNotIn("alert(1)", await response.text())
        finally:
            await client.close()

    async def test_switch_and_verify_require_csrf(self):
        response = await self.client.post("/clawbot/ephemeral/start",
                                          allow_redirects=False)
        sid = self._cookie(response)
        cookie = {"Cookie": f"clawbot_session={sid}"}
        response = await self.client.post("/clawbot/switch", headers=cookie)
        self.assertEqual(response.status, 403)
        response = await self.client.post("/clawbot/verify_code", headers=cookie,
                                          json={"code": "1234"})
        self.assertEqual(response.status, 403)

    async def test_qr_blocking_manager_does_not_block_cookie_response(self):
        manager = BlockingManager()
        app = build_web_app(manager, prefix="/clawbot", config={"rate_limit": 5})
        from aiohttp.test_utils import TestServer, TestClient
        server = TestServer(app)
        client = TestClient(server)
        await client.start_server()
        try:
            response = await client.post("/clawbot/ephemeral/start", allow_redirects=False)
            self.assertEqual(response.status, 302)
            await asyncio.wait_for(manager.started.wait(), timeout=0.5)
            sid = re.search(r"clawbot_session=([^;]+)", response.headers["Set-Cookie"]).group(1)
            response = await client.get("/clawbot/state",
                                        headers={"Cookie": f"clawbot_session={sid}"})
            self.assertEqual(response.status, 202)
        finally:
            manager.release.set()
            await client.close()

    async def test_expired_ephemeral_binding_stops_manager_session(self):
        manager = FakeManager()
        app = build_web_app(manager, prefix="/clawbot",
                            config={"session_ttl": 0.1, "rate_limit": 5})
        from aiohttp.test_utils import TestServer, TestClient
        server = TestServer(app)
        client = TestClient(server)
        await client.start_server()
        try:
            response = await client.post("/clawbot/ephemeral/start", allow_redirects=False)
            self.assertEqual(response.status, 302)
            await asyncio.sleep(0.35)
            self.assertEqual(manager.sessions, {})
        finally:
            await client.close()


if __name__ == "__main__":
    unittest.main()
