"""GoPay 接码渠道抽象 —— 支持 herosms（默认）、smspool 和 smsbower。

背景：``gopay-deploy`` 自带的 ``opai.core.sms_helpers`` 只对接 Hero-SMS，
API 形态是 ``getNumber/getStatus/setStatus``（activation_id 模型）。SMSPool
是另一套 REST API（order_id 模型，purchase/sms + sms/check + sms/resend +
sms/cancel）。

SMSBower 协议跟 Hero-SMS 完全一样（SMS-Activate 风格），只是 base URL 不同，
所以这里抽一个 ``SmsActivateStyleChannel``，SMSBower 是它的具体实例；以后
再接同协议的接码平台只要换 base URL 即可。

为了不改第三方 ``gopay-deploy`` 源码，这里用和 maxPrice patch 相同的思路：
``patch_worker_with_smspool`` / ``patch_worker_with_smsbower`` 直接覆盖
``gopay_protocol_worker`` 命名空间里的 ``sms_get_number/sms_wait_code/...``，
让注册流程（``_register_one``）无感切到对应渠道。

SMSPool API 文档：https://www.smspool.net/article/how-to-use-the-smspool-api
- POST /purchase/sms  key,country,service[,pool] -> {success, number, order_id, cc}
- POST /sms/check     key,orderid -> {status, sms}   status=3 表示完成
- POST /sms/resend    key,orderid
- POST /sms/cancel    key,orderid

SMSBower API 文档：https://smsbower.app/cn/api
- GET/POST /stubs/handler_api.php?api_key=xxx&action=getNumber&service=ni&country=6
  → ``ACCESS_NUMBER:<aid>:<phone>`` / ``NO_NUMBERS`` / ``BAD_KEY`` 等
- action=getStatus,id=<aid> → ``STATUS_OK:<code>`` / ``STATUS_WAIT_CODE`` / ``STATUS_CANCEL``
- action=setStatus,id=<aid>,status=3 让平台准备下一条 SMS（同 aid 复用）
- action=setStatus,id=<aid>,status=6 标记已完成（归还余额）
- action=setStatus,id=<aid>,status=8 取消激活

国家 / 服务标识：SMSPool 用自己的 country id 和 service id；SMSBower / Hero-SMS
用同一套（country=6 印度尼西亚，service=ni Gojek/GoPay）。这里默认值取
环境变量，找不到回退到字符串（用户在对应平台后台查到真实 id 后通过 extra / env
覆盖）。
"""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Optional

import tls_client

log = logging.getLogger(__name__)

SMSPOOL_API = "https://api.smspool.net"
SMSPOOL_DEFAULT_API_KEY = ""
# 印尼 country / Gojek(GoPay) service —— SMSPool 用自己的 id 体系。
# 默认取环境变量，没配就用占位字符串，用户可在 SMSPool 后台查到真实 id 后
# 通过 extra / env 覆盖。
# 印尼 country id = 9（用户确认）。SMSPool 部分端点要数字 id。
SMSPOOL_DEFAULT_COUNTRY = os.environ.get("OPAI_SMSPOOL_COUNTRY", "9")
# GoJek(GoPay) 在 SMSPool 的 service id = 392（用户确认）。
SMSPOOL_DEFAULT_SERVICE = os.environ.get("OPAI_SMSPOOL_SERVICE", "392")
# 购号价格上限（USD）。**这是上限不是目标价**：SMSPool 只保证不超过该价，
# 不保证买到最便宜的号。想买便宜号要把它压到接近实时最低价（见 get_price）。
# 空串/None 表示不传 max_price（让服务端按默认价出号）。默认 0.11。
SMSPOOL_DEFAULT_MAX_PRICE = os.environ.get("OPAI_SMSPOOL_MAX_PRICE", "0.11")
# pricing_option: 0=优先最低价池（可能无货时拿不到号），1=优先有货/成功率高
# 的池（可能更贵）。默认 0。
SMSPOOL_DEFAULT_PRICING_OPTION = os.environ.get("OPAI_SMSPOOL_PRICING_OPTION", "0")
SMS_TIMEOUT = 180


def _new_session() -> "tls_client.Session":
    return tls_client.Session(client_identifier="chrome_120")


