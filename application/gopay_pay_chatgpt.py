"""GoPay 协议付款 ChatGPT Plus 编排器。

整条流水线：

  ① **协议**：调 ``platforms.chatgpt.payment.generate_plus_link(country=ID, currency=IDR)``
      拿到 ChatGPT 的 cashier_url（Stripe hosted checkout）

  ② **浏览器**：打开 cashier_url，等用户/自动化把页面跳到 Midtrans 域，
      抓 ``page.url`` 即 ``midtrans_url``，关闭浏览器

  ③ **协议**：用预先注册好的 GoPay 号 + Hero-SMS aid，调
      ``opai.core.gopay_payment_protocol.GoPayPayment.pay`` 完成付款（14 步 Midtrans API）

设计原则：
- **不依赖** ``platforms/gopay-deploy`` 的 Payment Inbox 服务（Inbox 只是 worker 的 job 队列源）
- 复用 ``GoPayPayment`` 协议类（已经被 ``ensure_opai_on_path`` 加到 sys.path）
- 三步串行，整段失败任意一步就标 FAILED；中间产物（cashier_url / midtrans_url）写进 task result 方便排查
- 单条 ChatGPT × 单条 GoPay 号一一配对（concurrency=1 时）
"""

from __future__ import annotations

import os
import re
import threading
import time
from typing import Any, Callable, Optional

from sqlmodel import Session, select

from core.db import AccountModel, engine, save_account
from core.platform_accounts import build_platform_account


_MIDTRANS_URL_RE = re.compile(
    r"https?://app\.midtrans\.com/snap/v[34]/redirection/[0-9a-f-]{36}",
    re.IGNORECASE,
)


def _mask_proxy(proxy: str | None) -> str:
    """脱敏代理 URL 用于日志：只保留 host:port，把 user:pass 替换成 ***。"""
    value = str(proxy or "").strip()
    if not value or "@" not in value:
        return value
    scheme, _, rest = value.partition("://")
    if not rest:
        return value
    _, _, host = rest.rpartition("@")
    return f"{scheme}://***@{host}" if scheme else f"***@{host}"


def _normalize_proxy_url(proxy: str | None) -> str:
    """规范化代理 URL：缺 scheme 时自动补 ``http://``。

    数据库里存的代理常是裸 ``user:pass@host:port``（没有协议前缀），
    ``tls_client`` 等严格 URL 解析器会报 ``first path segment in URL
    cannot contain colon``。这里统一补前缀避免下游崩溃。
    """
    value = str(proxy or "").strip()
    if not value:
        return ""
    if "://" in value:
        return value
    return f"http://{value}"


class PhoneTTLGuard:
    """Hero-SMS 号码 20 分钟自动回收的护栏。

    流水线从开始（注册拿号）起算，每跨一步调一次 ``check()``；超过
    ``ttl_seconds`` 即抛 ``RuntimeError``，调用方据此判失败重开任务。
    用 ``time.monotonic`` 避免系统时钟回拨干扰。
    """

    def __init__(self, ttl_seconds: int = 1200):
        self.ttl_seconds = max(int(ttl_seconds or 0), 0)
        self._start = time.monotonic()

    def elapsed(self) -> float:
        return time.monotonic() - self._start

    def check(self) -> None:
        if self.ttl_seconds <= 0:
            return
        if self.elapsed() > self.ttl_seconds:
            raise RuntimeError(
                f"Hero-SMS 号码有效期({self.ttl_seconds // 60}min)已过，"
                f"本次任务判失败（已耗时 {int(self.elapsed())}s）"
            )


def claim_envelope_for_account(client, envelope_url: str, *, log: Callable[[str], None] = print) -> bool:
    """给已登录的 GoPay client 领一个红包。

    ``envelope_url`` 形如 ``https://app.gopay.co.id/NF8p/qps2s1y0``。空 URL
    直接返回 False。任何异常都吞掉返回 False（领红包失败不该让整条流水线崩）。
    """
    url = str(envelope_url or "").strip()
    if not url:
        return False
    try:
        from platforms.gopay._opai_loader import ensure_opai_on_path

        ensure_opai_on_path()
        from opai.core.envelope_manager import EnvelopeManager

        mgr = EnvelopeManager()
        mgr.add_url(url)
        result = mgr.claim_one(client)
        ok = bool(result)
        log(f"红包领取{'成功' if ok else '失败/无可用红包'}: {url}")
        return ok
    except Exception as exc:
        log(f"红包领取异常（忽略）: {exc}")
        return False


def _latest_gopay_account() -> AccountModel | None:
    """取最新注册的一条 gopay 号（不看余额）。"""
    with Session(engine) as session:
        latest = session.exec(
            select(AccountModel)
            .where(AccountModel.platform == "gopay")
            .order_by(AccountModel.created_at.desc())
            .limit(1)
        ).first()
        if not latest:
            return None
        latest_id = int(latest.id)
    with Session(engine) as session:
        return session.get(AccountModel, latest_id)


