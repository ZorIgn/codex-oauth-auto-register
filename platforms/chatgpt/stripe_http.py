"""Stripe Checkout (pay.openai.com hosted) 协议层。

通过纯 HTTP 调用替代浏览器自动化的 Stripe checkout 早期阶段：

1. ``stripe_init``                       —— 启动 checkout session，拿 ``init_checksum``
2. ``stripe_update_tax_region``          —— 提交账单地址（用于税务）
3. ``stripe_create_paypal_payment_method`` —— 创建 type=paypal 的 PaymentMethod
4. ``stripe_confirm_paypal``             —— 触发支付，从响应中拿到 PayPal redirect URL
5. ``stripe_poll``                       —— 轮询订阅完成状态

所有请求格式来自 ``tools/captures/checkout-*.har`` 实采。仅 PayPal 注册本身仍需浏览器。
"""

from __future__ import annotations

import re
import secrets
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

STRIPE_API_BASE = "https://api.stripe.com/v1"
# OpenAI 的 publishable key，pay.openai.com 页面里硬编码可见
STRIPE_PUBLISHABLE_KEY = (
    "pk_live_51HOrSwC6h1nxGoI3lTAgRjYVrz4dU3fVOabyCcKR3pbEJguCVAlqCxdxCUvoRh1XWwRacViovU3kLKvpkjh7IqkW00iXQsjo3n"
)
STRIPE_VERSION = "2020-08-27;custom_checkout_beta=v1"
STRIPE_JS_VERSION = "58d9408f11"
STRIPE_PAYMENT_USER_AGENT = (
    f"stripe.js/{STRIPE_JS_VERSION}; stripe-js-v3/{STRIPE_JS_VERSION}; checkout"
)

_CS_RE = re.compile(r"cs_(?:live|test)_[A-Za-z0-9]+")


def extract_checkout_session_id(url: str) -> str:
    """从 ``pay.openai.com/c/pay/cs_live_...`` 这类 URL 抽出 ``cs_live_xxx`` / ``cs_test_xxx``。"""
    match = _CS_RE.search(str(url or ""))
    if not match:
        raise ValueError(f"无法从 URL 提取 checkout session id: {url!r}")
    return match.group(0)


def _device_token() -> str:
    """生成 Stripe 风格的设备 token: UUIDv4 + 6 hex 后缀（与 HAR 实采一致）。"""
    return f"{uuid.uuid4()}{secrets.token_hex(3)}"


@dataclass
class StripeDeviceContext:
    """单次 checkout 内复用的 Stripe.js 设备/会话标识。"""

    guid: str = field(default_factory=_device_token)
    muid: str = field(default_factory=_device_token)
    sid: str = field(default_factory=_device_token)
    client_session_id: str = field(default_factory=lambda: str(uuid.uuid4()))


def _stripe_headers() -> dict:
    return {
        "Origin": "https://pay.openai.com",
        "Referer": "https://pay.openai.com/",
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.5",
        "Sec-Fetch-Site": "cross-site",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
        "Content-Type": "application/x-www-form-urlencoded",
    }


class StripeHttpError(RuntimeError):
    """Stripe API 返回 4xx/5xx 时抛出，携带 status / request-id / body 诊断。

    Stripe 错误响应 body 通常是 JSON：``{"error":{"code":"...","message":"...","type":"..."}}``。
    我们把整个 body 前 1KB 包进异常 message，方便协议模式 ``stage_*`` 把它打到
    日志里——之前 ``raise_for_status`` 默认只暴露 ``"HTTP Error 400: "`` 空字符串，
    根本看不出 Stripe 拒绝的真实原因。
    """

    def __init__(
        self,
        *,
        method: str,
        url: str,
        status: Optional[int],
        body_preview: str,
        request_id: str,
        cause: Optional[BaseException] = None,
    ) -> None:
        self.method = method
        self.url = url
        self.status = status
        self.body_preview = body_preview
        self.request_id = request_id
        self.cause = cause
        # 短 URL（去掉 cs_id 后面的查询参数和 hash）
        short_url = url.split("?", 1)[0]
        super().__init__(
            f"Stripe {method} {short_url} → status={status} "
            f"request-id={request_id!r} body={body_preview[:512]!r}"
        )


