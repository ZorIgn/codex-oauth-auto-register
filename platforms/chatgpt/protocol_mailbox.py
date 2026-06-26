"""ChatGPT 协议邮箱注册 worker。"""

from __future__ import annotations



from typing import Any, Callable



from platforms.chatgpt.register import RegistrationEngine, RegistrationResult





_OTP_FAILURE_MARKERS = (

    "获取验证码失败",

    "发送验证码失败",

    "验证码超时",

    "邮箱验证码",

    "email otp",

    "verification code",

)





def _result_text(result: Any, key: str) -> str:

    if isinstance(result, dict):

        return str(result.get(key, "") or "")

    return str(getattr(result, key, "") or "")





def _result_dict(result: Any) -> dict:

    return result if isinstance(result, dict) else {}





class _MailboxEmailService:

    def __init__(self, *, mailbox, mailbox_account, provider: str):

        self.service_type = type("ST", (), {"value": provider})()

        self._mailbox = mailbox

        self._mailbox_account = mailbox_account

        self._acct = None

        self._before_ids = None



    def create_email(self, config=None):

        self._acct = self._mailbox_account

        try:

            self._before_ids = self._mailbox.get_current_ids(self._mailbox_account)

        except Exception:

            self._before_ids = set()

        return {

            "email": self._mailbox_account.email,

            "service_id": getattr(self._mailbox_account, "account_id", ""),

            "token": getattr(self._mailbox_account, "account_id", ""),

        }



    def get_verification_code(self, email=None, email_id=None, timeout=120, pattern=None, otp_sent_at=None):

        import time as _time

        acct = self._acct or self._mailbox_account

        mailbox_type = type(self._mailbox).__name__



        # 如果知道 OTP 发送时间，先等邮件投递完成再开始轮询

        effective_timeout = timeout

        if otp_sent_at is not None:

            elapsed = _time.time() - otp_sent_at

            delivery_delay = 8

            if elapsed < delivery_delay:

                wait_remaining = delivery_delay - elapsed

                print(f"[Mailbox:{mailbox_type}] OTP 发送 {elapsed:.0f}s 前，等待 {wait_remaining:.0f}s 后开始轮询（让邮件到达）")

                _time.sleep(wait_remaining)

                effective_timeout = max(30, timeout - int(wait_remaining))



        before_count = len(self._before_ids) if self._before_ids else 0

        print(f"[Mailbox:{mailbox_type}] 开始等待验证码 email={acct.email} timeout={effective_timeout}s before_ids={before_count}")



        try:

            code = self._mailbox.wait_for_code(

                acct, keyword="", timeout=effective_timeout,

                code_pattern=pattern,

                before_ids=self._before_ids or None,

            )

            print(f"[Mailbox:{mailbox_type}] 轮询成功，获取到验证码: {code}")

            return code

        except TimeoutError:

            print(f"[Mailbox:{mailbox_type}] 轮询超时 ({effective_timeout}s)，未收到验证码")

            raise



    def update_status(self, success, error=None):

        return None



    @property

    def status(self):

        return None





class ChatGPTProtocolMailboxWorker:

    def __init__(

        self,

        *,

        mailbox,

        mailbox_account,

        provider: str,

        proxy_url: str | None = None,

        log_fn: Callable[[str], None] = print,

    ):

        if not mailbox or not mailbox_account:

            raise ValueError("ChatGPT 注册流程依赖 mailbox provider，当前未获取到邮箱账号")

        self.mailbox = mailbox

        self.mailbox_account = mailbox_account

        self.proxy_url = proxy_url

        self.log_fn = log_fn

        email_service = _MailboxEmailService(

            mailbox=mailbox,

            mailbox_account=mailbox_account,

            provider=provider,

        )

        self.engine = RegistrationEngine(

            email_service=email_service,

            proxy_url=proxy_url,

            callback_logger=log_fn,

        )



    def _log(self, message: str) -> None:

        try:

            self.log_fn(message)

        except Exception:

            pass



    def _should_fallback_to_browser(self, result) -> bool:

        message = _result_text(result, "error_message").lower()

        return any(marker.lower() in message for marker in _OTP_FAILURE_MARKERS)



    def _build_browser_otp_callback(self):

        before_ids = set()

        try:

            before_ids = set(self.mailbox.get_current_ids(self.mailbox_account) or set())

        except Exception as exc:

            self._log(f"浏览器兜底读取邮箱基线失败，将继续等待验证码: {exc}")



        def otp_callback():

            try:

                return self.mailbox.wait_for_code(

                    self.mailbox_account,

                    keyword="",

                    timeout=600,

                    before_ids=before_ids or None,

                )

            except TypeError:

                return self.mailbox.wait_for_code(

                    self.mailbox_account,

                    keyword="",

                    timeout=600,

                )



        return otp_callback



    def _browser_result_to_protocol_result(self, raw, *, email: str, password: str) -> RegistrationResult:

        raw_dict = _result_dict(raw)

        metadata = {

            "fallback": "browser",

            "cookies": raw_dict.get("cookies", ""),

            "profile": raw_dict.get("profile", {}),

            "expires_at": raw_dict.get("expires_at", ""),

            "session": raw_dict.get("session", {}),

        }

        return RegistrationResult(

            success=True,

            email=_result_text(raw, "email") or email,

            password=_result_text(raw, "password") or password,

            account_id=_result_text(raw, "account_id"),

            workspace_id=_result_text(raw, "workspace_id"),

            access_token=_result_text(raw, "access_token"),

            refresh_token=_result_text(raw, "refresh_token"),

            id_token=_result_text(raw, "id_token"),

            session_token=_result_text(raw, "session_token"),

            metadata=metadata,

            source="browser_fallback",

        )



    def _run_browser_fallback(self, *, email: str, password: str) -> RegistrationResult:

        from .browser_register import ChatGPTBrowserRegister



        self._log("协议模式验证码未送达，切换到浏览器模式继续注册...")

        worker = ChatGPTBrowserRegister(

            headless=True,

            proxy=self.proxy_url,

            otp_callback=self._build_browser_otp_callback(),

            phone_callback=None,

            log_fn=self.log_fn,

        )

        raw = worker.run(email=email, password=password)

        return self._browser_result_to_protocol_result(raw, email=email, password=password)



    def run(self, *, email: str, password: str):

        self.engine.email = email

        self.engine.password = password

        result = self.engine.run()

        if not result or not result.success:

            if result and self._should_fallback_to_browser(result):

                return self._run_browser_fallback(email=email, password=password)

            raise RuntimeError(result.error_message if result else "注册失败")

        return result

