"""ChatGPT / Codex CLI 平台插件"""
import os
import re
import secrets
import threading
import time
from core.base_platform import BasePlatform, Account, AccountStatus, RegisterConfig
from core.base_mailbox import BaseMailbox
from core.registration import BrowserRegistrationAdapter, OtpSpec, ProtocolMailboxAdapter, ProtocolOAuthAdapter, RegistrationCapability, RegistrationResult
from core.registration.helpers import resolve_timeout
from core.registry import register
from core.proxy_pool import proxy_pool
from platforms._browser_backend import BrowserBackendConfig


def _result_text(result, key: str) -> str:
    if isinstance(result, dict):
        return str(result.get(key, "") or "")
    return str(getattr(result, key, "") or "")


def _assert_complete_oauth_callback(result) -> None:
    # NextAuth 流程只返回 account_id + access_token (+ session_token)
    # 传统 Codex CLI 流程返回全部 4 个字段
    required = ("account_id", "access_token")
    missing = [key for key in required if not _result_text(result, key)]
    if missing:
        raise RuntimeError(
            "ChatGPT 注册未完成完整 OAuth callback，缺少: " + ", ".join(missing)
        )


def _bool_param(params: dict, key: str, default: bool) -> bool:
    value = params.get(key)
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in {"0", "false", "no", "off", "否"}


def _int_param(params: dict, key: str, default: int) -> int:
    try:
        return int(params.get(key, default))
    except (TypeError, ValueError):
        return default


def _optional_int_param(params: dict, key: str) -> int | None:
    value = params.get(key)
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _mask_proxy(proxy: str | None) -> str:
    value = str(proxy or "").strip()
    if not value or "@" not in value:
        return value
    prefix, _, host = value.rpartition("@")
    scheme, sep, _credentials = prefix.partition("://")
    return f"{scheme}{sep}***@{host}" if sep else f"***@{host}"


def _build_checkout_har_path(email: str) -> str:
    """为 Camoufox checkout 生成 HAR 文件路径：tools/captures/checkout-<ts>-<email-slug>.har"""
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    capture_dir = os.path.join(project_root, "tools", "captures")
    os.makedirs(capture_dir, exist_ok=True)
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "_", str(email or "anon")).strip("_") or "anon"
    return os.path.join(capture_dir, f"checkout-{timestamp}-{slug}.har")


def _run_sync_checkout_isolated(checkout_fn, **kwargs):
    """把 checkout 函数丢进独立线程跑，避免阻塞外层 asyncio loop / 任务线程。

    **subtask 标签透传**：外层 ``logger.log`` 用 thread-local 标签把日志
    分组到对应的 worker（前端按这个折叠）。子线程是新线程，thread-local
    天然是空的，所以这里在父线程从 ``log_fn`` 上抠出当前绑定的
    subtask（如果是 ``TaskLogger.log``），子线程进去再 set 一遍，最后
    finally 清掉。
    """
    result_box = {}
    error_box = {}

    # 尝试从 log_fn 上抠出 TaskLogger 实例和当前 subtask（best-effort）
    log_fn = kwargs.get("log_fn")
    parent_logger = getattr(log_fn, "__self__", None)
    parent_subtask: tuple[str, str] | None = None
    if parent_logger is not None and hasattr(parent_logger, "_current_subtask"):
        try:
            parent_subtask = parent_logger._current_subtask()
        except Exception:
            parent_subtask = None

    def _target():
        # 把父线程的 subtask 标签复制到子线程的 thread-local，确保子线程里
        # 调 ``logger.log`` 也能正确分组。
        if parent_logger is not None and parent_subtask and parent_subtask[0]:
            try:
                parent_logger.set_subtask(parent_subtask[0], parent_subtask[1])
            except Exception:
                pass
        try:
            result_box["result"] = checkout_fn(**kwargs)
        except BaseException as exc:
            error_box["error"] = exc
        finally:
            if parent_logger is not None and parent_subtask and parent_subtask[0]:
                try:
                    parent_logger.clear_subtask()
                except Exception:
                    pass

    thread = threading.Thread(target=_target, name="chatgpt-paypal-checkout")
    thread.start()
    thread.join()
    if error_box:
        raise error_box["error"]
    return result_box.get("result")


def _generate_chatgpt_registration_password(length: int = 16) -> str:
    """生成更稳定通过 OpenAI 注册页校验的密码。

    旧协议流已经验证过：至少带小写、数字、符号时，成功率明显更稳。
    这里再补一个大写字符，避免浏览器流随机生成出“看起来够长但组合不够强”的密码。
    """
    specials = ",._!@#"
    minimum_length = 12
    size = max(int(length or minimum_length), minimum_length)
    required = [
        secrets.choice("abcdefghijklmnopqrstuvwxyz"),
        secrets.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ"),
        secrets.choice("0123456789"),
        secrets.choice(specials),
    ]
    pool = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789" + specials
    required.extend(secrets.choice(pool) for _ in range(size - len(required)))
    secrets.SystemRandom().shuffle(required)
    return "".join(required)