def _request(method: str, session, url: str, *, data: Optional[dict] = None,
             params: Optional[dict] = None) -> Any:
    if method == "POST":
        resp = session.post(url, data=data, headers=_stripe_headers())
    elif method == "GET":
        resp = session.get(url, params=params or None, headers=_stripe_headers())
    else:
        raise ValueError(f"unsupported method: {method!r}")

    status = getattr(resp, "status_code", None)
    body_text = getattr(resp, "text", "") or ""
    headers_attr = getattr(resp, "headers", None) or {}
    if hasattr(headers_attr, "get"):
        request_id = str(
            headers_attr.get("request-id")
            or headers_attr.get("Request-Id")
            or headers_attr.get("X-Request-Id")
            or ""
        )
    else:
        request_id = ""

    if hasattr(resp, "raise_for_status"):
        try:
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001 — requests.HTTPError etc.
            raise StripeHttpError(
                method=method, url=url, status=status,
                body_preview=body_text[:1024], request_id=request_id, cause=exc,
            ) from exc

    try:
        return resp.json() if hasattr(resp, "json") else None
    except Exception as exc:  # noqa: BLE001 — JSONDecodeError etc.
        raise StripeHttpError(
            method=method, url=url, status=status,
            body_preview=body_text[:1024], request_id=request_id, cause=exc,
        ) from exc


def _post(session, url: str, data: dict) -> Any:
    return _request("POST", session, url, data=data)


def _get(session, url: str, params: Optional[dict] = None) -> Any:
    return _request("GET", session, url, params=params)


def stripe_init(
    session,
    *,
    cs_id: str,
    browser_locale: str = "en-US",
    browser_timezone: str = "America/Los_Angeles",
) -> dict:
    """``POST /v1/payment_pages/{cs}/init``。返回完整 checkout session 对象。"""
    body = {
        "key": STRIPE_PUBLISHABLE_KEY,
        "eid": "NA",
        "browser_locale": browser_locale,
        "browser_timezone": browser_timezone,
        "redirect_type": "url",
    }
    return _post(session, f"{STRIPE_API_BASE}/payment_pages/{cs_id}/init", body)


def stripe_update_tax_region(
    session,
    *,
    cs_id: str,
    address: dict,
    eid: Optional[str] = None,
) -> dict:
    """``POST /v1/payment_pages/{cs}``，提交税务地址（country/state/postal/line1/city）。"""
    body = {
        "eid": eid or str(uuid.uuid4()),
        "tax_region[country]": str(address.get("country") or "US"),
        "tax_region[state]": str(address.get("state") or ""),
        "tax_region[postal_code]": str(address.get("postal_code") or ""),
        "tax_region[line1]": str(address.get("line1") or ""),
        "tax_region[city]": str(address.get("city") or ""),
        "key": STRIPE_PUBLISHABLE_KEY,
    }
    return _post(session, f"{STRIPE_API_BASE}/payment_pages/{cs_id}", body)


