from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.base_platform import RegisterConfig
from platforms.chatgpt import browser_register as browser_register_module
from platforms.chatgpt.plugin import (
    ChatGPTPlatform,
    _assert_complete_oauth_callback,
    _generate_chatgpt_registration_password,
)


class _SessionApiResponse:
    status = 200

    def text(self):
        return (
            '{"accessToken":"at_123","user":{"email":"user@example.com"},'
            '"expires":"2026-05-20T12:00:00Z"}'
        )


class _UnreadableSessionApiResponse:
    status = 200

    def text(self):
        raise RuntimeError("Response body is unavailable for redirect responses")


def test_assert_complete_oauth_callback_accepts_complete_payload():
    _assert_complete_oauth_callback({
        "account_id": "acct_123",
        "access_token": "at_123",
        "refresh_token": "rt_123",
        "id_token": "id_123",
    })


def test_assert_complete_oauth_callback_rejects_partial_payload():
    with pytest.raises(RuntimeError, match="OAuth callback"):
        _assert_complete_oauth_callback({
            "account_id": "acct_123",
            "access_token": "",
            "refresh_token": "",
            "id_token": "",
        })


def test_generate_chatgpt_registration_password_meets_openai_strength_requirements():
    for _ in range(8):
        password = _generate_chatgpt_registration_password()
        assert len(password) >= 12
        assert any(ch.islower() for ch in password)
        assert any(ch.isupper() for ch in password)
        assert any(ch.isdigit() for ch in password)
        assert any(ch in ",._!@#" for ch in password)


def test_auth_timeout_retry_text_detects_openai_retry_page():
    text = "Oops, an error occurred! Operation timed out Try again Terms of Use"

    assert browser_register_module._is_auth_timeout_retry_text(text) is True


def test_auth_timeout_retry_text_ignores_plain_try_again_copy():
    assert browser_register_module._is_auth_timeout_retry_text("Try again later") is False


def test_chatgpt_platform_preserves_user_supplied_password():
    platform = object.__new__(ChatGPTPlatform)
    assert platform._prepare_registration_password("Secret123!") == "Secret123!"


def test_protocol_mailbox_mapper_rejects_partial_oauth_result():
    platform = object.__new__(ChatGPTPlatform)
    platform.mailbox = None
    platform.config = RegisterConfig()
    adapter = ChatGPTPlatform.build_protocol_mailbox_adapter(platform)
    ctx = SimpleNamespace(password="Secret123!", proxy=None, log=lambda message: None)
    result = SimpleNamespace(
        email="user@example.com",
        password="Secret123!",
        account_id="acct_123",
        access_token="",
        refresh_token="",
        id_token="",
        session_token="sess_123",
        workspace_id="",
    )

    with pytest.raises(RuntimeError, match="OAuth callback"):
        adapter.result_mapper(ctx, result)


def test_browser_registration_mapper_accepts_completed_registration_without_codex_tokens():
    platform = object.__new__(ChatGPTPlatform)

    mapped = platform._map_chatgpt_result({
        "email": "user@example.com",
        "password": "Secret123!",
        "account_id": "",
        "access_token": "",
        "refresh_token": "",
        "id_token": "",
        "session_token": "",
        "workspace_id": "",
        "cookies": "{\"login_session\":\"yes\"}",
        "profile": {},
    })

    assert mapped.email == "user@example.com"
    assert mapped.password == "Secret123!"
    assert mapped.user_id == ""
    assert mapped.token == ""
    assert mapped.extra["access_token"] == ""
    assert mapped.extra["cookies"] == "{\"login_session\":\"yes\"}"


def test_browser_oauth_adapter_still_requires_complete_oauth_result():
    platform = object.__new__(ChatGPTPlatform)
    adapter = ChatGPTPlatform.build_browser_registration_adapter(platform)
    ctx = SimpleNamespace(identity=SimpleNamespace(identity_provider="oauth_browser"))

    with pytest.raises(RuntimeError, match="OAuth callback"):
        adapter.result_mapper(ctx, {
            "email": "user@example.com",
            "account_id": "",
            "access_token": "",
        })