@register
class ChatGPTPlatform(BasePlatform):
    name = "chatgpt"
    display_name = "ChatGPT"
    version = "1.0.0"
    supported_executors = ["protocol", "headless", "headed"]
    supported_identity_modes = ["mailbox", "oauth_browser"]
    supported_oauth_providers = ["google", "microsoft"]
    protocol_captcha_order = ("yescaptcha_api", "twocaptcha_api", "local_solver")

    # Declarative capabilities
    capabilities = [
        "query_state",      # Query account state/quota
        "refresh_token",    # Refresh auth token
        "generate_link",    # Generate payment link
        "switch_desktop",   # Switch to Codex desktop
        "upload_cpa",       # Upload to CPA system
        "upload_tm",        # Upload to Team Manager
    ]

    def __init__(self, config: RegisterConfig = None, mailbox: BaseMailbox = None):
        super().__init__(config)
        self.mailbox = mailbox

    def check_valid(self, account: Account) -> bool:
        self._last_check_overview = {}
        try:
            from platforms.chatgpt.payment import fetch_subscription_status_details
            from core.proxy_pool import proxy_pool
            class _A: pass
            a = _A()
            extra = account.extra or {}
            a.access_token = extra.get("access_token") or account.token
            a.id_token = extra.get("id_token", "")
            a.cookies = extra.get("cookies", "")
            a.extra = extra

            region = str(getattr(account, "region", "") or extra.get("region", "") or "").strip()
            configured_proxy = self.config.proxy if self.config else None
            proxy_candidates: list[tuple[str | None, bool]] = []
            if configured_proxy:
                proxy_candidates.append((configured_proxy, False))
            else:
                pooled_proxy = proxy_pool.get_next(region=region)
                if pooled_proxy:
                    proxy_candidates.append((pooled_proxy, True))
            proxy_candidates.append((None, False))

            for proxy, should_report in proxy_candidates:
                try:
                    details = fetch_subscription_status_details(a, proxy=proxy)
                    if should_report and proxy:
                        proxy_pool.report_success(proxy)
                    status = details.get("status")
                    # 把订阅状态同步映射成前端能用的 plan_state / chips
                    # 来源（避免老 chips 还带 "Plus" 但实际已 free）。
                    if status == "plus":
                        plan_state = "subscribed"
                        chips = ["Plus"]
                    elif status == "team":
                        plan_state = "subscribed"
                        chips = ["Team"]
                    elif status == "free":
                        plan_state = "free"
                        chips = ["Free"]
                    elif status in ("expired", "invalid", "banned"):
                        plan_state = "expired"
                        chips = []
                    else:
                        plan_state = "unknown"
                        chips = []
                    overview = {
                        "plan": status,
                        "plan_name": status,
                        "plan_state": plan_state,
                        "chips": chips,
                        "check_source": details.get("source"),
                    }
                    if isinstance(details.get("usage"), dict):
                        overview["chatgpt_usage"] = details["usage"]
                    self._last_check_overview = overview
                    return status not in ("expired", "invalid", "banned", None)
                except Exception:
                    if should_report and proxy:
                        proxy_pool.report_fail(proxy)
                    continue
        except Exception:
            return False
        return False

    def get_last_check_overview(self) -> dict:
        return dict(getattr(self, "_last_check_overview", {}) or {})

    def _prepare_registration_password(self, password: str | None) -> str | None:
        if password:
            return password
        return _generate_chatgpt_registration_password()

    def _map_chatgpt_result(
        self,
        result: dict,
        *,
        password: str = "",
        user_id: str = "",
        require_oauth: bool = False,
    ) -> RegistrationResult:
        if require_oauth:
            _assert_complete_oauth_callback(result)
        return RegistrationResult(
            email=result.get("email", ""),
            password=password or result.get("password", ""),
            user_id=user_id or result.get("account_id", ""),
            token=result.get("access_token", ""),
            status=AccountStatus.REGISTERED,
            extra={
                "account_id": result.get("account_id", ""),
                "access_token": result.get("access_token", ""),
                "refresh_token": result.get("refresh_token", ""),
                "id_token": result.get("id_token", ""),
                "session_token": result.get("session_token", ""),
                "workspace_id": result.get("workspace_id", ""),
                "cookies": result.get("cookies", ""),
                "profile": result.get("profile", {}),
                "expires_at": result.get("expires_at", ""),
                "codex_oauth_status": result.get("codex_oauth_status", ""),
                "codex_oauth_error": result.get("codex_oauth_error", ""),
            },
        )

    def _run_protocol_oauth(self, ctx) -> dict:
        from platforms.chatgpt.browser_oauth import register_with_browser_oauth

        return register_with_browser_oauth(
            proxy=ctx.proxy,
            oauth_provider=ctx.identity.oauth_provider,
            email_hint=ctx.identity.email,
            timeout=resolve_timeout(ctx.extra, ("browser_oauth_timeout", "manual_oauth_timeout"), 300),
            log_fn=ctx.log,
            headless=(ctx.executor_type == "headless"),
            chrome_user_data_dir=ctx.identity.chrome_user_data_dir,
            chrome_cdp_url=ctx.identity.chrome_cdp_url,
        )

    def build_browser_registration_adapter(self):
        return BrowserRegistrationAdapter(
            result_mapper=lambda ctx, result: self._map_chatgpt_result(
                result,
                require_oauth=getattr(ctx.identity, "identity_provider", "") == "oauth_browser",
            ),
            browser_worker_builder=lambda ctx, artifacts: __import__("platforms.chatgpt.browser_register", fromlist=["ChatGPTBrowserRegister"]).ChatGPTBrowserRegister(
                headless=(ctx.executor_type == "headless"),
                proxy=ctx.proxy,
                otp_callback=artifacts.otp_callback,
                phone_callback=artifacts.phone_callback,
                log_fn=ctx.log,
                auto_codex_oauth=_bool_param(ctx.extra or {}, "auto_codex_oauth_after_register", True),
            ),
            browser_register_runner=lambda worker, ctx, artifacts: worker.run(
                email=ctx.identity.email or "",
                password=ctx.password or "",
            ),
            oauth_runner=self._run_protocol_oauth,
            capability=RegistrationCapability(oauth_headless_requires_browser_reuse=True),
            otp_spec=OtpSpec(wait_message="等待验证码...", timeout=600),
        )

    def build_protocol_oauth_adapter(self):
        return ProtocolOAuthAdapter(
            oauth_runner=self._run_protocol_oauth,
            result_mapper=lambda ctx, result: self._map_chatgpt_result(
                result,
                user_id=result.get("account_id", ""),
                require_oauth=True,
            ),
        )

    def build_protocol_mailbox_adapter(self):
        def _build_worker(ctx, artifacts):
            from platforms.chatgpt.protocol_mailbox import ChatGPTProtocolMailboxWorker

            return ChatGPTProtocolMailboxWorker(
                mailbox=self.mailbox,
                mailbox_account=ctx.identity.mailbox_account,
                provider=(self.config.extra or {}).get("mail_provider", ""),
                proxy_url=ctx.proxy,
                log_fn=ctx.log,
            )

        def _map_result(ctx, result):
            _assert_complete_oauth_callback(result)
            access_token = result.access_token or ""
            refresh_token = result.refresh_token or ""
            session_token = result.session_token or ""
            metadata = getattr(result, "metadata", None) or {}

            return RegistrationResult(
                email=result.email,
                password=result.password or (ctx.password or ""),
                user_id=result.account_id,
                token=access_token,
                status=AccountStatus.REGISTERED,
                extra={
                    "access_token": access_token,
                    "refresh_token": refresh_token,
                    "id_token": result.id_token,
                    "session_token": session_token,
                    "workspace_id": result.workspace_id,
                    "cookies": metadata.get("cookies", ""),
                    "profile": metadata.get("profile", {}),
                    "expires_at": metadata.get("expires_at", ""),
                    "session": metadata.get("session", {}),
                },
            )

        return ProtocolMailboxAdapter(
            result_mapper=_map_result,
            worker_builder=_build_worker,
            register_runner=lambda worker, ctx, artifacts: worker.run(
                email=ctx.identity.email,
                password=ctx.password,
            ),
        )

    def get_platform_actions(self) -> list:
        return [
            {"id": "switch_account", "label": "切换到 Codex 桌面端", "params": []},
            {"id": "get_account_state", "label": "查询账号状态/订阅", "params": []},
            {"id": "refresh_token", "label": "刷新 Token", "params": []},
            {"id": "payment_link", "label": "打开支付链接",
             "params": [
                 {"key": "country", "label": "地区", "type": "select",
                  "options": ["ID","US","SG","TR","HK","JP","GB","AU","CA","IN","BR","MX","EU"]},
                 {"key": "currency", "label": "币种", "type": "select",
                  "options": ["IDR","USD","SGD","TRY","HKD","JPY","GBP","AUD","CAD","INR","BRL","MXN","EUR"]},
                 {"key": "plan", "label": "套餐", "type": "select",
                  "options": ["plus", "team"]},
                 {"key": "auto_checkout", "label": "自动提交 PayPal", "type": "select",
                  "options": ["true", "false"]},
                 {"key": "payment_method", "label": "支付方式", "type": "select",
                  "options": ["paypal"]},
                 {"key": "headless", "label": "后台模式", "type": "select",
                  "options": ["false", "true"]},
                 # checkout_mode 决定 PayPal checkout 浏览器后端：
                 #   - protocol: 走 Stripe API 协议链，无浏览器
                 #   - camoufox_headed / camoufox_headless: 老 Camoufox 路径
                 #   - bitbrowser_headed / bitbrowser_hidden / bitbrowser_headless:
                 #     新 BitBrowser 路径，profile ID 通过 bit_profile_id 字段传入
                 {"key": "checkout_mode", "label": "Checkout 后端模式", "type": "select",
                  "options": [
                      "",
                      "protocol",
                      "camoufox_headed",
                      "camoufox_headless",
                      "bitbrowser_headed",
                      "bitbrowser_hidden",
                      "bitbrowser_headless",
                  ]},
                 # bitbrowser_* 模式下必填：BitBrowser 客户端里手工创建好的 profile ID
                 # （比特浏览器 → 浏览器列表 → 编辑那一栏看到的 ID 字符串）。
                 # 留空时回退到 BIT_PROFILE_ID 环境变量。
                 {"key": "bit_profile_id", "label": "BitBrowser Profile ID", "type": "text",
                  "placeholder": "比特浏览器 profile ID（仅 bitbrowser_* 模式下生效）"},
                 {"key": "checkout_timeout", "label": "结账超时秒数", "type": "number"},
                 {"key": "checkout_hold_seconds", "label": "前台保留秒数", "type": "number"},
                 # SMS 号码池：批量手机号 + 短信中转 URL，PayPal OTP 用
                 # 每行 `+phone----relay_url`，多行批量。空行 / # 注释行自动忽略。
                 {"key": "sms_pool", "label": "SMS 号码池 (+phone----relay_url 每行一条)",
                  "type": "textarea", "placeholder": "+15822057201----https://mail-api.yuecheng.shop/api/text-relay/eca_tr_xxx"},
             ]},
            {"id": "upload_cpa", "label": "上传 CPA",
             "params": [
                 {"key": "api_url", "label": "CPA API URL", "type": "text"},
                 {"key": "api_key", "label": "CPA API Key", "type": "text"},
             ]},
            {"id": "upload_tm", "label": "上传 Team Manager",
             "params": [
                 {"key": "api_url", "label": "TM API URL", "type": "text"},
                 {"key": "api_key", "label": "TM API Key", "type": "text"},
             ]},
        ]

    def get_desktop_state(self) -> dict:
        from platforms.chatgpt.switch import get_codex_desktop_state

        return get_codex_desktop_state()

    def execute_action(self, action_id: str, account: Account, params: dict) -> dict:
        if action_id == "payment_link":
            return self._handle_generate_link(account, params)
        return super().execute_action(action_id, account, params)

    def _execute_platform_action(self, action_id: str, account: Account, params: dict) -> dict:
        """Handle ChatGPT-specific actions."""
        proxy = self.config.proxy if self.config else None
        extra = account.extra or {}

        class _A: pass
        a = _A()
        a.email = account.email
        a.access_token = extra.get("access_token") or account.token
        a.refresh_token = extra.get("refresh_token", "")
        a.id_token = extra.get("id_token", "")
        a.session_token = extra.get("session_token", "")
        from .constants import OAUTH_CLIENT_ID
        a.client_id = extra.get("client_id", OAUTH_CLIENT_ID)
        a.cookies = extra.get("cookies", "")
        a.user_id = account.user_id or ""
        a.account_id = account.user_id or ""

        if action_id == "switch_desktop":
            from platforms.chatgpt.switch import (
                close_codex_app,
                extract_session_token,
                fetch_chatgpt_account_state,
                get_codex_desktop_state,
                read_current_codex_account,
                restart_codex_app,
                switch_codex_account,
            )

            session_token = extract_session_token(a.session_token, a.cookies)
            if not session_token:
                return {"ok": False, "error": "Switch to Codex desktop requires session_token"}

            close_ok, close_msg = close_codex_app()
            switch_ok, switch_data = switch_codex_account(session_token=session_token, cookies=a.cookies)
            if not switch_ok:
                return {"ok": False, "error": switch_data.get("error", "Switch failed")}

            remote_state = fetch_chatgpt_account_state(
                access_token=a.access_token,
                session_token=session_token,
                cookies=a.cookies,
                proxy=proxy,
            )
            local_state = read_current_codex_account()
            restart_ok, restart_msg = restart_codex_app()
            message_parts = [switch_data.get("message", "Codex credentials written")]
            if close_msg:
                message_parts.append(close_msg)
            if restart_msg:
                message_parts.append(restart_msg)
            data = {
                "message": ".".join(part for part in message_parts if part),
                "close": {"ok": close_ok, "message": close_msg},
                "restart": {"ok": restart_ok, "message": restart_msg},
                "local_app_account": local_state,
                "desktop_app_state": get_codex_desktop_state(),
                "remote_state": remote_state,
                "switch_details": switch_data,
            }
            if remote_state.get("access_token"):
                data["access_token"] = remote_state["access_token"]
            if remote_state.get("refresh_token"):
                data["refresh_token"] = remote_state["refresh_token"]
            return {"ok": True, "data": data}

        if action_id == "upload_cpa":
            from platforms.chatgpt.cpa_upload import upload_to_cpa, generate_token_json
            token_data = generate_token_json(a)
            ok, msg = upload_to_cpa(token_data, api_url=params.get("api_url"),
                                    api_key=params.get("api_key"))
            return {"ok": ok, "data": msg}

        if action_id == "upload_tm":
            from platforms.chatgpt.cpa_upload import upload_to_team_manager
            ok, msg = upload_to_team_manager(a, api_url=params.get("api_url"),
                                             api_key=params.get("api_key"))
            return {"ok": ok, "data": msg}

        if action_id == "payment_link":
            return self._handle_generate_link(account, params)

        raise NotImplementedError(f"Unknown action: {action_id}")

    # Override specific capability handlers
    def _handle_query_state(self, account: Account, params: dict) -> dict:
        """Handle query_state capability for ChatGPT."""
        proxy = self.config.proxy if self.config else None
        extra = account.extra or {}

        class _A: pass
        a = _A()
        a.access_token = extra.get("access_token") or account.token
        a.session_token = extra.get("session_token", "")
        a.cookies = extra.get("cookies", "")

        from platforms.chatgpt.switch import fetch_chatgpt_account_state, get_codex_desktop_state, read_current_codex_account

        data = fetch_chatgpt_account_state(
            access_token=a.access_token,
            session_token=a.session_token,
            cookies=a.cookies,
            proxy=proxy,
        )
        data["local_app_account"] = read_current_codex_account()
        data["desktop_app_state"] = get_codex_desktop_state()
        return {"ok": True, "data": data}

    def _handle_refresh_token(self, account: Account, params: dict) -> dict:
        """Handle refresh_token capability for ChatGPT."""
        proxy = self.config.proxy if self.config else None
        extra = account.extra or {}

        class _A: pass
        a = _A()
        a.access_token = extra.get("access_token") or account.token
        a.refresh_token = extra.get("refresh_token", "")
        a.session_token = extra.get("session_token", "")
        a.cookies = extra.get("cookies", "")

        from platforms.chatgpt.token_refresh import TokenRefreshManager
        manager = TokenRefreshManager(proxy_url=proxy)
        result = manager.refresh_account(a)
        if result.success:
            data = {"access_token": result.access_token, "refresh_token": result.refresh_token}
            try:
                from platforms.chatgpt.switch import fetch_chatgpt_account_state
                data["account_state"] = fetch_chatgpt_account_state(
                    access_token=result.access_token,
                    session_token=a.session_token,
                    cookies=a.cookies,
                    proxy=proxy,
                )
            except Exception:
                pass
            return {"ok": True, "data": data}
        return {"ok": False, "error": result.error_message}

    def _build_turnstile_solver_for_checkout(self):
        """构造给 Camoufox checkout 用的验证码求解回调。

        PayPal security challenge 只使用 YesCaptcha；如未配置可用 YesCaptcha，则返回
        None，让 checkout 流程退化为人工等待。
        """
        log_fn = getattr(self, "_log_fn", print)
        try:
            if not self._has_configured_captcha("yescaptcha_api"):
                log_fn("未启用验证码自动求解（YesCaptcha 未配置）")
                return None
            captcha_solver = self._make_captcha(provider_key="yescaptcha_api")
        except Exception as exc:
            log_fn(f"未启用验证码自动求解（YesCaptcha 初始化失败: {exc}）")
            return None
        log_fn("已启用验证码自动求解，provider: YesCaptcha")

        def _solver(page_url: str, site_key: str, challenge_type: str = "turnstile") -> str:
            if challenge_type == "recaptcha_v2":
                return captcha_solver.solve_recaptcha_v2(page_url, site_key)
            # **PayPal 实战证据** (`@tools/captures/checkout-20260526-003842-z6qrov0qi0_edu.hsxhome.com.har`
            # entry 347)：``paypal.com/pay/`` 风控页是 hCaptcha (iframe src 含
            # ``hcaptcha_fph.html?siteKey=...``)，必须走 ``solve_hcaptcha`` 才能拿到
            # 可注入到 ``form[name=challenge]`` 里的 ``g-recaptcha-response`` token。
            if challenge_type == "hcaptcha":
                return captcha_solver.solve_hcaptcha(page_url, site_key)
            return captcha_solver.solve_turnstile(page_url, site_key)

        return _solver

    def _handle_generate_link(self, account: Account, params: dict) -> dict:
        """Handle generate_link capability for ChatGPT.

        **行为变更**（"打开支付链接"语义）：账号 ``extra`` 里已存了
        ``cashier_url`` 时优先把它**直接返回**——前端拿到 URL 就在新标签
        页打开。这样"打开支付链接"按钮就跟字面意思一致了：注册阶段已生成
        过的链接直接复用，不再每次都重新打 ChatGPT 后端 API 创建新会话。

        ``params`` 里若显式传 ``regenerate=true`` 则跳过这条路径，强制重新
        生成（用于链接过期 / 想要换 country/currency 等场景）。
        """
        self.raise_if_cancelled()
        proxy = self.config.proxy if self.config else None
        extra = account.extra or {}

        regenerate = _bool_param(params, "regenerate", False)
        if not regenerate:
            existing_url = str(
                extra.get("cashier_url")
                or (extra.get("account_overview") or {}).get("cashier_url")
                or ""
            ).strip()
            if existing_url:
                getattr(self, "_log_fn", print)(
                    f"复用账号已有 cashier_url（不重新生成）: {existing_url}"
                )
                return {
                    "ok": True,
                    "data": {
                        "url": existing_url,
                        "checkout_url": existing_url,
                        "cashier_url": existing_url,
                        "plan": params.get("plan", "plus"),
                        "auto_checkout": False,
                        "message": "支付链接已存在，直接打开",
                        "reused": True,
                    },
                }

        class _A: pass
        a = _A()
        a.email = account.email
        a.password = account.password
        a.access_token = extra.get("access_token") or account.token
        a.refresh_token = extra.get("refresh_token", "")
        a.id_token = extra.get("id_token", "")
        a.session_token = extra.get("session_token", "")
        a.cookies = extra.get("cookies", "")

        from platforms.chatgpt import payment as payment_module
        plan = params.get("plan", "plus")
        country = params.get("country", "ID")
        currency = params.get("currency") or None
        # 账单地址来源（meiguodizhi.com 接口）："US" 走 ``/``，"JP" 走 ``/jp-address``。
        # 默认 US 保持向下兼容；其它值在 fetch_billing_address 里 fallback US。
        address_region = str(params.get("address_region") or "US").strip().upper() or "US"
        auto_checkout = _bool_param(params, "auto_checkout", True)
        payment_method = str(params.get("payment_method") or "paypal").strip().lower()
        headless = _bool_param(params, "headless", False)
        checkout_timeout = _int_param(params, "checkout_timeout", 180)
        checkout_hold_seconds = _optional_int_param(params, "checkout_hold_seconds")
        record_har = _bool_param(params, "record_har", False)
        record_har_path = _build_checkout_har_path(account.email) if record_har else None
        checkout_mode = str(params.get("checkout_mode") or "").strip().lower()
        if not checkout_mode:
            checkout_mode = "camoufox_headless" if headless else "camoufox_headed"
        # bitbrowser_* 模式下必须有 profile ID。表单输入优先于环境变量。
        bit_profile_id = str(params.get("bit_profile_id") or "").strip()
        if not bit_profile_id:
            bit_profile_id = os.environ.get("BIT_PROFILE_ID", "").strip()
        # 把 checkout_mode 翻成 BrowserBackendConfig；protocol 模式不需要 backend
        # （这里给 None，下游 _run_camoufox 不会被调用到）。
        backend_config: BrowserBackendConfig | None = None
        # acquired_profile_id 记录"是从池里 acquire 出来的"，跑完要 release。
        # 表单/环境变量传进来的不在池里，不需要 release。
        acquired_profile_id: str = ""
        if checkout_mode.startswith("bitbrowser_"):
            window_mode = checkout_mode[len("bitbrowser_"):]
            # 优先从 BitBrowser profile 池里 acquire 一个最少使用的 profile。
            # 池为空时回落到表单/环境变量提供的单一 ID（保持向后兼容）。
            from application.bitbrowser_profiles import (
                bitbrowser_profile_pool,
                BitBrowserProfilePoolEmpty,
            )
            try:
                resolved_profile_id = bitbrowser_profile_pool.acquire_or(
                    fallback=bit_profile_id
                )
                # 判断是不是真的从池里 acquire 的（影响 release）：池里有这个
                # ID 就视为"从池里出来的"，否则视为 fallback。
                pool_ids = {
                    item["profile_id"]
                    for item in bitbrowser_profile_pool.list_profiles()
                }
                if resolved_profile_id in pool_ids:
                    acquired_profile_id = resolved_profile_id
            except BitBrowserProfilePoolEmpty:
                # 池空 + 没 fallback → fail-fast，避免下到 BitBrowser API 才报错
                return {
                    "ok": False,
                    "error": (
                        "checkout_mode=bitbrowser_* 需要在「设置 → BitBrowser」"
                        "里添加 profile ID，或在表单里填写 BitBrowser Profile ID"
                        "（也可设置 BIT_PROFILE_ID 环境变量）"
                    ),
                }
            backend_config = BrowserBackendConfig.bitbrowser(
                profile_id=resolved_profile_id,
                window_mode=window_mode,
                api_url=os.environ.get("BIT_API_URL", "").strip() or None,
                api_token=os.environ.get("BIT_API_TOKEN", "").strip() or None,
            )
            getattr(self, "_log_fn", print)(
                f"BitBrowser profile 已选择: {resolved_profile_id} "
                f"(window_mode={window_mode}, "
                f"来源={'profile 池' if acquired_profile_id else '表单/环境变量'})"
            )
        elif checkout_mode in ("camoufox_headless", "camoufox_headed"):
            backend_config = BrowserBackendConfig.camoufox(
                headless=(checkout_mode == "camoufox_headless"),
            )
        # 解析 SMS 号码池：多行 +phone----relay_url。失败行会被静默忽略，
        # 这里只保留结构化后的非空列表，避免后续 stage / camoufox 反复字符串处理。
        sms_pool_raw = str(params.get("sms_pool") or "")
        try:
            sms_pool = payment_module.parse_sms_pool(sms_pool_raw)
        except Exception as exc:  # 防御性：解析失败也不应阻塞 checkout
            sms_pool = []
            getattr(self, "_log_fn", print)(f"SMS 号码池解析失败（忽略）: {exc}")
        if sms_pool_raw and not sms_pool:
            getattr(self, "_log_fn", print)(
                "警告：sms_pool 提供了内容但没解析出任何条目，请按 `+phone----relay_url` 格式排查"
            )
        elif sms_pool:
            getattr(self, "_log_fn", print)(f"SMS 号码池已加载 {len(sms_pool)} 条")
        checkout_proxy = None

        # Manually construct basic cookie in case old accounts don't have complete cookie string
        if not a.cookies and a.session_token:
            a.cookies = f"__Secure-next-auth.session-token={a.session_token}"

        getattr(self, "_log_fn", print)("生成 ChatGPT 测试支付链接不使用代理")
        if plan == "plus":
            url = payment_module.generate_plus_link(a, proxy=None, country=country, currency=currency)
        else:
            url = payment_module.generate_team_link(a, proxy=None, country=country, currency=currency)
        self.raise_if_cancelled()

        checkout_automation = None
        if url and auto_checkout:
            checkout_proxy = proxy
            if not checkout_proxy:
                proxy_region = str(params.get("proxy_region") or country or getattr(account, "region", "") or "").strip().upper()
                checkout_proxy = proxy_pool.get_next(region=proxy_region)
            if checkout_proxy:
                getattr(self, "_log_fn", print)(f"Camoufox checkout 使用代理: {_mask_proxy(checkout_proxy)}")
            else:
                getattr(self, "_log_fn", print)("Camoufox checkout 未配置代理")
            getattr(self, "_log_fn", print)("支付链接已生成，开始自动 PayPal checkout")
            getattr(self, "_log_fn", print)(f"checkout 模式: {checkout_mode}")
            # 是否启用 YesCaptcha 远端求解（前端弹窗里的开关）。
            # 关闭时 turnstile_solver 强制为 None，payment 模块的 captcha
            # 路径会退化为"代码鼠标点击 + 10s 等待跳转"，避免反复在
            # YesCaptcha 不识别的 sitekey 上烧配额。
            use_captcha_service = _bool_param(params, "use_captcha_service", True)
            if use_captcha_service:
                turnstile_solver = self._build_turnstile_solver_for_checkout()
            else:
                getattr(self, "_log_fn", print)(
                    "已禁用 YesCaptcha 求解（弹窗开关），captcha 出现时仅自动点击 + 等 10s"
                )
                turnstile_solver = None
            log_fn = getattr(self, "_log_fn", print)

            def _run_camoufox(headless_flag: bool):
                # 名字保留 _run_camoufox 兼容老日志/调用方，实际后端由
                # backend_config 决定（Camoufox / BitBrowser）。
                backend_label = (
                    f"BitBrowser({backend_config.window_mode})"
                    if backend_config and backend_config.is_bitbrowser
                    else f"Camoufox(headless={headless_flag})"
                )
                log_fn(
                    f"切换到独立线程执行 checkout backend={backend_label}"
                )
                return _run_sync_checkout_isolated(
                    payment_module.complete_paypal_checkout,
                    checkout_url=url,
                    cookies_str=a.cookies,
                    proxy=checkout_proxy,
                    email=account.email,
                    payment_method=payment_method,
                    headless=headless_flag,
                    timeout=checkout_timeout,
                    hold_seconds=checkout_hold_seconds,
                    log_fn=log_fn,
                    cancel_check=self.is_cancel_requested,
                    turnstile_solver=turnstile_solver,
                    record_har_path=record_har_path,
                    sms_pool=sms_pool,
                    backend_config=backend_config,
                    phone_swap_callback=params.get("phone_swap_callback"),
                    address_region=address_region,
                )

            def _run_protocol():
                log_fn("启动协议模式 checkout")
                return _run_sync_checkout_isolated(
                    payment_module.complete_paypal_checkout_protocol,
                    checkout_url=url,
                    cookies_str=a.cookies,
                    proxy=checkout_proxy,
                    email=account.email,
                    payment_method=payment_method,
                    timeout=checkout_timeout,
                    log_fn=log_fn,
                    cancel_check=self.is_cancel_requested,
                    turnstile_solver=turnstile_solver,
                    sms_pool=sms_pool,
                    address_region=address_region,
                )

            if checkout_mode == "protocol":
                # 协议模式失败时**直接报错**，不再自动回落 camoufox。
                # 理由：camoufox 兜底会掩盖协议链的真实失败原因，让调试变难；
                # 而且每次跑都要等 camoufox 启动 + 浏览器自动化，浪费时间。
                # 真要 fallback 的话，由前端在外层切换 checkout_mode 重新发起。
                checkout_automation = _run_protocol()
                if checkout_automation and not checkout_automation.get("ok"):
                    proto_err = str(checkout_automation.get("error", "") or "").strip()
                    log_fn(
                        "协议模式 checkout 失败（stage="
                        + str(checkout_automation.get("stage", "?"))
                        + "），不再回落 camoufox（便于排查）"
                        + (f"；原因: {proto_err}" if proto_err else "")
                    )
            else:
                try:
                    checkout_automation = _run_camoufox(
                        headless_flag=(checkout_mode == "camoufox_headless")
                    )
                finally:
                    # BitBrowser 池里 acquire 出来的 profile，跑完后释放计数，
                    # 让下一次并发能挑到当前没在用的 profile。表单/环境变量
                    # 传的 ID 不在池里，acquired_profile_id 是空字符串，
                    # release 是 no-op。
                    if acquired_profile_id:
                        try:
                            from application.bitbrowser_profiles import (
                                bitbrowser_profile_pool,
                            )
                            bitbrowser_profile_pool.release(acquired_profile_id)
                            log_fn(
                                f"BitBrowser profile 池已释放: {acquired_profile_id}"
                            )
                        except Exception as exc:
                            log_fn(f"BitBrowser profile 池释放失败（忽略）: {exc}")
            self.raise_if_cancelled()
            if checkout_automation.get("ok"):
                getattr(self, "_log_fn", print)("PayPal checkout 自动流程已提交")
            else:
                checkout_error = str(checkout_automation.get("error", "") or "PayPal checkout automation failed")
                getattr(self, "_log_fn", print)(f"PayPal checkout 自动流程失败: {checkout_error}")

        checkout_ok = bool(checkout_automation and checkout_automation.get("ok"))
        action_ok = bool(url) if not auto_checkout else bool(url and checkout_ok)
        action_error = ""
        if url and auto_checkout and not checkout_ok:
            action_error = str(
                (checkout_automation or {}).get("error", "")
                or "PayPal checkout automation failed"
            )

        data = {
            "url": url,
            "checkout_url": url,
            "cashier_url": url,
            "plan": plan,
            "country": country,
            "currency": currency or "",
            "payment_method": payment_method,
            "auto_checkout": auto_checkout,
            "headless": headless,
            "checkout_mode": checkout_mode,
            "proxy_used": checkout_proxy or "",
            "record_har_path": record_har_path or "",
            "message": (
                "Payment link generated, PayPal checkout automation submitted."
                if checkout_ok
                else (
                    "Payment link generated, but PayPal checkout automation failed."
                    if url and auto_checkout
                    else "Payment link generated."
                )
            ),
        }
        if checkout_automation is not None:
            data["checkout_automation"] = checkout_automation

        return {
            "ok": action_ok,
            "data": data,
            "error": action_error,
        }

    