def _resolve_gopay_client(phone: str, proxy: str, *, log: Callable[[str], None] = print):
    """resume 一个 GoPay client（用于轮询余额 / 领红包）。失败返回 None。"""
    phone = str(phone or "").strip()
    if not phone:
        return None
    try:
        from platforms.gopay._opai_loader import ensure_opai_on_path

        ensure_opai_on_path()
        from opai.core.gopay_protocol_worker import _resume_account

        resumed = _resume_account(phone, proxy=_normalize_proxy_url(proxy))
        if resumed and resumed.get("client") is not None:
            return resumed["client"]
    except Exception as exc:
        log(f"resume GoPay client 失败: {exc}")
    return None


def _maybe_topup_with_envelope(envelope_url: str, *, log: Callable[[str], None] = print) -> AccountModel | None:
    """没有余额够的号时，给最新注册的 GoPay 号领红包补余额后再挑。

    没有 envelope_url 直接返回 None。领完红包重查余额，把新余额写回 graph，
    然后返回该号（若余额 ≥ 1）。
    """
    url = str(envelope_url or "").strip()
    if not url:
        return None

    # 挑一条最新注册的 gopay 号（不看余额，可能是余额=0 等红包的号）
    with Session(engine) as session:
        latest = session.exec(
            select(AccountModel)
            .where(AccountModel.platform == "gopay")
            .order_by(AccountModel.created_at.desc())
            .limit(1)
        ).first()
        if not latest:
            return None
        latest_id = int(latest.id)

    with Session(engine) as session:
        model = session.get(AccountModel, latest_id)
        extra = _account_extra(model)
    phone = str(extra.get("phone") or model.email or "").strip()
    register_proxy = str(extra.get("register_proxy") or "")

    try:
        from platforms.gopay._opai_loader import ensure_opai_on_path

        ensure_opai_on_path()
        from opai.core.gopay_protocol_worker import _resume_account, _check_balance
    except Exception as exc:
        log(f"领红包前 resume 模块加载失败: {exc}")
        return None

    resumed = _resume_account(phone, proxy=_normalize_proxy_url(register_proxy))
    if not resumed:
        log(f"领红包前 resume 账号失败: {phone}")
        return None
    client = resumed["client"]

    if not claim_envelope_for_account(client, url, log=log):
        return None

    new_balance = 0
    try:
        new_balance = max(int(_check_balance(client) or 0), 0)
    except Exception:
        new_balance = 0
    log(f"领红包后余额 = {new_balance} IDR")

    # 写回 graph
    from core.account_graph import patch_account_graph

    with Session(engine) as session:
        model = session.get(AccountModel, latest_id)
        if model:
            patch_account_graph(session, model, summary_updates={"balance_rp": new_balance})
            session.commit()

    if new_balance >= 1:
        with Session(engine) as session:
            return session.get(AccountModel, latest_id)
    return None