def stripe_create_paypal_payment_method(
    session,
    *,
    cs_id: str,
    address: dict,
    email: str,
    device: StripeDeviceContext,
    config_id: str = "",
) -> dict:
    """``POST /v1/payment_methods``，建 ``type=paypal`` PaymentMethod，返回 ``pm_xxx``。"""
    body = {
        "type": "paypal",
        "billing_details[email]": str(email or ""),
        "billing_details[address][country]": str(address.get("country") or "US"),
        "billing_details[address][line1]": str(address.get("line1") or ""),
        "billing_details[address][city]": str(address.get("city") or ""),
        "billing_details[address][postal_code]": str(address.get("postal_code") or ""),
        "billing_details[address][state]": str(address.get("state") or ""),
        "guid": device.guid,
        "muid": device.muid,
        "sid": device.sid,
        "_stripe_version": STRIPE_VERSION,
        "key": STRIPE_PUBLISHABLE_KEY,
        "payment_user_agent": STRIPE_PAYMENT_USER_AGENT,
        "client_attribution_metadata[client_session_id]": device.client_session_id,
        "client_attribution_metadata[checkout_session_id]": cs_id,
        "client_attribution_metadata[merchant_integration_source]": "checkout",
        "client_attribution_metadata[merchant_integration_version]": "hosted_checkout",
        "client_attribution_metadata[payment_method_selection_flow]": "automatic",
    }
    if config_id:
        body["client_attribution_metadata[checkout_config_id]"] = config_id
    return _post(session, f"{STRIPE_API_BASE}/payment_methods", body)


def extract_expected_amount(init_resp: dict) -> str:
    """从 ``stripe_init`` 响应里抽取 ``expected_amount`` 字符串（cents）。

    ``/confirm`` 请求里的 ``expected_amount`` 必须与 Stripe 服务端最新 invoice
    金额**精确匹配**，否则 Stripe 报 ``checkout_amount_mismatch``。HAR 实采里
    成功 case 的 init 响应字段层级（金额优先级从高到低）：

    * ``elements_options.amount`` —— Stripe Elements 客户端用的金额（最权威，
      Stripe checkout SDK 直接读这个传给 confirm）
    * ``invoice.amount_due`` —— 折扣 + tax 后的应付金额
    * ``invoice.total`` —— 折扣后 / tax 前金额

    HAR 实采里这三个都是 ``0``（100% off coupon trial），但 ChatGPT plus 现在
    没 trial 时这些会是 ``2000`` (=$20)。**硬编码 ``"0"`` 是历史遗留 bug**——
    用户账号无 trial 资格时整链直接 400。我们改成从响应动态读，缺失字段时
    fallback 到 ``"0"`` 保留旧行为。
    """
    if not isinstance(init_resp, dict):
        return "0"
    elements_options = init_resp.get("elements_options")
    if isinstance(elements_options, dict) and "amount" in elements_options:
        amount = elements_options.get("amount")
        if amount is not None:
            return str(int(amount))
    invoice = init_resp.get("invoice")
    if isinstance(invoice, dict):
        for key in ("amount_due", "total"):
            if key in invoice and invoice[key] is not None:
                return str(int(invoice[key]))
    return "0"


def stripe_confirm_paypal(
    session,
    *,
    cs_id: str,
    payment_method_id: str,
    init_checksum: str,
    device: StripeDeviceContext,
    return_url_origin: str = "https://pay.openai.com",
    config_id: str = "",
    expected_amount: str = "0",
) -> dict:
    """``POST /v1/payment_pages/{cs}/confirm``，触发实际支付，响应里含 PayPal redirect URL。

    ``expected_amount`` 必须与 ``stripe_init`` 响应里的 ``elements_options.amount``
    精确匹配（cents 字符串），否则 Stripe 报 ``checkout_amount_mismatch``。调用
    方应当用 :func:`extract_expected_amount` 从 init 响应里取，**不要**继续靠
    默认值 ``"0"``——那只在 100% off trial 资格时才正确。
    """
    return_url = f"{return_url_origin}/c/pay/{cs_id}?redirect_pm_type=paypal&ui_mode=hosted"
    body = {
        "eid": "NA",
        "payment_method": payment_method_id,
        "expected_amount": str(expected_amount),
        "consent[terms_of_service]": "accepted",
        "expected_payment_method_type": "paypal",
        "return_url": return_url,
        "_stripe_version": STRIPE_VERSION,
        "guid": device.guid,
        "muid": device.muid,
        "sid": device.sid,
        "key": STRIPE_PUBLISHABLE_KEY,
        "version": STRIPE_JS_VERSION,
        "init_checksum": init_checksum,
        "client_attribution_metadata[client_session_id]": device.client_session_id,
        "client_attribution_metadata[checkout_session_id]": cs_id,
        "client_attribution_metadata[merchant_integration_source]": "checkout",
        "client_attribution_metadata[merchant_integration_version]": "hosted_checkout",
        "client_attribution_metadata[payment_method_selection_flow]": "automatic",
    }
    if config_id:
        body["client_attribution_metadata[checkout_config_id]"] = config_id
    return _post(session, f"{STRIPE_API_BASE}/payment_pages/{cs_id}/confirm", body)


