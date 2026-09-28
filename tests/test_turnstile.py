import asyncio

import httpx
import pytest

import main


CONFIG = {
    "TURNSTILE_SECRET_KEY": "test-secret",
    "PUBLIC_BASE_URL": "https://sdgj.tech",
}


class FakeClient:
    def __init__(self, result=None, error=None, **_kwargs):
        self.result = result
        self.error = error

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def post(self, _url, data):
        assert data["response"] == "token"
        if self.error:
            raise self.error
        return httpx.Response(
            200,
            json=self.result,
            request=httpx.Request("POST", "https://challenges.cloudflare.com"),
        )


def test_siteverify_checks_action_and_hostname(monkeypatch):
    monkeypatch.setattr(main.httpx, "AsyncClient", lambda **kwargs: FakeClient(
        {"success": True, "hostname": "sdgj.tech", "action": "mood-report"}, **kwargs
    ))
    assert asyncio.run(main.verify_turnstile(CONFIG, "token", "mood-report"))
    assert not asyncio.run(main.verify_turnstile(CONFIG, "token", "comment"))


def test_missing_token_and_outage_fail_closed(monkeypatch):
    assert not asyncio.run(main.verify_turnstile(CONFIG, "", "mood-report"))
    monkeypatch.setattr(main.httpx, "AsyncClient", lambda **kwargs: FakeClient(
        error=httpx.ConnectError("offline"), **kwargs
    ))
    with pytest.raises(main.TurnstileUnavailable):
        asyncio.run(main.verify_turnstile(CONFIG, "token", "mood-report"))