def register_gopay_account(
    *,
    herosms_api_key: str,
    pin: str = "000000",
    proxy: str = "",
    envelope_url: str = "",
    sms_provider: str = "herosms",
    smspool_api_key: str = "",
    smsbower_api_key: str = "",
    herosms_max_price_usd: str = "",
    smspool_max_price: str = "",
    log: Callable[[str], None] = print,
) -> AccountModel | None:
    """自动注册一个新 GoPay 号并入库，返回 AccountModel。

    流程：调 GoPay plugin 的 ``register()``（内含拿号 + 注册 OTP + PIN OTP，
    见 platforms/gopay/plugin.py）→ ``save_account`` 入库 → 若余额 0 且给了
    红包链接则 resume client 领红包补余额 → 返回最新 AccountModel。

    ``sms_provider``: herosms（默认，需要 herosms_api_key）或 smspool
    （用 smspool_api_key，缺省走内置默认 key）。
    ``herosms_max_price_usd`` / ``smspool_max_price``: 拿号价格上限，空则
    走插件默认（0.11）。

    失败返回 None（不抛，让调用方决定是否继续）。
    """
    from core.base_platform import RegisterConfig
    from core.registry import get as get_platform

    provider = str(sms_provider or "herosms").strip().lower()
    api_key = str(herosms_api_key or "").strip()
    if provider == "herosms" and not api_key:
        log("自动注册 GoPay 失败：缺少 Hero-SMS API key")
        return None

    # 没显式传代理时，从主项目代理池取一个 ID 区域的代理（动态代理优先，
    # 失败回退静态池）。GoPay 的注册接口对印尼区出口 IP 敏感，直连容易被
    # WAF 403 / 风控；用代理池能稳得多。代理池也没号时回退直连。
    effective_proxy = _normalize_proxy_url(proxy)
    if not effective_proxy:
        try:
            from core.proxy_pool import proxy_pool

            picked = proxy_pool.get_next(region="ID") or ""
            if picked:
                effective_proxy = _normalize_proxy_url(picked)
                log(f"代理池分配：{_mask_proxy(effective_proxy)}（GoPay 注册用）")
            else:
                log("代理池为空，GoPay 注册回退直连")
        except Exception as exc:
            log(f"代理池调用异常，GoPay 注册回退直连：{exc}")

    cfg = RegisterConfig(
        executor_type="protocol",
        captcha_solver="auto",
        proxy=effective_proxy or None,
        extra={
            "identity_provider": "phone",
            "herosms_api_key": api_key,
            "gopay_pin": str(pin or "000000"),
            "gopay_proxy": effective_proxy or "",
            "sms_provider": provider,
            "smspool_api_key": str(smspool_api_key or ""),
            "smsbower_api_key": str(smsbower_api_key or ""),
            "herosms_max_price_usd": str(herosms_max_price_usd or ""),
            "smspool_max_price": str(smspool_max_price or ""),
        },
    )
    try:
        platform_cls = get_platform("gopay")
        platform = platform_cls(config=cfg)
        if hasattr(platform, "set_logger"):
            platform.set_logger(log)
        log("没有可用 GoPay 号，开始自动注册新号…")
        account = platform.register()
    except Exception as exc:
        log(f"自动注册 GoPay 失败: {exc}")
        return None

    save_account(account)
    # ``save_account`` 返回的 model 出了它内部的 session 就 detached，
    # 访问 ``.id`` 会触发懒加载报 DetachedInstanceError。用 email 重新查一次
    # 拿稳定的 id。
    with Session(engine) as session:
        fresh = session.exec(
            select(AccountModel)
            .where(AccountModel.platform == "gopay")
            .where(AccountModel.email == account.email)
        ).first()
        if not fresh:
            log("GoPay 自动注册入库后查不到记录，异常")
            return None
        model_id = int(fresh.id)
    log(f"GoPay 自动注册成功并入库: #{model_id} {account.email}")

    # 余额 0 + 有红包链接 → 领红包补余额
    extra = dict(getattr(account, "extra", {}) or {})
    balance_rp = int(extra.get("balance_rp") or 0)
    env_url = str(envelope_url or "").strip()
    if balance_rp < 1 and env_url:
        phone = str(extra.get("phone") or account.email or "").strip()
        try:
            from platforms.gopay._opai_loader import ensure_opai_on_path

            ensure_opai_on_path()
            from opai.core.gopay_protocol_worker import _resume_account, _check_balance

            resumed = _resume_account(phone, proxy=_normalize_proxy_url(proxy))
            if resumed and claim_envelope_for_account(resumed["client"], env_url, log=log):
                new_balance = max(int(_check_balance(resumed["client"]) or 0), 0)
                log(f"自动注册号领红包后余额 = {new_balance} IDR")
                from core.account_graph import patch_account_graph

                with Session(engine) as session:
                    m = session.get(AccountModel, model_id)
                    if m:
                        patch_account_graph(session, m, summary_updates={"balance_rp": new_balance})
                        session.commit()
        except Exception as exc:
            log(f"自动注册号领红包异常（忽略）: {exc}")

    with Session(engine) as session:
        return session.get(AccountModel, model_id)


def wait_for_balance(
    *,
    client,
    envelope_url: str,
    ttl_guard: "PhoneTTLGuard",
    poll_interval: float = 15.0,
    log: Callable[[str], None] = print,
) -> int:
    """轮询 GoPay 余额直到 ≥ 1 IDR，否则一直等到 ``ttl_guard`` 超时抛错。

    每轮：若给了 ``envelope_url`` 先尝试领红包补余额，再查余额。余额 ≥ 1
    立即返回。不再因"某次查到 0"就判失败——红包/充值到账有延迟，必须等。

    Args:
        client: 已登录的 GoPay client（``_resume_account`` 返回的 client）
        envelope_url: 红包链接，空则只查余额不领红包
        ttl_guard: 20 分钟号码有效期护栏；超时由它抛 RuntimeError
        poll_interval: 两次查询间隔秒数
    """
    from platforms.gopay._opai_loader import ensure_opai_on_path

    ensure_opai_on_path()
    from opai.core.gopay_protocol_worker import _check_balance

    env_url = str(envelope_url or "").strip()
    round_no = 0
    while True:
        # 先检查 TTL——超时抛错（任务判失败重开）
        ttl_guard.check()
        round_no += 1
        if env_url:
            try:
                claim_envelope_for_account(client, env_url, log=log)
            except Exception as exc:
                log(f"轮询领红包异常（忽略）: {exc}")
        try:
            balance = max(int(_check_balance(client) or 0), 0)
        except Exception:
            balance = 0
        log(f"余额轮询第 {round_no} 轮：{balance} IDR")
        if balance >= 1:
            return balance
        time.sleep(max(float(poll_interval or 0), 0))


