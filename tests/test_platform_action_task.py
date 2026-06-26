from __future__ import annotations

from application import tasks as tasks_module
from domain.actions import ActionExecutionResult
from domain.actions import ActionExecutionCommand
from infrastructure import platform_runtime as runtime_module


class _FakeLogger:
    def __init__(self):
        self.events = []
        self.result_data = None
        self.finished = None
        self.cancel_requested = False

    def log(self, message, **kwargs):
        self.events.append(("log", message, kwargs))

    def record_error(self, error):
        self.events.append(("error", error, {}))

    def record_success(self):
        self.events.append(("success", "", {}))

    def set_result_data(self, data):
        self.result_data = data

    def set_progress(self, current, total):
        self.events.append(("progress", current, {"total": total}))

    def is_cancel_requested(self):
        return self.cancel_requested

    def set_subtask(self, subtask_id, label=""):
        self.events.append(("subtask", subtask_id, {"label": label}))

    def clear_subtask(self):
        self.events.append(("clear_subtask", "", {}))

    def finish(self, status, *, error=""):
        self.finished = (status, error)


def test_platform_action_task_passes_task_logger_to_runtime(monkeypatch):
    seen = {}

    class FakeRuntime:
        def execute_action(self, command, *, log_fn=None, cancel_check):
            seen["log_fn"] = log_fn
            seen["cancel_check"] = cancel_check
            if log_fn:
                log_fn("checkout step log")
            return ActionExecutionResult(ok=True, data={"message": "summary"})

    monkeypatch.setattr(tasks_module, "PlatformRuntime", FakeRuntime)
    logger = _FakeLogger()

    tasks_module._execute_platform_action_task(
        {
            "platform": "chatgpt",
            "account_id": 123,
            "action_id": "payment_link",
            "params": {"auto_checkout": "true"},
        },
        logger,
    )

    assert getattr(seen["log_fn"], "__self__", None) is logger
    assert getattr(seen["log_fn"], "__name__", "") == "log"
    assert getattr(seen["cancel_check"], "__self__", None) is logger
    assert getattr(seen["cancel_check"], "__name__", "") == "is_cancel_requested"
    assert seen["cancel_check"]() is False
    assert ("log", "checkout step log", {}) in logger.events
    assert logger.result_data == {"message": "summary"}
    assert logger.finished == (tasks_module.TASK_STATUS_SUCCEEDED, "")


def test_phone_bind_task_passes_logger_and_browser_mode(monkeypatch):
    seen = {}

    class FakePhoneBindingService:
        def bind(self, **kwargs):
            seen.update(kwargs)
            kwargs["log_fn"]("phone bind step")
            return {"success_count": 1, "failure_count": 0, "phones": []}

    monkeypatch.setattr(tasks_module, "PhoneBindingService", FakePhoneBindingService, raising=False)
    logger = _FakeLogger()

    tasks_module._execute_phone_bind_task(
        {
            "platform": "chatgpt",
            "ids": [123],
            "fallback_ids": [],
            "phone_lines": "7857019646----https://mail-api.yuecheng.shop/api/sms/recordText?key=abc",
            "browser_mode": "camoufox_headed",
            "bit_profile_id": "profile-1",
            "concurrency": 7,
        },
        logger,
    )

    assert seen["ids"] == [123]
    assert seen["browser_mode"] == "camoufox_headed"
    assert seen["bit_profile_id"] == "profile-1"
    assert seen["concurrency"] == 7
    assert getattr(seen["log_fn"], "__self__", None) is logger
    assert ("log", "phone bind step", {}) in logger.events
    assert logger.result_data == {"success_count": 1, "failure_count": 0, "phones": []}
    assert logger.finished == (tasks_module.TASK_STATUS_SUCCEEDED, "")