def test_fetch_chatgpt_session_opens_session_api_directly():
    calls = []

    class FakePage:
        context = SimpleNamespace(cookies=lambda: [
            {"name": "__Secure-next-auth.session-token", "value": "sess_123"},
            {"name": "oai-did", "value": "did_123"},
        ])

        def goto(self, url, **kwargs):
            calls.append((url, kwargs))
            return _SessionApiResponse()

    logs = []

    result = browser_register_module._fetch_chatgpt_session_from_page(
        FakePage(),
        {"login_session": "yes"},
        logs.append,
        timeout=5,
    )

    assert calls[0][0] == "https://chatgpt.com/api/auth/session"
    assert "chatgpt.com/api/auth/session" in logs[0]
    assert result["access_token"] == "at_123"
    assert result["session_token"] == "sess_123"
    assert result["cookies"] == "login_session=yes; __Secure-next-auth.session-token=sess_123; oai-did=did_123"


def test_fetch_chatgpt_session_uses_same_origin_fetch_before_navigation():
    calls = {"evaluate": 0, "goto": 0}

    class FakePage:
        url = "https://chatgpt.com/"
        context = SimpleNamespace(cookies=lambda: [
            {"name": "__Secure-next-auth.session-token", "value": "sess_123"},
        ])

        def evaluate(self, script, arg=None):
            calls["evaluate"] += 1
            assert arg == "https://chatgpt.com/api/auth/session"
            return {
                "status": 200,
                "url": "https://chatgpt.com/api/auth/session",
                "text": (
                    '{"accessToken":"at_fetch","user":{"email":"user@example.com"},'
                    '"expires":"2026-05-20T12:00:00Z"}'
                ),
            }

        def goto(self, url, **kwargs):
            calls["goto"] += 1
            raise AssertionError("same-origin session fetch should avoid navigation")

    result = browser_register_module._fetch_chatgpt_session_from_page(
        FakePage(),
        {},
        lambda message: None,
        timeout=5,
    )

    assert calls == {"evaluate": 1, "goto": 0}
    assert result["access_token"] == "at_fetch"
    assert result["session_token"] == "sess_123"


def test_fetch_chatgpt_session_falls_back_to_page_body_when_response_text_unavailable(monkeypatch):
    times = iter([100.0, 101.0, 106.0])
    monkeypatch.setattr(browser_register_module.time, "time", lambda: next(times))
    monkeypatch.setattr(browser_register_module.time, "sleep", lambda seconds: None)

    class FakeBody:
        def inner_text(self, timeout=3000):
            return (
                '{"accessToken":"at_from_body","user":{"email":"user@example.com"},'
                '"expires":"2026-05-20T12:00:00Z"}'
            )

    class FakePage:
        url = "https://chatgpt.com/api/auth/session"
        context = SimpleNamespace(cookies=lambda: [
            {"name": "__Secure-next-auth.session-token", "value": "sess_123"},
        ])

        def goto(self, url, **kwargs):
            self.url = url
            return _UnreadableSessionApiResponse()

        def locator(self, selector):
            assert selector == "body"
            return FakeBody()

    result = browser_register_module._fetch_chatgpt_session_from_page(
        FakePage(),
        {},
        lambda message: None,
        timeout=5,
    )

    assert result["access_token"] == "at_from_body"
    assert result["session_token"] == "sess_123"