def _account_extra(account_model: AccountModel) -> dict:
    """从 ``AccountModel`` 通过 ``build_platform_account`` 读出统一 extra。

    主项目里 ``AccountModel`` 自身只有 platform/email/password/user_id 这
    几列，``extra`` 实际上是从 ``AccountOverviewModel.summary_json`` +
    credentials + provider_accounts/resources 等多张表拼出来的，必须走
    ``build_platform_account`` 才能读到 plugin 写进去的 ``phone_local``、
    ``pin``、``herosms_activation_id`` 等字段。

    这些字段实际是写在 overview 的 ``summary_json`` 里，``build_platform_extra``
    会把它们整体放在 ``extra["account_overview"]``——所以这里把 overview
    字段也合并提到顶层，方便调用方按 ``extra["balance_rp"]`` / ``extra["pin"]``
    这种习惯写法直接读取。
    """
    if not account_model:
        return {}
    with Session(engine) as session:
        merged = session.merge(account_model, load=False)
        platform_account = build_platform_account(session, merged)
    extra = getattr(platform_account, "extra", {}) or {}
    if not isinstance(extra, dict):
        return {}
    merged_extra: dict[str, Any] = dict(extra)
    overview = extra.get("account_overview")
    if isinstance(overview, dict):
        # overview 里的字段优先级**低于**已存在的顶层字段（避免覆盖
        # plugin 主动写到 credentials 里的同名 key）
        for k, v in overview.items():
            merged_extra.setdefault(k, v)
    return merged_extra


def find_chatgpt_account(account_id: int) -> AccountModel | None:
    with Session(engine) as session:
        m = session.get(AccountModel, int(account_id))
        if not m or m.platform != "chatgpt":
            return None
        return m


def pick_available_gopay_account(min_balance_rp: int = 1) -> AccountModel | None:
    """从主项目数据库挑一条**可用**的 GoPay 号：注册成功 + 余额 ≥ 阈值。

    仅做最小可用筛选；如果你想要更复杂的策略（最早注册、按 Hero-SMS aid
    新鲜度排序）再扩展。这里按 created_at 倒序挑最新的，避免取到接码已
    过期（Hero-SMS 默认 20min）的旧号。
    """
    with Session(engine) as session:
        rows = session.exec(
            select(AccountModel)
            .where(AccountModel.platform == "gopay")
            .order_by(AccountModel.created_at.desc())
            .limit(50)
        ).all()
        for m in rows:
            extra = _account_extra(m)
            balance = int(extra.get("balance_rp") or 0)
            if balance >= int(min_balance_rp or 1):
                return m
    return None


def step_generate_cashier_url(
    chatgpt_account_model: AccountModel,
    *,
    country: str = "ID",
    currency: str = "IDR",
    proxy: Optional[str] = None,
    log: Callable[[str], None] = print,
) -> str:
    """步骤 ①：协议拿 ChatGPT Plus cashier URL。"""
    from platforms.chatgpt import payment as chatgpt_payment

    with Session(engine) as session:
        account = build_platform_account(session, chatgpt_account_model)

    # ``generate_plus_link`` 期望 ``account.access_token`` / ``account.cookies``，
    # 但 ``build_platform_account`` 返回的 ``Account`` 把 token 放在 ``token``
    # 字段、cookies 在 ``extra`` 里——参考 chatgpt/plugin.py::check_valid 的做法
    # 用一个 SimpleNamespace 适配过去。
    extra = dict(getattr(account, "extra", {}) or {})

    class _AccountAdapter:
        pass

    a = _AccountAdapter()
    a.access_token = str(extra.get("access_token") or getattr(account, "token", "") or "")
    a.cookies = str(extra.get("cookies", "") or "")
    if not a.access_token:
        raise RuntimeError(
            f"ChatGPT 账号 {account.email} 缺少 access_token，无法生成支付链接"
        )

    log(f"协议生成 cashier_url（country={country}, currency={currency}，不使用代理）")
    # 生成支付链接强制直连：ChatGPT cashier API 不需要代理，走代理反而可能
    # 因为出口 IP 与账号注册地不一致触发风控。忽略传入的 proxy。
    #
    # 并发场景下 curl_cffi 首次在多线程里初始化 SSL 库会偶发
    # ``curl: (35) TLS connect error ... invalid library`` 竞态——10 个 worker
    # 同时打 cashier API 时极易命中。这里加轻量重试（指数退避）兜底，区分
    # 瞬时 TLS/连接错误（重试）和业务错误（直接抛）。
    last_exc: Exception | None = None
    url = ""
    for attempt in range(1, 4):
        try:
            url = chatgpt_payment.generate_plus_link(
                a,
                proxy=None,
                country=country,
                currency=currency,
            )
            break
        except Exception as exc:  # noqa: BLE001 - 需按错误内容判断是否重试
            last_exc = exc
            msg = str(exc).lower()
            transient = (
                "tls connect error" in msg
                or "invalid library" in msg
                or "curl: (35)" in msg
                or "curl: (56)" in msg
                or "connection reset" in msg
                or "failed to perform" in msg
            )
            if attempt >= 3 or not transient:
                raise
            backoff = 0.5 * (2 ** (attempt - 1))
            log(f"cashier_url 生成瞬时失败（第 {attempt}/3 次，{backoff}s 后重试）: {exc}")
            time.sleep(backoff)
    if not url:
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("ChatGPT API 未返回 cashier URL")
    log(f"cashier_url = {url}")
    return url