def test_codex_oauth_task_passes_logger_and_browser_mode(monkeypatch):
    seen = []

    class FakeCtfPlusAccountsService:
        def run_codex_oauth_browser(self, **kwargs):
            seen.append(kwargs)
            kwargs["log_fn"]("oauth step")
            return {"ok": True, "account_id": kwargs["account_id"], "email": "oauth@test.com"}

    monkeypatch.setattr(tasks_module, "CtfPlusAccountsService", FakeCtfPlusAccountsService, raising=False)
    logger = _FakeLogger()

    tasks_module._execute_codex_oauth_task(
        {
            "ids": [456],
            "browser_mode": "bitbrowser_hidden",
            "bit_profile_id": "profile-2",
            "concurrency": 9,
        },
        logger,
    )

    assert seen[0]["account_id"] == 456
    assert seen[0]["browser_mode"] == "bitbrowser_hidden"
    assert seen[0]["bit_profile_id"] == "profile-2"
    assert getattr(seen[0]["log_fn"], "__self__", None) is logger
    assert any(event[0] == "log" and event[1] == "oauth step" for event in logger.events)
    assert logger.result_data["success_count"] == 1
    assert logger.result_data["concurrency"] == 1
    assert logger.result_data["results"][0]["account_id"] == 456
    assert logger.finished == (tasks_module.TASK_STATUS_SUCCEEDED, "")


def test_codex_oauth_task_runs_multiple_accounts_without_capping_concurrency(monkeypatch):
    seen = []

    class FakeCtfPlusAccountsService:
        def run_codex_oauth_browser(self, **kwargs):
            seen.append(kwargs["account_id"])
            return {"ok": True, "account_id": kwargs["account_id"], "email": f"{kwargs['account_id']}@test.com"}

    monkeypatch.setattr(tasks_module, "CtfPlusAccountsService", FakeCtfPlusAccountsService, raising=False)
    logger = _FakeLogger()

    tasks_module._execute_codex_oauth_task(
        {
            "ids": [1, 2, 3],
            "browser_mode": "camoufox_headed",
            "concurrency": 99,
        },
        logger,
    )

    assert sorted(seen) == [1, 2, 3]
    assert logger.result_data["total"] == 3
    assert logger.result_data["success_count"] == 3
    assert logger.result_data["failure_count"] == 0
    assert logger.result_data["concurrency"] == 3
    assert logger.finished == (tasks_module.TASK_STATUS_SUCCEEDED, "")


def test_platform_action_task_finishes_cancelled_without_starting_runtime(monkeypatch):
    class FakeRuntime:
        def execute_action(self, *args, **kwargs):
            raise AssertionError("runtime should not start after cancellation")

    monkeypatch.setattr(tasks_module, "PlatformRuntime", FakeRuntime)
    logger = _FakeLogger()
    logger.cancel_requested = True

    tasks_module._execute_platform_action_task(
        {
            "platform": "chatgpt",
            "account_id": 123,
            "action_id": "payment_link",
            "params": {"auto_checkout": "true"},
        },
        logger,
    )

    assert logger.finished == (tasks_module.TASK_STATUS_CANCELLED, "任务已取消")


def test_platform_action_task_marks_cancelled_after_runtime_cancel(monkeypatch):
    class FakeRuntime:
        def execute_action(self, command, *, log_fn=None, cancel_check):
            assert cancel_check() is False
            logger.cancel_requested = True
            return ActionExecutionResult(ok=False, error="任务已取消")

    monkeypatch.setattr(tasks_module, "PlatformRuntime", FakeRuntime)
    logger = _FakeLogger()

    tasks_module._execute_platform_action_task(
        {
            "platform": "chatgpt",
            "account_id": 123,
            "action_id": "payment_link",
            "params": {"auto_checkout": "true"},
        },
        logger,
    )

    assert logger.finished == (tasks_module.TASK_STATUS_CANCELLED, "任务已取消")