class SmsPoolChannel:
    """SMSPool 接码渠道。接口与 worker 期望的 (phone, id) 元组语义对齐。"""

    def __init__(
        self,
        api_key: str,
        *,
        country: str = "",
        service: str = "",
        pool: str = "",
        max_price: str = "",
        pricing_option: str = "",
    ):
        self.api_key = str(api_key or "").strip() or SMSPOOL_DEFAULT_API_KEY
        self.country = str(country or "").strip() or SMSPOOL_DEFAULT_COUNTRY
        self.service = str(service or "").strip() or SMSPOOL_DEFAULT_SERVICE
        self.pool = str(pool or "").strip()
        # max_price 显式传空字符串时用默认；传 "0" 也视为有效上限（不覆盖）
        mp = str(max_price).strip() if max_price is not None else ""
        self.max_price = mp if mp != "" else SMSPOOL_DEFAULT_MAX_PRICE
        po = str(pricing_option).strip() if pricing_option is not None else ""
        self.pricing_option = po if po != "" else SMSPOOL_DEFAULT_PRICING_OPTION

    def _post(self, path: str, params: dict, retries: int = 3) -> dict:
        body = {"key": self.api_key, **params}
        last_exc: Optional[Exception] = None
        for i in range(1, retries + 1):
            try:
                s = _new_session()
                r = s.post(f"{SMSPOOL_API}{path}", data=body, timeout_seconds=30)
                try:
                    return r.json()
                except Exception:
                    return {"raw": getattr(r, "text", ""), "status_code": r.status_code}
            except Exception as exc:
                last_exc = exc
                log.debug("smspool %s attempt %d: %s", path, i, exc)
                if i < retries:
                    time.sleep(3)
        log.warning("smspool %s failed after %d retries: %s", path, retries, last_exc)
        return {}

    def get_price(self) -> dict:
        """查 country+service 的实时价。返回 ``/request/price`` 的原始 dict。

        典型响应：``{"price":"0.06","high_price":"0.10","success_rate":58}``
        - ``price``：当前**最低**可用池价（USD）
        - ``high_price``：当前**最高**池价（USD）
        失败返回 ``{}``。
        """
        data = self._post("/request/price", {"country": self.country, "service": self.service})
        return data if isinstance(data, dict) else {}

    def get_number(self) -> tuple[str | None, str | None]:
        """购买一个号。返回 ``(phone_e164, order_id)``，失败返回 ``(None, None)``。

        **关于 max_price**：它是「价格上限」不是「目标价」。SMSPool 会在不超过
        ``max_price`` 的前提下出一个**当时有货**的号——不保证是最便宜的那个
        （最低价池没库存时会回退到更贵但有货的池）。所以想买便宜号要把
        ``max_price`` 压到接近最低价（用 ``get_price()`` 查），而不是设个大上限。
        购号成功后把实付价打到日志，方便核对到底花了多少。
        """
        params = {"country": self.country, "service": self.service}
        if self.pool:
            params["pool"] = self.pool
        if self.max_price not in ("", None):
            params["max_price"] = str(self.max_price)
        if self.pricing_option not in ("", None):
            params["pricing_option"] = str(self.pricing_option)
        data = self._post("/purchase/sms", params)
        if not isinstance(data, dict) or int(data.get("success") or 0) != 1:
            log.warning("smspool purchase failed (max_price=%s): %s", self.max_price, data)
            return None, None
        number = str(data.get("number") or data.get("phonenumber") or "").strip()
        order_id = str(data.get("order_id") or data.get("orderid") or "").strip()
        if not number or not order_id:
            log.warning("smspool purchase missing number/order_id: %s", data)
            return None, None
        # 实付价：purchase 响应里常见字段名 cost / price。打到日志便于核对。
        cost = data.get("cost")
        if cost is None:
            cost = data.get("price")
        log.info(
            "smspool 购号成功 number=%s order_id=%s 实付=%s USD (max_price=%s, pricing_option=%s)",
            number, order_id, cost if cost is not None else "?",
            self.max_price, self.pricing_option,
        )
        phone = number if number.startswith("+") else f"+{number}"
        return phone, order_id

    def peek_code(self, order_id: str) -> str | None:
        """单次查 ``/sms/check``，返回当前已收到的验证码（status=3）或 None。

        用于付款前快照"旧码"——注册阶段收过的 OTP 会让 order 停在 status=3，
        付款时必须先记下它，等新码时把它排除掉，避免把旧码当付款 OTP 提交。
        """
        data = self._post("/sms/check", {"orderid": order_id})
        if isinstance(data, dict) and int(data.get("status") or 0) == 3:
            sms = str(data.get("sms") or data.get("code") or "").strip()
            if sms:
                m = re.search(r"\b(\d{4,6})\b", sms)
                return m.group(1) if m else sms
        return None

    def wait_code(
        self,
        order_id: str,
        timeout: int = SMS_TIMEOUT,
        *,
        ignore_code: str | None = None,
    ) -> str | None:
        """轮询 ``/sms/check`` 直到 status=3 拿到验证码，否则超时返回 None。

        ``ignore_code``：付款阶段传入注册时的旧码。SMSPool 的 order 收过短信后
        一直停在 status=3 并缓存最后一条码；付款复用同一 order 时 ``/sms/check``
        会立刻返回那条旧码。传入 ``ignore_code`` 后，只有当返回的码**不同于**
        旧码（即 GoPay 新发的付款 OTP 到达）才认作有效，否则继续等。
        """
        ignore = str(ignore_code or "").strip()
        deadline = time.monotonic() + max(int(timeout or 0), 0)
        while time.monotonic() < deadline:
            data = self._post("/sms/check", {"orderid": order_id})
            if isinstance(data, dict):
                status = int(data.get("status") or 0)
                sms = str(data.get("sms") or data.get("code") or "").strip()
                if status == 3 and sms:
                    m = re.search(r"\b(\d{4,6})\b", sms)
                    code = m.group(1) if m else sms
                    # 还是注册时的旧码 → GoPay 新 OTP 尚未到达，继续等
                    if ignore and code == ignore:
                        time.sleep(5)
                        continue
                    return code
                # status 6 = refunded/cancelled
                if status == 6:
                    log.warning("smspool order %s cancelled/refunded", order_id)
                    return None
            time.sleep(5)
        return None

    def request_another(self, order_id: str) -> bool:
        """让 SMSPool 对同一 order 再发一条（resend）。"""
        data = self._post("/sms/resend", {"orderid": order_id})
        return isinstance(data, dict) and int(data.get("success") or 0) == 1

    def cancel(self, order_id: str) -> None:
        try:
            self._post("/sms/cancel", {"orderid": order_id})
        except Exception:
            pass