def test_browser_registration_flow_starts_from_chatgpt_nextauth(monkeypatch):
    calls = {}

    class FakePage:
        url = "about:blank"
        context = SimpleNamespace(cookies=lambda: [
            {"name": "login_session", "value": "yes"},
        ])

        def evaluate(self, script, *args):
            return "Mozilla/5.0"

    def start_via_authorize(page, email, device_id, log):
        calls["authorize"] = (email, device_id)
        page.url = "https://chatgpt.com/api/auth/callback/openai?code=abc"
        return {"page_type": "oauth_callback", "current_url": page.url}

    def fail_via_page(*args, **kwargs):
        calls["page"] = True
        raise AssertionError("browser registration should start from ChatGPT NextAuth")

    monkeypatch.setattr(browser_register_module, "_seed_browser_device_id", lambda page, device_id: calls.setdefault("seed", device_id))
    monkeypatch.setattr(browser_register_module, "_start_browser_signup_via_authorize", start_via_authorize)
    monkeypatch.setattr(browser_register_module, "_start_browser_signup_via_page", fail_via_page)
    monkeypatch.setattr(browser_register_module, "_handle_post_signup_onboarding", lambda page, log: None)

    state = browser_register_module._browser_registration_flow(
        FakePage(),
        "user@example.com",
        "Secret123!",
        otp_callback=None,
        phone_callback=None,
        log=lambda message: None,
    )

    assert calls["authorize"][0] == "user@example.com"
    assert calls["authorize"][1] == calls["seed"]
    assert "page" not in calls
    assert state["page_type"] == "oauth_callback"


def test_browser_register_run_returns_after_registration_without_codex_oauth(monkeypatch):
    class FakePage:
        def __init__(self):
            self.url = "about:blank"
            self.context = SimpleNamespace(cookies=lambda: [])

        def goto(self, url, **kwargs):
            self.url = url

    class FakeBrowser:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def new_page(self):
            return FakePage()

    called = {"oauth": False}

    def fail_if_oauth_runs(self, email, password):
        called["oauth"] = True
        raise AssertionError("Codex OAuth should not run after browser registration")

    monkeypatch.setattr(browser_register_module, "Camoufox", lambda **kwargs: FakeBrowser())
    monkeypatch.setattr(browser_register_module, "_browser_registration_flow", lambda *args, **kwargs: {"page_type": "oauth_callback"})
    monkeypatch.setattr(browser_register_module, "_click_first", lambda page, selectors, timeout=3: setattr(page, "url", "https://auth.openai.com/log-in") or selectors[0])
    monkeypatch.setattr(
        browser_register_module,
        "_get_cookies",
        lambda page: {"login_session": "yes", "__Secure-next-auth.session-token": "sess_123"},
    )
    monkeypatch.setattr(
        browser_register_module,
        "_fetch_chatgpt_session_from_page",
        lambda page, cookies, log: {
            "access_token": "at_123",
            "refresh_token": "",
            "id_token": "",
            "session_token": "sess_123",
            "account_id": "acct_123",
            "workspace_id": "",
            "profile": {"email": "user@example.com"},
            "expires_at": "2026-05-20T12:00:00Z",
            "cookies": "__Secure-next-auth.session-token=sess_123; login_session=yes",
        },
        raising=False,
    )
    monkeypatch.setattr(browser_register_module, "_do_codex_oauth", lambda *args, **kwargs: None)
    monkeypatch.setattr(browser_register_module.ChatGPTBrowserRegister, "_retry_oauth_fresh_browser", fail_if_oauth_runs)
    monkeypatch.setattr(browser_register_module.time, "sleep", lambda seconds: None)

    worker = browser_register_module.ChatGPTBrowserRegister(
        headless=True,
        proxy=None,
        otp_callback=None,
        log_fn=lambda message: None,
    )

    result = worker.run(email="user@example.com", password="Secret123!")

    assert called["oauth"] is False
    assert result["email"] == "user@example.com"
    assert result["password"] == "Secret123!"
    assert result["access_token"] == "at_123"
    assert result["account_id"] == "acct_123"
    assert result["session_token"] == "sess_123"
    assert result["cookies"] == "__Secure-next-auth.session-token=sess_123; login_session=yes"