def test_chatgpt_auto_plus_followup_generates_payment_link(monkeypatch):
    saved_accounts = []

    class FakeLogger(_FakeLogger):
        def add_cashier_url(self, url):
            self.events.append(("cashier_url", url, {}))

    class FakeAccount:
        platform = "chatgpt"
        email = "ctf@example.com"
        password = "Secret123!"
        extra = {"access_token": "access-token"}

    class FakePlatform:
        def __init__(self):
            self.calls = []

        def execute_action(self, action_id, account, params):
            self.calls.append((action_id, params))
            return {
                "ok": True,
                "data": {
                    "cashier_url": "https://checkout.example/plus",
                    "checkout_url": "https://checkout.example/plus",
                    "message": "Payment link generated.",
                },
            }

    monkeypatch.setattr(tasks_module, "save_account", lambda account: saved_accounts.append(dict(account.extra)))
    logger = FakeLogger()
    platform = FakePlatform()
    account = FakeAccount()

    error = tasks_module._auto_followup_chatgpt_plus_payment(
        platform_name="chatgpt",
        payload={
            "extra": {
                "auto_chatgpt_plus_payment": True,
                "chatgpt_payment": {
                    "country": "US",
                    "currency": "USD",
                    "headless": "true",
                    "checkout_hold_seconds": 0,
                },
            }
        },
        platform=platform,
        account=account,
        logger=logger,
    )

    assert error == ""
    assert platform.calls == [
        (
            "payment_link",
            {
                "plan": "plus",
                "country": "US",
                "currency": "USD",
                "auto_checkout": "true",
                "payment_method": "paypal",
                "headless": "true",
                "checkout_timeout": 180,
                "checkout_hold_seconds": 0,
            },
        )
    ]
    assert account.extra["cashier_url"] == "https://checkout.example/plus"
    assert saved_accounts[-1]["cashier_url"] == "https://checkout.example/plus"
    assert ("cashier_url", "https://checkout.example/plus", {}) in logger.events
    assert account.status == tasks_module.AccountStatus.SUBSCRIBED
    assert account.extra["account_overview"]["plan_state"] == "subscribed"
    assert account.extra["account_overview"]["plan_name"] == "Plus"
    assert "Plus" in account.extra["account_overview"]["chips"]


def test_chatgpt_auto_plus_followup_forwards_checkout_mode_and_record_har(monkeypatch):
    class FakeAccount:
        platform = "chatgpt"
        email = "ctf@example.com"
        password = "Secret123!"
        extra = {"access_token": "access-token"}

    class FakeLogger(_FakeLogger):
        def add_cashier_url(self, url):
            self.events.append(("cashier_url", url, {}))

    class FakePlatform:
        def __init__(self):
            self.calls = []

        def execute_action(self, action_id, account, params):
            self.calls.append((action_id, dict(params)))
            return {"ok": True, "data": {"cashier_url": "https://checkout.example/plus"}}

    monkeypatch.setattr(tasks_module, "save_account", lambda account: None)
    logger = FakeLogger()
    platform = FakePlatform()
    account = FakeAccount()

    tasks_module._auto_followup_chatgpt_plus_payment(
        platform_name="chatgpt",
        payload={
            "extra": {
                "auto_chatgpt_plus_payment": True,
                "chatgpt_payment": {
                    "country": "US",
                    "currency": "USD",
                    "headless": "false",
                    "checkout_mode": "camoufox_headed",
                    "record_har": "true",
                },
            }
        },
        platform=platform,
        account=account,
        logger=logger,
    )

    assert len(platform.calls) == 1
    forwarded = platform.calls[0][1]
    assert forwarded["checkout_mode"] == "camoufox_headed"
    assert forwarded["record_har"] == "true"


def test_chatgpt_auto_plus_followup_omits_unset_checkout_mode_and_record_har(monkeypatch):
    class FakeAccount:
        platform = "chatgpt"
        email = "ctf@example.com"
        password = "Secret123!"
        extra = {}

    class FakePlatform:
        def __init__(self):
            self.calls = []

        def execute_action(self, action_id, account, params):
            self.calls.append((action_id, dict(params)))
            return {"ok": True, "data": {}}

    monkeypatch.setattr(tasks_module, "save_account", lambda account: None)
    logger = _FakeLogger()
    platform = FakePlatform()
    account = FakeAccount()

    tasks_module._auto_followup_chatgpt_plus_payment(
        platform_name="chatgpt",
        payload={
            "extra": {
                "auto_chatgpt_plus_payment": True,
                "chatgpt_payment": {"country": "US", "currency": "USD"},
            }
        },
        platform=platform,
        account=account,
        logger=logger,
    )

    forwarded = platform.calls[0][1]
    assert "checkout_mode" not in forwarded
    assert "record_har" not in forwarded