def patch_worker_with_smspool(
    *,
    api_key: str,
    country: str = "",
    service: str = "",
    pool: str = "",
    max_price: str = "",
    pricing_option: str = "",
) -> None:
    """覆盖 ``gopay_protocol_worker`` 命名空间里的 5 个 sms 函数走 SMSPool。

    ``_register_one`` 用 ``from .sms_helpers import sms_get_number`` 等形式
    把名字绑到 worker 模块本地，所以 patch 必须打在 worker 模块上（同
    maxPrice patch）。herosms 渠道不调用本函数，保持 worker 原生实现。

    幂等：重复调用只是用最新参数重新封装。worker 期望的函数签名：
      sms_get_number(api_key) -> (phone, id)
      sms_wait_code(api_key, id, timeout=...) -> code|None
      sms_request_another(api_key, id) -> bool
      sms_cancel(api_key, id) -> None
      sms_done(api_key, id) -> None
    第一个 ``api_key`` 参数被忽略（channel 自带 key），保持签名兼容。
    """
    from opai.core import gopay_protocol_worker as _worker

    channel = SmsPoolChannel(
        api_key=api_key, country=country, service=service, pool=pool,
        max_price=max_price, pricing_option=pricing_option,
    )

    def _get_number(_api_key):
        return channel.get_number()

    def _wait_code(_api_key, order_id, timeout: int = SMS_TIMEOUT):
        return channel.wait_code(order_id, timeout=timeout)

    def _request_another(_api_key, order_id):
        return channel.request_another(order_id)

    def _cancel(_api_key, order_id):
        channel.cancel(order_id)

    def _done(_api_key, order_id):
        # SMSPool 没有显式 "done/complete" 概念，号用完即结束（不退款），
        # 这里 no-op。
        return None

    _worker.sms_get_number = _get_number
    _worker.sms_wait_code = _wait_code
    _worker.sms_request_another = _request_another
    _worker.sms_cancel = _cancel
    _worker.sms_done = _done
    log.info("gopay worker sms 函数已切换到 SMSPool 渠道")


# ---------------------------------------------------------------------------
# SMSBower（SMS-Activate 风格协议，与 Hero-SMS 完全兼容）
# ---------------------------------------------------------------------------

SMSBOWER_API = "https://smsbower.page/stubs/handler_api.php"
SMSBOWER_DEFAULT_API_KEY = ""
# 印度尼西亚 country=6（用户确认）
SMSBOWER_DEFAULT_COUNTRY = os.environ.get("OPAI_SMSBOWER_COUNTRY", "6")
# Gojek/GoPay service=ni（用户确认）
SMSBOWER_DEFAULT_SERVICE = os.environ.get("OPAI_SMSBOWER_SERVICE", "ni")