def step_grab_midtrans_url(
    cashier_url: str,
    *,
    checkout_mode: str = "camoufox_headed",
    bit_profile_id: str = "",
    bit_api_url: str = "",
    bit_api_token: str = "",
    proxy: Optional[str] = None,
    timeout_seconds: int = 300,
    cancel_check: Optional[Callable[[], bool]] = None,
    log: Callable[[str], None] = print,
) -> str:
    """步骤 ②：浏览器打开 cashier_url，自动选 GoPay 渠道、填账单、点订阅，
    抓跳转后的 Midtrans URL 后关闭浏览器返回。

    ``checkout_mode`` 解析成 camoufox/bitbrowser backend（同 CtfGptPlus 那套）：
      camoufox_headed / camoufox_headless / bitbrowser_headed /
      bitbrowser_hidden / bitbrowser_headless。
    bitbrowser_* 必须提供 ``bit_profile_id``。
    """
    from platforms.chatgpt import payment as chatgpt_payment
    from platforms._browser_backend import parse_checkout_mode, DEFAULT_BIT_API_URL

    backend_config = parse_checkout_mode(
        checkout_mode,
        bit_profile_id=bit_profile_id,
        bit_api_url=bit_api_url or DEFAULT_BIT_API_URL,
        bit_api_token=bit_api_token,
    )
    log(
        f"浏览器抓 midtrans（mode={checkout_mode} -> backend={backend_config.backend}/"
        f"{backend_config.window_mode}）"
    )
    return chatgpt_payment.select_gopay_and_grab_midtrans(
        cashier_url,
        backend_config=backend_config,
        proxy=proxy,
        timeout_seconds=timeout_seconds,
        cancel_check=cancel_check,
        log=log,
    )


def step_pay_with_gopay(
    midtrans_url: str,
    gopay_account_model: AccountModel,
    *,
    herosms_api_key_override: str = "",
    smspool_api_key_override: str = "",
    smsbower_api_key_override: str = "",
    sms_provider_override: str = "",
    log: Callable[[str], None] = print,
) -> dict:
    """步骤 ③：用 GoPay 号协议完成 Midtrans 付款（14 步）。

    需要 ``Account.extra`` 里有 ``phone_local`` / ``pin`` / ``herosms_activation_id``，
    这些都是注册阶段写进去的（见 ``platforms/gopay/plugin.py::register``）。

    **接码渠道必须和注册时一致**：``herosms_activation_id`` 这个字段对
    SMSPool 注册的号来说存的其实是 SMSPool 的 ``order_id``，拿去 Hero-SMS
    查 OTP 永远等不到（必现 OTP timeout）。所以这里先从账号 extra 读
    注册渠道（``sms_provider``），据此选对应平台的接码 API 接付款 OTP。
    没记录渠道的老号回退到 ``sms_provider_override`` / 默认 herosms。

    接码平台 API key 来源（**不存账号 extra 里**，避免 /accounts API 把
    overview 返回给前端时泄漏全局密钥）：
      1. 显式传参（task payload 走这条）
      2. 环境变量（Hero-SMS: ``OPAI_HEROSMS_API_KEY``）
    """
    from platforms.gopay._opai_loader import ensure_opai_on_path

    ensure_opai_on_path()
    from opai.core.gopay_payment_protocol import GoPayPayment, GoPayFraudDenyError

    extra = _account_extra(gopay_account_model)
    phone_local = str(extra.get("phone_local") or "").strip()
    pin = str(extra.get("pin") or gopay_account_model.password or "").strip()
    aid = str(extra.get("herosms_activation_id") or "").strip()
    register_proxy = _normalize_proxy_url(extra.get("register_proxy"))
    # register_proxy 为空（注册时直连 / 老号没存代理）时从代理池补一个，
    # GoPay/Midtrans 对外国直连出口 IP 可能返回 407（CDN 把它当代理拦住），
    # 走代理池能用相同出口跑完付款流程。
    if not register_proxy:
        try:
            from core.proxy_pool import proxy_pool

            picked = proxy_pool.get_next(region="ID") or ""
            if picked:
                register_proxy = _normalize_proxy_url(picked)
                log(f"代理池分配：{_mask_proxy(register_proxy)}（GoPay 付款用）")
        except Exception as exc:
            log(f"代理池调用异常，GoPay 付款回退直连：{exc}")
    provider = (
        str(extra.get("sms_provider") or "").strip().lower()
        or str(sms_provider_override or "").strip().lower()
        or "herosms"
    )

    if not (phone_local and pin and aid):
        raise RuntimeError(
            "GoPay 账号缺少 phone_local / pin / herosms_activation_id，无法付款"
        )

    log(
        f"开始 GoPay 协议付款（phone={phone_local}, aid={aid}, "
        f"接码={provider}, midtrans=...{midtrans_url[-40:]}）"
    )

    # 按注册渠道构造「等付款 OTP」回调 + 「付款成功后归还号」回调。
    wait_otp, sms_done = _build_payment_sms_callbacks(
        provider=provider,
        aid=aid,
        herosms_api_key=herosms_api_key_override,
        smspool_api_key=smspool_api_key_override,
        smsbower_api_key=smsbower_api_key_override,
        log=log,
    )

    payment = GoPayPayment(proxy=register_proxy)
    try:
        result = payment.pay(
            midtrans_url=midtrans_url,
            phone=phone_local,
            country_code="62",
            pin=pin,
            wait_otp=wait_otp,
            otp_total_timeout=120,
            otp_resend_after=60,
        )
    except GoPayFraudDenyError as exc:
        raise RuntimeError(f"GoPay 风控拒付（号被烧）: {exc}")

    if not result.get("success"):
        detail = str(result.get("detail") or "unknown")
        raise RuntimeError(f"GoPay 付款失败: {detail}")

    # 付款成功——归还/结束号
    try:
        sms_done()
    except Exception as exc:
        log(f"sms_done 失败（忽略）: {exc}")

    log(f"GoPay 付款成功: {result}")
    return result