def test_chatgpt_auto_plus_followup_returns_error_when_payment_link_fails(monkeypatch):
    class FakeLogger(_FakeLogger):
        def add_cashier_url(self, url):
            self.events.append(("cashier_url", url, {}))

    class FakeAccount:
        platform = "chatgpt"
        email = "ctf@example.com"
        password = "Secret123!"
        extra = {}

    class FakePlatform:
        def execute_action(self, action_id, account, params):
            return {
                "ok": False,
                "error": "checkout failed",
                "data": {
                    "cashier_url": "https://checkout.example/partial",
                },
            }

    saved_accounts = []
    monkeypatch.setattr(tasks_module, "save_account", lambda account: saved_accounts.append(dict(account.extra)))
    logger = FakeLogger()
    account = FakeAccount()

    error = tasks_module._auto_followup_chatgpt_plus_payment(
        platform_name="chatgpt",
        payload={"extra": {"auto_chatgpt_plus_payment": True}},
        platform=FakePlatform(),
        account=account,
        logger=logger,
    )

    assert error == "ChatGPT Plus 支付链接生成失败: checkout failed"
    assert account.extra["cashier_url"] == "https://checkout.example/partial"
    assert saved_accounts[-1]["cashier_url"] == "https://checkout.example/partial"
    assert ("cashier_url", "https://checkout.example/partial", {}) in logger.events


def test_platform_runtime_wires_log_fn_to_platform(monkeypatch):
    logs = []
    seen = {}

    class FakeSession:
        def __init__(self, engine):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, model_cls, account_id):
            return type("Model", (), {"platform": "chatgpt"})()

    class FakePlatform:
        def __init__(self, config=None):
            self._log_fn = print

        def set_logger(self, logger):
            self._log_fn = logger

        def set_cancel_checker(self, checker):
            seen["cancel_check"] = checker

        def execute_action(self, action_id, account, params):
            self._log_fn("runtime platform log")
            assert self.is_cancel_requested() is False
            return {"ok": True, "data": {"message": "ok"}}

        def is_cancel_requested(self):
            return seen["cancel_check"]()

    monkeypatch.setattr(runtime_module, "Session", FakeSession)
    monkeypatch.setattr(runtime_module, "load_all", lambda: None)
    monkeypatch.setattr(runtime_module, "get", lambda platform: FakePlatform)
    monkeypatch.setattr(runtime_module, "build_platform_account", lambda session, model: object())

    result = runtime_module.PlatformRuntime().execute_action(
        ActionExecutionCommand(
            platform="chatgpt",
            account_id=123,
            action_id="payment_link",
            params={"auto_checkout": "true"},
        ),
        log_fn=logs.append,
        cancel_check=lambda: False,
    )

    assert result.ok is True
    assert logs == ["runtime platform log"]
    assert seen["cancel_check"]() is False


def test_platform_runtime_persists_cashier_url_even_when_action_fails_after_link(monkeypatch):
    patched = {}

    class FakeSession:
        def __init__(self, engine):
            self.added = []
            self.committed = False

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, model_cls, account_id):
            return type("Model", (), {"id": account_id, "platform": "chatgpt", "updated_at": None})()

        def add(self, model):
            self.added.append(model)

        def commit(self):
            self.committed = True

    class FakePlatform:
        def __init__(self, config=None):
            pass

        def execute_action(self, action_id, account, params):
            return {
                "ok": False,
                "error": "checkout failed",
                "data": {
                    "cashier_url": "https://checkout.stripe.com/c/pay/cs_test_link",
                    "message": "Payment link generated, but checkout failed.",
                },
            }

    def fake_patch_account_graph(session, model, **kwargs):
        patched.update(kwargs)

    monkeypatch.setattr(runtime_module, "Session", FakeSession)
    monkeypatch.setattr(runtime_module, "load_all", lambda: None)
    monkeypatch.setattr(runtime_module, "get", lambda platform: FakePlatform)
    monkeypatch.setattr(runtime_module, "build_platform_account", lambda session, model: object())
    monkeypatch.setattr(runtime_module, "patch_account_graph", fake_patch_account_graph)

    result = runtime_module.PlatformRuntime().execute_action(
        ActionExecutionCommand(
            platform="chatgpt",
            account_id=123,
            action_id="payment_link",
            params={"auto_checkout": "true"},
        )
    )

    assert result.ok is False
    assert result.error == "checkout failed"
    assert patched["cashier_url"] == "https://checkout.stripe.com/c/pay/cs_test_link"
    assert patched["summary_updates"]["cashier_url"] == "https://checkout.stripe.com/c/pay/cs_test_link"