class SmsActivateStyleChannel:
    """SMS-Activate 风格通用接码渠道（Hero-SMS / SMSBower 共用同一协议）。

    协议形态：
      GET ``<base_url>?api_key=xxx&action=getNumber&service=ni&country=6``
        → ``ACCESS_NUMBER:<aid>:<phone>`` 或 ``NO_NUMBERS`` / ``BAD_KEY`` 等
      action=getStatus,id=<aid>      → ``STATUS_OK:<code>`` / ``STATUS_WAIT_CODE``
      action=setStatus,id=<aid>,status=3  让平台准备下一条 SMS（同 aid 复用）
      action=setStatus,id=<aid>,status=6  标记已完成（归还余额）
      action=setStatus,id=<aid>,status=8  取消激活

    一个 activation_id 内能多次 ``setStatus=3`` 续接新短信，正好覆盖 GoPay
    注册→PIN→付款 3 次 OTP，扛得住。
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        service: str = "ni",
        country: str = "6",
    ):
        self.base_url = str(base_url or "").strip()
        self.api_key = str(api_key or "").strip()
        self.service = str(service or "").strip() or "ni"
        self.country = str(country or "").strip() or "6"

    def _request(self, action: str, params: dict | None = None, retries: int = 3) -> str:
        p = {"api_key": self.api_key, "action": action}
        if params:
            p.update(params)
        for i in range(1, retries + 1):
            try:
                s = _new_session()
                r = s.get(self.base_url, params=p, timeout_seconds=30)
                return (r.text or "").strip()
            except Exception as exc:
                log.debug("smsactivate %s attempt %d: %s", action, i, exc)
                if i < retries:
                    time.sleep(3)
        raise RuntimeError(f"sms api {action} failed after {retries} retries")

    def get_number(self) -> tuple[str | None, str | None]:
        resp = self._request("getNumber", {"service": self.service, "country": self.country})
        log.info("getNumber: %s", resp)
        if resp.startswith("ACCESS_NUMBER:"):
            parts = resp.split(":")
            return f"+{parts[2]}", parts[1]
        log.warning("getNumber failed: %s", resp)
        return None, None

    def wait_code(self, aid: str, timeout: int = SMS_TIMEOUT) -> str | None:
        deadline = time.time() + max(int(timeout or 0), 0)
        while time.time() < deadline:
            try:
                resp = self._request("getStatus", {"id": aid})
            except Exception:
                time.sleep(5)
                continue
            if resp.startswith("STATUS_OK:"):
                code = resp.split(":", 1)[1]
                m = re.search(r"\b(\d{4,6})\b", code)
                return m.group(1) if m else code
            if resp == "STATUS_CANCEL":
                log.warning("SMS activation %s cancelled", aid)
                return None
            time.sleep(5)
        return None

    def request_another(self, aid: str) -> bool:
        try:
            resp = self._request("setStatus", {"id": aid, "status": "3"})
            log.info("sms_request_another: %s", resp)
            return "ACCESS_RETRY_GET" in resp
        except Exception:
            return False

    def cancel(self, aid: str) -> None:
        try:
            self._request("setStatus", {"id": aid, "status": "8"})
        except Exception:
            pass

    def done(self, aid: str) -> None:
        try:
            self._request("setStatus", {"id": aid, "status": "6"})
        except Exception:
            pass


def make_smsbower_channel(api_key: str = "", *, service: str = "", country: str = "") -> SmsActivateStyleChannel:
    """构造 SMSBower 渠道（带默认值兜底）。"""
    return SmsActivateStyleChannel(
        base_url=SMSBOWER_API,
        api_key=str(api_key or "").strip() or SMSBOWER_DEFAULT_API_KEY,
        service=str(service or "").strip() or SMSBOWER_DEFAULT_SERVICE,
        country=str(country or "").strip() or SMSBOWER_DEFAULT_COUNTRY,
    )


def patch_worker_with_smsbower(
    *,
    api_key: str = "",
    service: str = "",
    country: str = "",
) -> None:
    """覆盖 ``gopay_protocol_worker`` 的 5 个 sms 函数走 SMSBower。

    与 ``patch_worker_with_smspool`` 同一思路。SMSBower 协议跟 Hero-SMS
    完全一致（都是 SMS-Activate 风格），所以 worker 用同一个 aid 跨注册/PIN/
    付款 3 次 OTP 都能续接，扛得住 GoPay 全生命周期。

    幂等：重复调用只是用最新参数重新封装。
    """
    from opai.core import gopay_protocol_worker as _worker

    channel = make_smsbower_channel(api_key=api_key, service=service, country=country)

    def _get_number(_api_key):
        return channel.get_number()

    def _wait_code(_api_key, aid, timeout: int = SMS_TIMEOUT):
        return channel.wait_code(aid, timeout=timeout)

    def _request_another(_api_key, aid):
        return channel.request_another(aid)

    def _cancel(_api_key, aid):
        channel.cancel(aid)

    def _done(_api_key, aid):
        channel.done(aid)

    _worker.sms_get_number = _get_number
    _worker.sms_wait_code = _wait_code
    _worker.sms_request_another = _request_another
    _worker.sms_cancel = _cancel
    _worker.sms_done = _done
    log.info("gopay worker sms 函数已切换到 SMSBower 渠道")