def stripe_poll(session, *, cs_id: str) -> dict:
    """``GET /v1/payment_pages/{cs}/poll``，返回 ``state`` 与 ``success_url``。"""
    return _get(
        session,
        f"{STRIPE_API_BASE}/payment_pages/{cs_id}/poll",
        params={"key": STRIPE_PUBLISHABLE_KEY},
    )


def extract_paypal_redirect_url(confirm_resp: dict) -> tuple[str, str]:
    """从 ``/confirm`` 响应抽取 PayPal redirect URL 与 return URL。

    Stripe 在 ``/confirm`` 响应里**根据 amount 不同走两条路径**：

    * **trial / $0 订阅**（HAR 实采）：用 ``setup_intent`` —— 仅"绑定 PayPal
      用于将来计费"，不当场收款，所以是 SetupIntent
    * **非 trial / 真收款**（用户实际场景）：用 ``payment_intent`` —— 当场
      payment $20，所以是 PaymentIntent

    两者结构相同：``next_action.redirect_to_url.{url, return_url}``。这里两条
    路径都尝试，谁先非空用谁。返回 ``(redirect_url, return_url)``；都缺时抛
    ``ValueError`` 并带响应 keys 摘要便于排查。
    """
    for key in ("setup_intent", "payment_intent"):
        intent = confirm_resp.get(key) or {}
        next_action = (intent.get("next_action") or {}) if isinstance(intent, dict) else {}
        redirect = next_action.get("redirect_to_url") or {} if isinstance(next_action, dict) else {}
        redirect_url = str((redirect or {}).get("url") or "").strip()
        return_url = str((redirect or {}).get("return_url") or "").strip()
        if redirect_url:
            return redirect_url, return_url
    # 都没有 — 给个能定位的 error message
    top_keys = list(confirm_resp.keys()) if isinstance(confirm_resp, dict) else []
    raise ValueError(
        "Stripe /confirm 响应缺少 next_action.redirect_to_url.url "
        f"(setup_intent / payment_intent 都没有); response top keys={top_keys[:20]}"
    )


# 终态/进行中的 Stripe checkout state 取值（HAR 里见过 succeeded、active、processing；
# 失败语义按经验补 failed、cancelled、expired）。
_TERMINAL_SUCCESS_STATES = frozenset({"succeeded", "complete"})
_TERMINAL_FAILURE_STATES = frozenset({"failed", "cancelled", "canceled", "expired"})
_PENDING_STATES = frozenset({"active", "open", "processing", "pending", "requires_action"})


def classify_poll_state(poll_resp: dict) -> str:
    """把 ``/poll`` 响应里的 ``state`` 归一到 ``success | failure | pending`` 三态。"""
    raw = str((poll_resp or {}).get("state") or "").strip().lower()
    if raw in _TERMINAL_SUCCESS_STATES:
        return "success"
    if raw in _TERMINAL_FAILURE_STATES:
        return "failure"
    return "pending"


def extract_poll_success_url(poll_resp: dict) -> str:
    """从 ``/poll`` 响应抽取最终 ``success_url``；缺字段时抛 ``ValueError``。"""
    success_url = str((poll_resp or {}).get("success_url") or "").strip()
    if not success_url:
        raise ValueError("Stripe /poll 响应缺少 success_url")
    return success_url