def _build_payment_sms_callbacks(
    *,
    provider: str,
    aid: str,
    herosms_api_key: str = "",
    smspool_api_key: str = "",
    smsbower_api_key: str = "",
    log: Callable[[str], None] = print,
) -> tuple[Callable[..., Optional[str]], Callable[[], None]]:
    """按接码渠道返回 ``(wait_otp, sms_done)`` 两个回调。

    - herosms / smsbower：SMS-Activate 风格，同 aid 内 ``setStatus=3``
      让平台准备下一条 SMS，再阻塞 ``getStatus`` 拿码；成功后 ``setStatus=6``
      归还余额。一个 aid 跨注册/PIN/付款 3 次 OTP 都能续接。
    - smspool：先 ``/sms/resend`` 重发，再轮询 ``/sms/check`` 拿码；SMSPool
      一次性号付款阶段拿不到新码（号会停在 Completed 不再接），只能尽力
      ignore 注册旧码避免把它当付款 OTP；想稳就用 herosms / smsbower。
    """
    provider = (provider or "herosms").strip().lower()

    if provider == "smspool":
        from platforms.gopay.sms_channel import SmsPoolChannel, SMSPOOL_DEFAULT_API_KEY
        import os as _os

        key = (
            str(smspool_api_key or "").strip()
            or _os.environ.get("OPAI_SMSPOOL_API_KEY", "").strip()
            or SMSPOOL_DEFAULT_API_KEY
        )
        channel = SmsPoolChannel(api_key=key)
        # 付款前快照"旧码"：SMSPool 的 order 在注册阶段收过 OTP 后会一直停在
        # status=3 并缓存最后一条码，付款复用同一 order 时 /sms/check 会立刻
        # 吐回那条旧码。先记下它，等新码时排除，避免把注册旧码当付款 OTP
        # 提交（会被 GoPay 判 GoPay-900 / validate-otp 500）。
        old_code = None
        try:
            old_code = channel.peek_code(aid)
            if old_code:
                log(f"SMSPool 旧码快照={old_code}（付款时将忽略，只认 GoPay 新发的 OTP）")
        except Exception:
            old_code = None

        def _wait_otp(_phone_arg: str = "", timeout: int = 120) -> Optional[str]:
            # 触发 SMSPool resend（让它准备接收下一条短信），再等**新**码。
            try:
                channel.request_another(aid)
            except Exception:
                pass
            time.sleep(2)
            return channel.wait_code(aid, timeout=timeout, ignore_code=old_code)

        def _sms_done() -> None:
            # SMSPool 号用完即止，无需显式关闭。
            return None

        return _wait_otp, _sms_done

    if provider == "smsbower":
        from platforms.gopay.sms_channel import (
            make_smsbower_channel,
            SMSBOWER_DEFAULT_API_KEY,
        )
        import os as _os

        key = (
            str(smsbower_api_key or "").strip()
            or _os.environ.get("OPAI_SMSBOWER_API_KEY", "").strip()
            or SMSBOWER_DEFAULT_API_KEY
        )
        channel = make_smsbower_channel(api_key=key)

        def _wait_otp(_phone_arg: str = "", timeout: int = 120) -> Optional[str]:
            # SMSBower（SMS-Activate 风格）：先 setStatus=3 通知平台准备下一条
            # SMS，再阻塞 getStatus 拿码。同 aid 内能续接 3 次 OTP。
            try:
                channel.request_another(aid)
            except Exception:
                pass
            time.sleep(2)
            return channel.wait_code(aid, timeout=timeout)

        def _sms_done() -> None:
            channel.done(aid)

        return _wait_otp, _sms_done

    # 默认 Hero-SMS
    from opai.core.sms_helpers import sms_wait_code, sms_request_another, sms_api
    import os as _os

    api_key = (
        str(herosms_api_key or "").strip()
        or _os.environ.get("OPAI_HEROSMS_API_KEY", "").strip()
    )
    if not api_key:
        raise RuntimeError(
            "缺少 Hero-SMS API key（task payload 没传，"
            "环境变量 OPAI_HEROSMS_API_KEY 也没设）"
        )

    def _wait_otp(_phone_arg: str = "", timeout: int = 120) -> Optional[str]:
        try:
            sms_request_another(api_key, aid)
        except Exception:
            pass
        time.sleep(2)
        return sms_wait_code(api_key, aid, timeout=timeout)

    def _sms_done() -> None:
        sms_api(api_key, "setStatus", {"id": aid, "status": "6"})

    return _wait_otp, _sms_done


def execute_gopay_pay_chatgpt(
    *,
    chatgpt_account_id: int,
    gopay_account_id: Optional[int] = None,
    cashier_url_override: str = "",
    midtrans_url_override: str = "",
    country: str = "ID",
    currency: str = "IDR",
    headless: bool = False,
    checkout_mode: str = "camoufox_headed",
    bit_profile_id: str = "",
    envelope_url: str = "",
    proxy: Optional[str] = None,
    grab_timeout: int = 300,
    herosms_api_key_override: str = "",
    phone_ttl_seconds: int = 1200,
    auto_register_gopay: bool = False,
    gopay_pin: str = "000000",
    sms_provider: str = "herosms",
    smspool_api_key: str = "",
    smsbower_api_key: str = "",
    max_price: str = "",
    gopay_source: str = "auto",
    log: Callable[[str], None] = print,
    cancel_check: Optional[Callable[[], bool]] = None,
) -> dict:
    """整条流水线（同步）。

    Args:
        chatgpt_account_id: 主项目 ``accounts`` 表中 platform=chatgpt 的行 id
        gopay_account_id: 指定的 GoPay 号 id；为空则从池里挑一条余额 ≥ 1 的
        cashier_url_override: 跳过步骤 ①，直接用这个 cashier_url
        midtrans_url_override: 跳过步骤 ① + ②，直接用这个 midtrans_url
        country/currency: 默认印尼盾，跟 ChatGPT 印尼区订阅匹配
        checkout_mode: 浏览器模式 camoufox_headed/camoufox_headless/
            bitbrowser_headed/bitbrowser_hidden/bitbrowser_headless
        bit_profile_id: bitbrowser_* 模式必填
        envelope_url: GoPay 红包链接，选号后余额不足时领取补余额
        grab_timeout: 步骤 ② 等跳到 Midtrans 的最长秒数
        phone_ttl_seconds: Hero-SMS 号码有效期（默认 1200=20min），整条
            流水线超时即判失败
        gopay_source: GoPay 号来源开关——
            ``pool``=只用号池里已有的号（池空直接失败，不注册）；
            ``register``=强制现注册新号（忽略号池）；
            ``auto``（默认）=先查号池，池空再按 ``auto_register_gopay`` 决定
            是否注册（保持原有行为）。``gopay_account_id`` 显式指定时此开关无效。

    Returns:
        ``{"chatgpt_account_id", "gopay_account_id", "cashier_url",
           "midtrans_url", "payment": <pay 返回>}``
    """
    ttl_guard = PhoneTTLGuard(ttl_seconds=phone_ttl_seconds)
    out: dict[str, Any] = {
        "chatgpt_account_id": int(chatgpt_account_id),
        "gopay_account_id": None,
        "cashier_url": cashier_url_override or "",
        "midtrans_url": midtrans_url_override or "",
        "payment": {},
    }

    chatgpt = None
    if int(chatgpt_account_id) > 0:
        chatgpt = find_chatgpt_account(int(chatgpt_account_id))
        if not chatgpt:
            raise RuntimeError(f"ChatGPT 账号 #{chatgpt_account_id} 不存在或不是 chatgpt 平台")
    elif not midtrans_url_override:
        # chatgpt_account_id=0 占位仅在已提供 midtrans_url 时合法（需求 2：
        # 直接拿 url 付款，不关联具体 ChatGPT 账号）。
        raise RuntimeError("chatgpt_account_id 为 0 时必须提供 midtrans_url_override")

    # ① 拿 cashier_url（除非已 override）
    if not midtrans_url_override:
        ttl_guard.check()
        if not cashier_url_override:
            out["cashier_url"] = step_generate_cashier_url(
                chatgpt,
                country=country,
                currency=currency,
                proxy=proxy,
                log=log,
            )
        cashier_url = out["cashier_url"]

        # ② 浏览器抓 midtrans_url（自动选 GoPay + 填表 + 点订阅）
        ttl_guard.check()
        out["midtrans_url"] = step_grab_midtrans_url(
            cashier_url,
            checkout_mode=checkout_mode,
            bit_profile_id=bit_profile_id,
            proxy=proxy,
            timeout_seconds=grab_timeout,
            cancel_check=cancel_check,
            log=log,
        )
    midtrans_url = out["midtrans_url"]

    # ③ 选 GoPay 号 + 协议付款
    ttl_guard.check()
    source = str(gopay_source or "auto").strip().lower()

    def _do_register():
        ttl_guard.check()
        api_key_for_register = (
            herosms_api_key_override
            or os.environ.get("OPAI_HEROSMS_API_KEY", "")
        )
        return register_gopay_account(
            herosms_api_key=api_key_for_register,
            pin=gopay_pin,
            proxy=proxy or "",
            envelope_url=envelope_url,
            sms_provider=sms_provider,
            smspool_api_key=smspool_api_key,
            smsbower_api_key=smsbower_api_key,
            herosms_max_price_usd=max_price,
            smspool_max_price=max_price,
            log=log,
        )

    if source == "register":
        # 强制现注册新号：优先级高于 gopay_account_id（避免前端残留的指定号
        # 覆盖"强制注册"开关）。忽略号池和任何指定号。
        log("GoPay 号来源=强制注册：现注册一个新号（忽略号池/指定号）")
        gopay = _do_register()
        if not gopay:
            raise RuntimeError("强制注册 GoPay 号失败，详见上方日志")
    elif gopay_account_id:
        with Session(engine) as session:
            gopay = session.get(AccountModel, int(gopay_account_id))
            if not gopay or gopay.platform != "gopay":
                raise RuntimeError(
                    f"GoPay 账号 #{gopay_account_id} 不存在或不是 gopay 平台"
                )
    else:
        gopay = None

        if source == "pool":
            # 只用号池，池空直接失败，不注册。
            gopay = pick_available_gopay_account(min_balance_rp=1) or _latest_gopay_account()
            if not gopay:
                raise RuntimeError("GoPay 号来源=号池：池里没有可用号（且不允许注册）")
            log("GoPay 号来源=号池：复用已有号")
        else:
            # auto：先查池，池空再按 auto_register_gopay 决定是否注册。
            gopay = pick_available_gopay_account(min_balance_rp=1)
            if not gopay and auto_register_gopay:
                gopay = _do_register()
            if not gopay:
                # 注册也没成（或没开自动注册）：退而求其次取最新一条已注册的号，
                # 后面靠 wait_for_balance 轮询等红包/充值到账。
                gopay = _latest_gopay_account()
            if not gopay:
                raise RuntimeError("没有可用的 GoPay 账号，且无法自动注册")
    out["gopay_account_id"] = int(gopay.id)
    log(f"使用 GoPay 账号 #{gopay.id}（{gopay.email}）")

    # **需求 1**：余额不足不直接判失败——resume client 后轮询【领红包→查余额】
    # 直到余额 ≥ 1 或 20 分钟号码有效期超时。
    current_balance = int((_account_extra(gopay).get("balance_rp")) or 0)
    if current_balance < 1:
        log(f"GoPay 号 #{gopay.id} 当前余额 {current_balance} IDR，开始轮询等红包/充值到账…")
        gopay_extra = _account_extra(gopay)
        phone = str(gopay_extra.get("phone") or gopay.email or "").strip()
        register_proxy = _normalize_proxy_url(
            str(gopay_extra.get("register_proxy") or proxy or "").strip()
        )
        client = _resolve_gopay_client(phone, register_proxy, log=log)
        if client is None:
            raise RuntimeError(f"GoPay 号 #{gopay.id} 无法 resume（拿不到 client），无法轮询余额")
        final_balance = wait_for_balance(
            client=client,
            envelope_url=envelope_url,
            ttl_guard=ttl_guard,
            log=log,
        )
        # 余额到账，写回 graph
        from core.account_graph import patch_account_graph

        with Session(engine) as session:
            m = session.get(AccountModel, int(gopay.id))
            if m:
                patch_account_graph(session, m, summary_updates={"balance_rp": final_balance})
                session.commit()

    ttl_guard.check()
    out["payment"] = step_pay_with_gopay(
        midtrans_url,
        gopay,
        herosms_api_key_override=herosms_api_key_override,
        smspool_api_key_override=smspool_api_key,
        smsbower_api_key_override=smsbower_api_key,
        sms_provider_override=sms_provider,
        log=log,
    )

    # 把 ChatGPT 账号标 SUBSCRIBED 并存 cashier_url / midtrans_url
    # （chatgpt_account_id=0 占位场景跳过——没有关联的 ChatGPT 账号）
    if int(chatgpt_account_id) > 0:
        from core.account_graph import patch_account_graph

        with Session(engine) as session:
            m = session.get(AccountModel, int(chatgpt_account_id))
            if m:
                patch_account_graph(
                    session,
                    m,
                    lifecycle_status="subscribed",
                    cashier_url=out["cashier_url"] or None,
                    summary_updates={
                        "midtrans_url": out["midtrans_url"],
                        "paid_via": "gopay",
                        "paid_via_gopay_account_id": out["gopay_account_id"],
                        "plan_state": "subscribed",
                        "plan_name": "Plus",
                    },
                )
                session.commit()
        log("ChatGPT 账号已标记 subscribed")

    return out
