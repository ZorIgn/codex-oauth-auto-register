"""接码服务基类 + SMS-Activate / HeroSMS 实现。"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import requests

logger = logging.getLogger(__name__)


@dataclass
class SmsActivation:
    """Represents an active phone number rental."""
    activation_id: str
    phone_number: str
    country: str = ""
    metadata: dict = field(default_factory=dict)


class BaseSmsProvider(ABC):
    """Base class for SMS verification code providers."""

    auto_report_success_on_code = True

    @abstractmethod
    def get_number(self, *, service: str, country: str = "") -> SmsActivation:
        """Rent a phone number for the given service."""
        ...

    @abstractmethod
    def get_code(self, activation_id: str, *, timeout: int = 120) -> str:
        """Wait for and return the SMS verification code."""
        ...

    @abstractmethod
    def cancel(self, activation_id: str) -> bool:
        """Cancel/release an activation. Returns True on success."""
        ...

    def report_success(self, activation_id: str) -> bool:
        """Report that the code was used successfully (optional)."""
        return True

    def set_resend_callback(self, callback: Callable[[], None] | None) -> None:
        """Optional hook used by providers that can request upstream resend."""
        return None

    def mark_code_failed(self, activation_id: str, reason: str = "") -> None:
        """Optional hook used when the target service rejects a received code."""
        return None

    def mark_send_failed(self, activation_id: str, reason: str = "") -> None:
        """Optional hook used when the target service rejects the rented phone."""
        return None

    def mark_send_succeeded(self, activation_id: str) -> None:
        """Optional hook used when the target service accepts the rented phone."""
        return None

    def get_reuse_info(self) -> dict:
        """Return provider-specific reuse state for task scheduling."""
        return {}


# ---------------------------------------------------------------------------
# SMS-Activate implementation (https://sms-activate.guru)
# ---------------------------------------------------------------------------

SMS_ACTIVATE_SERVICES = {
    "cursor": "ot",
    "chatgpt": "dr",
    "openai": "dr",
    "google": "go",
    "microsoft": "mg",
    "default": "ot",
}

SMS_ACTIVATE_COUNTRIES = {
    "ru": "0",
    "us": "187",
    "uk": "16",
    "in": "22",
    "id": "6",
    "ph": "4",
    "th": "52",
    "br": "73",
    "default": "0",
}


def _resolve_sms_activate_country_id(country: str, default_country: str) -> str:
    raw = str(country or default_country or "").strip().lower()
    if not raw:
        raw = "default"
    if raw.isdigit():
        return raw
    return SMS_ACTIVATE_COUNTRIES.get(raw, SMS_ACTIVATE_COUNTRIES["default"])


class SmsActivateProvider(BaseSmsProvider):
    """SMS-Activate (sms-activate.guru) provider."""

    BASE_URL = "https://api.sms-activate.guru/stubs/handler_api.php"

    def __init__(self, api_key: str, *, default_country: str = "", proxy: str = None):
        self.api_key = api_key
        self.default_country = default_country or "ru"
        self._proxy = {"http": proxy, "https": proxy} if proxy else None

    def _request(self, action: str, **params) -> str:
        params["api_key"] = self.api_key
        params["action"] = action
        resp = requests.get(
            self.BASE_URL,
            params=params,
            timeout=20,
            proxies=self._proxy,
        )
        resp.raise_for_status()
        return resp.text.strip()

    def get_balance(self) -> float:
        result = self._request("getBalance")
        if result.startswith("ACCESS_BALANCE:"):
            return float(result.split(":")[1])
        raise RuntimeError(f"SMS-Activate getBalance failed: {result}")

    def get_number(self, *, service: str, country: str = "") -> SmsActivation:
        service_code = SMS_ACTIVATE_SERVICES.get(service, SMS_ACTIVATE_SERVICES["default"])
        country_id = _resolve_sms_activate_country_id(country, self.default_country)

        result = self._request("getNumber", service=service_code, country=country_id)
        if result.startswith("ACCESS_NUMBER:"):
            parts = result.split(":")
            return SmsActivation(
                activation_id=parts[1],
                phone_number=parts[2],
                country=country or self.default_country,
            )

        if "NO_NUMBERS" in result:
            raise RuntimeError(f"SMS-Activate: 当前无可用号码 (service={service_code}, country={country_id})")
        if "NO_BALANCE" in result:
            raise RuntimeError("SMS-Activate: 余额不足")
        raise RuntimeError(f"SMS-Activate getNumber failed: {result}")

    def get_code(self, activation_id: str, *, timeout: int = 120) -> str:
        deadline = time.time() + timeout
        while time.time() < deadline:
            result = self._request("getStatus", id=activation_id)
            if result.startswith("STATUS_OK:"):
                return result.split(":")[1]
            if result == "STATUS_WAIT_CODE":
                time.sleep(3)
                continue
            if result == "STATUS_WAIT_RETRY":
                self._request("setStatus", id=activation_id, status="6")
                time.sleep(3)
                continue
            if result == "STATUS_CANCEL":
                return ""
            time.sleep(3)

        self.cancel(activation_id)
        return ""

    def cancel(self, activation_id: str) -> bool:
        result = self._request("setStatus", id=activation_id, status="8")
        return "ACCESS" in result

    def report_success(self, activation_id: str) -> bool:
        result = self._request("setStatus", id=activation_id, status="6")
        return "ACCESS" in result


# ---------------------------------------------------------------------------
# HeroSMS implementation (https://hero-sms.com/stubs/handler_api.php)
# ---------------------------------------------------------------------------

HERO_SMS_DEFAULT_SERVICE = "dr"
HERO_SMS_DEFAULT_COUNTRY = "187"
HERO_SMS_PHONE_LIFETIME = 20 * 60
_HERO_SMS_CACHE_LOCK = threading.Lock()
_HERO_SMS_VERIFY_LOCK = threading.RLock()
_HERO_SMS_CACHE: dict | None = None


def _project_data_dir() -> Path:
    root = Path(__file__).resolve().parent.parent
    data_dir = root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


def hero_sms_cache_file() -> Path:
    return _project_data_dir() / ".herosms_phone_cache.json"


def _hash_secret(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _safe_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_float(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_bool(value, default: bool) -> bool:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in {"0", "false", "no", "off", "否"}


def _normalize_hero_proxy(proxy: str | None) -> str | None:
    proxy = str(proxy or "").strip()
    if not proxy or proxy.startswith("singbox://"):
        return None
    return proxy


def _parse_hero_status_text(text: str) -> dict:
    text = str(text or "").strip()
    if text == "STATUS_WAIT_CODE":
        return {"status": "wait_code"}
    if text.startswith("STATUS_WAIT_RETRY"):
        return {"status": "wait_retry", "raw": text}
    if text == "STATUS_WAIT_RESEND":
        return {"status": "wait_resend"}
    if text.startswith("STATUS_OK:"):
        return {"status": "ok", "code": text.split(":", 1)[1]}
    if text == "STATUS_CANCEL":
        return {"status": "cancel"}
    return {"status": "unknown", "raw": text}


def _canonical_sms_event_fields(event_fields: dict | None) -> dict:
    event_fields = event_fields or {}
    canonical: dict[str, str] = {}
    channel = str(event_fields.get("channel") or "").strip()
    if channel:
        canonical["channel"] = channel
    sms_time = (
        event_fields.get("dateTime")
        or event_fields.get("date")
        or event_fields.get("smsDate")
        or event_fields.get("smsTime")
        or ""
    )
    if sms_time:
        canonical["time"] = str(sms_time)
    text = event_fields.get("text") or event_fields.get("smsText")
    if text:
        canonical["text"] = str(text)
    if channel == "call":
        for key in ("from", "url"):
            if event_fields.get(key):
                canonical[key] = str(event_fields[key])
    if not sms_time:
        for key in ("repeated", "activationStatus", "verificationType"):
            if event_fields.get(key) is not None:
                canonical[key] = str(event_fields[key])
    return canonical


def _has_real_sms_time(event_fields: dict | None) -> bool:
    raw_time = (
        (event_fields or {}).get("dateTime")
        or (event_fields or {}).get("date")
        or (event_fields or {}).get("smsDate")
        or (event_fields or {}).get("smsTime")
        or ""
    )
    raw_time = str(raw_time).strip()
    return bool(raw_time and raw_time not in {"0", "0000-00-00 00:00:00", "0000-00-00T00:00:00"})


def _sms_event_key(activation_id: str, code: str, event_fields: dict | None) -> str:
    identity = {"activation_id": str(activation_id), "code": str(code)}
    identity.update(_canonical_sms_event_fields(event_fields))
    raw = json.dumps(identity, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _make_sms_candidate(activation_id: str, source: str, code, event_fields: dict | None = None) -> dict | None:
    code = str(code or "").strip()
    if not code or code in {"null", "None"}:
        return None
    canonical = _canonical_sms_event_fields(event_fields)
    sms_key = _sms_event_key(activation_id, code, event_fields) if event_fields else ""
    return {
        "status": "ok",
        "code": code,
        "source": source,
        "sms_key": sms_key,
        "sms_time": canonical.get("time", ""),
        "sms_text": canonical.get("text", ""),
        "allow_same_code": _has_real_sms_time(event_fields),
    }


def _candidate_is_attempted(candidate: dict, used_codes: set, attempted_sms_keys: set) -> bool:
    sms_key = str(candidate.get("sms_key") or "")
    code = str(candidate.get("code") or "")
    if sms_key and sms_key in attempted_sms_keys:
        return True
    return bool(code in used_codes and not candidate.get("allow_same_code"))


class HeroSmsProvider(BaseSmsProvider):
    """HeroSMS provider with resend, SMS event dedupe, and short-lived phone reuse."""

    BASE_URL = "https://hero-sms.com/stubs/handler_api.php"
    auto_report_success_on_code = False

    def __init__(
        self,
        api_key: str,
        *,
        default_service: str = HERO_SMS_DEFAULT_SERVICE,
        default_country: str = HERO_SMS_DEFAULT_COUNTRY,
        max_price: float = -1,
        proxy: str | None = None,
        reuse_phone_to_max: bool = True,
        phone_success_max: int = 3,
    ):
        self.api_key = str(api_key or "").strip()
        self.default_service = str(default_service or HERO_SMS_DEFAULT_SERVICE).strip()
        self.default_country = str(default_country or HERO_SMS_DEFAULT_COUNTRY).strip()
        self.max_price = float(max_price or -1)
        self.proxy = _normalize_hero_proxy(proxy)
        self.proxies = {"http": self.proxy, "https": self.proxy} if self.proxy else None
        self.reuse_phone_to_max = bool(reuse_phone_to_max)
        self.phone_success_max = max(0, int(phone_success_max or 0))
        self.openai_resend_callback: Callable[[], None] | None = None
        self.last_code_result: dict | None = None
        self.current_activation: SmsActivation | None = None

    def _request(self, params: dict, *, needs_key: bool = True, timeout: int = 30) -> requests.Response:
        payload = dict(params)
        if needs_key:
            payload["api_key"] = self.api_key
        resp = requests.get(self.BASE_URL, params=payload, timeout=timeout, proxies=self.proxies)
        resp.raise_for_status()
        return resp

    def get_balance(self) -> float:
        text = self._request({"action": "getBalance"}).text.strip()
        if text.startswith("ACCESS_BALANCE:"):
            return float(text.split(":", 1)[1])
        raise RuntimeError(f"HeroSMS getBalance failed: {text}")

    def get_services(self, country: str | int | None = None, lang: str = "cn") -> list:
        params = {"action": "getServicesList", "lang": lang}
        if country not in (None, ""):
            params["country"] = country
        data = self._request(params, needs_key=False).json()
        if isinstance(data, dict) and data.get("status") == "success":
            return list(data.get("services") or [])
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            # 可能是 {"dr": {"name": "OpenAI", ...}, ...} 格式
            result = []
            for key, value in data.items():
                if key in ("status", "message", "error"):
                    continue
                if isinstance(value, dict):
                    if "code" not in value:
                        value["code"] = key
                    result.append(value)
                elif isinstance(value, str):
                    result.append({"code": key, "name": value})
            if result:
                return result
        raise RuntimeError("HeroSMS getServicesList returned unexpected response")

    def get_countries(self) -> list:
        data = self._request({"action": "getCountries"}, needs_key=False).json()
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            # 检查是否是错误响应 {"status":0,"message":"No access","data":[]}
            if data.get("status") == 0 or data.get("message") == "No access":
                raise RuntimeError(f"SMS API access denied: {data.get('message', 'unknown')}")
            # HeroSMS 可能返回 {"0": {"id": 0, "eng": "Russia"}, ...} 格式
            result = []
            for key, value in data.items():
                if key in ("status", "message", "data", "error"):
                    continue
                if isinstance(value, dict):
                    if "id" not in value:
                        value["id"] = key
                    result.append(value)
                elif isinstance(value, str):
                    result.append({"id": key, "eng": value, "name": value})
            if result:
                return result
        raise RuntimeError("SMS getCountries returned unexpected response")

    def get_prices(self, service: str | None = None, country: str | int | None = None) -> dict:
        params = {"action": "getPrices"}
        if service:
            params["service"] = service
        if country not in (None, ""):
            params["country"] = country
        data = self._request(params).json()
        if isinstance(data, dict):
            return data
        raise RuntimeError("HeroSMS getPrices returned unexpected response")

    def get_top_countries(self, service: str | None = None) -> list[dict]:
        """获取指定服务按价格排序的国家列表（含价格和库存）。

        优先使用 getTopCountriesByServiceRank API，降级到 getPrices 全量解析。
        返回格式: [{"country": "66", "name": "Thailand", "price": 0.12, "count": 150}, ...]
        """
        service_code = str(service or self.default_service or HERO_SMS_DEFAULT_SERVICE).strip()

        # 策略1: 使用 getTopCountriesByServiceRank（HeroSMS 专用排名接口）
        for action in ("getTopCountriesByServiceRank", "getTopCountriesByService"):
            try:
                data = self._request({"action": action, "service": service_code}).json()
                rows = self._parse_top_countries_response(data)
                if rows:
                    rows.sort(key=lambda r: (r.get("price") or 999, -(r.get("count") or 0)))
                    return rows
            except Exception:
                continue

        # 策略2: 从 getPrices 全量数据中解析
        try:
            prices = self.get_prices(service=service_code)
            rows = []
            for country_id, services in prices.items():
                if not isinstance(services, dict):
                    continue
                svc_data = services.get(service_code)
                if not isinstance(svc_data, dict):
                    continue
                price = svc_data.get("cost") or svc_data.get("price")
                count = svc_data.get("count") or svc_data.get("qty") or svc_data.get("available")
                try:
                    price = float(price) if price is not None else None
                except (TypeError, ValueError):
                    price = None
                try:
                    count = int(count) if count is not None else 0
                except (TypeError, ValueError):
                    count = 0
                if price is not None and count > 0:
                    rows.append({"country": str(country_id), "price": price, "count": count})
            rows.sort(key=lambda r: (r.get("price") or 999, -(r.get("count") or 0)))
            return rows
        except Exception:
            return []

    def _parse_top_countries_response(self, data) -> list[dict]:
        """解析 getTopCountriesByServiceRank 响应。"""
        rows = []
        items = data
        # 可能嵌套在 data/result 键下
        if isinstance(data, dict):
            items = data.get("data") or data.get("result") or data.get("response") or data
        if isinstance(items, dict):
            # {country_id: {price, count, ...}} 格式
            for key, value in items.items():
                if not isinstance(value, dict):
                    continue
                try:
                    country_id = str(int(key))
                except (TypeError, ValueError):
                    continue
                price = value.get("price") or value.get("cost") or value.get("retail_price")
                count = value.get("count") or value.get("qty") or value.get("available") or value.get("stock")
                name = value.get("name") or value.get("countryName") or value.get("country_name") or ""
                try:
                    price = float(price) if price is not None else None
                except (TypeError, ValueError):
                    price = None
                try:
                    count = int(count) if count is not None else 0
                except (TypeError, ValueError):
                    count = 0
                if price is not None:
                    rows.append({"country": country_id, "name": str(name), "price": price, "count": count})
        elif isinstance(items, list):
            for item in items:
                if not isinstance(item, dict):
                    continue
                country_id = item.get("country") or item.get("countryId") or item.get("country_id") or item.get("id")
                if country_id is None:
                    continue
                price = item.get("price") or item.get("cost") or item.get("retail_price") or item.get("retailPrice")
                count = item.get("count") or item.get("qty") or item.get("available") or item.get("stock") or item.get("total")
                name = item.get("name") or item.get("countryName") or item.get("country_name") or item.get("title") or ""
                try:
                    price = float(price) if price is not None else None
                except (TypeError, ValueError):
                    price = None
                try:
                    count = int(count) if count is not None else 0
                except (TypeError, ValueError):
                    count = 0
                if price is not None:
                    rows.append({"country": str(country_id), "name": str(name), "price": price, "count": count})
        return rows

    # OpenAI service="dr" 推荐国家偏好顺序，参考 https://sms.fur.li 的 OpenAI 推荐白名单
    # （上游也来自开源项目 FoundZiGu/SMSBazaar）。
    #
    # 注意：sms.fur.li 把推荐分成 path=0（先手机号注册 / 无 WhatsApp）和 path=1
    # （后手机号绑定 = OAuth add_phone）。我们走的是 OAuth add_phone，所以应优先
    # path=1 那批"OpenAI add_phone 真的会下 SMS"的国家；CO/BR/PL 是 path=0
    # （register-first，OpenAI add_phone 阶段对它们通常只给 WhatsApp），SMSBower
    # 的号码收不到 WhatsApp，所以放在后面兜底。
    SMS_PREFERRED_COUNTRIES_FOR_OPENAI = (
        # —— path=1（OAuth add_phone 推荐，SMS 实际下发）——
        "53",   # Saudi Arabia — sms.fur.li 推荐第一名（~$0.023）
        "52",   # Thailand — ~$0.054
        "38",   # Ghana — ~$0.054
        "19",   # Nigeria — ~$0.054
        "76",   # Angola — ~$0.054
        "41",   # Cameroon — ~$0.055
        "36",   # Canada — ~$0.032
        "60",   # Bangladesh
        "187",  # USA
        # —— path=0（register-first，add_phone 时通常只发 WhatsApp）——
        # 用户的默认国家是 CO，放在 path=0 的最前面作为兜底
        "33",   # Colombia
        "73",   # Brazil
        "15",   # Poland
    )

    # sms.fur.li 推荐数据缓存（进程级，1 小时刷新一次）
    _SMSFURLI_CACHE_LOCK = threading.Lock()
    _SMSFURLI_CACHE: dict | None = None
    _SMSFURLI_CACHE_TS: float = 0.0
    _SMSFURLI_CACHE_TTL = 3600.0
    # SMSBazaar/sms.fur.li 推荐数据里 ISO2 国家 → SMSBower 国家 ID（与 SMSBower getCountries
    # 实测对齐）。运行时如能成功访问 sms.fur.li 会自动补齐，否则用这个静态表兜底。
    SMSBOWER_ISO2_TO_ID = {
        "SA": "53", "BR": "73", "CA": "36", "CO": "33", "PL": "15", "TH": "52",
        "GH": "38", "NG": "19", "AO": "76", "CM": "41", "US": "187", "BD": "60",
    }

    @classmethod
    def fetch_smsfurli_recommended_country_ids(
        cls,
        *,
        proxy: str | None = None,
        prefer_paths: tuple[int, ...] | None = None,
        max_price_usd: float = 0.0,
        timeout: int = 8,
    ) -> list[str]:
        """从 sms.fur.li/api/compare 拉取当前推荐国家，映射为 SMSBower 国家 ID 列表。

        - ``prefer_paths``: 限定 recommendationPath；用户当前是 OAuth bind-after 流程，
          对应 path=1。传 ``None`` 表示不过滤 path（含 path=0 的注册前国家）。
        - ``max_price_usd``: 仅返回 minPriceUsd ≤ 这个值的国家（用户的 SMSBower 价格上限）。
        - 失败或超时返回 ``[]``，调用方应回退到 ``SMS_PREFERRED_COUNTRIES_FOR_OPENAI``。
        """
        now = time.time()
        with cls._SMSFURLI_CACHE_LOCK:
            cache = cls._SMSFURLI_CACHE
            if cache and (now - cls._SMSFURLI_CACHE_TS) < cls._SMSFURLI_CACHE_TTL:
                rec = cache
            else:
                rec = None

        if rec is None:
            try:
                proxies = {"http": proxy, "https": proxy} if proxy else None
                resp = requests.get(
                    "https://sms.fur.li/api/compare",
                    params={"provider": "smsbower", "recommended": "true"},
                    timeout=timeout,
                    proxies=proxies,
                )
                resp.raise_for_status()
                rec = resp.json()
            except Exception as exc:
                logger.warning("sms.fur.li 拉取推荐失败: %s", exc)
                rec = None
            with cls._SMSFURLI_CACHE_LOCK:
                if rec is not None:
                    cls._SMSFURLI_CACHE = rec
                    cls._SMSFURLI_CACHE_TS = time.time()

        if not isinstance(rec, dict):
            return []
        rows = rec.get("rows") or []
        candidates: list[tuple[float, str, str]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            path = row.get("recommendationPath")
            if path is None:
                continue
            if prefer_paths is not None and int(path) not in prefer_paths:
                continue
            iso = str(row.get("countryIso2") or "").upper()
            cid = cls.SMSBOWER_ISO2_TO_ID.get(iso)
            if not cid:
                continue
            try:
                min_price = float(row.get("minPriceUsd") or 0)
            except (TypeError, ValueError):
                continue
            if max_price_usd > 0 and min_price > max_price_usd:
                continue
            candidates.append((min_price, cid, iso))
        candidates.sort(key=lambda t: t[0])
        return [cid for _, cid, _ in candidates]

    def iter_candidate_countries(
        self,
        service: str | None = None,
        *,
        max_price: float = 0,
        min_stock: int = 1,
        prefer: tuple[str, ...] | None = None,
    ) -> list[dict]:
        """返回候选国家列表，按"偏好顺序"排序（sms.fur.li 推荐 + 用户默认）。

        - 实测 SMSBower 的 ``getPrices?service=dr``（聚合）对 CO 这类多档位国家会返回
          最高档价 ``$0.502`` 而不是最低 ``$0.034``，所以**不**用它来过滤。
        - sms.fur.li 推荐里的国家总是保留（哪怕 SMSBower 聚合价显示超 cap），实际取号
          走 ``getPricesV3`` 按档位逐级试，过 cap 自动跳过。
        - 非推荐国家用 SMSBower 聚合数据兜底，且应用价格上限做粗筛。
        """
        try:
            rows = self.get_top_countries(service=service)
        except Exception as exc:
            logger.warning("iter_candidate_countries 查询失败: %s", exc)
            rows = []

        recommended_ids: set[str] = set()
        # sms.fur.li 把 OpenAI 推荐分成 path=0（先手机号注册）和 path=1（OAuth 绑定）。
        # SMSBower 的号码收不到 WhatsApp，OpenAI 在 path=0 国家 add_phone 阶段经常只给
        # WhatsApp（如 CO/BR/PL）。所以 OpenAI add_phone(service="dr") 流程下，path=0
        # 的国家**不应优先**——哪怕用户把默认国家设成了 CO（path=0），也要把它降级到
        # path=1 之后，避免每次注册都先在 CO 上撞 WhatsApp。
        is_openai_addphone = str(service or "").lower() in ("dr", "go")
        # 已知 path=0（register-first）国家集合，用于 OpenAI add_phone 流程下的降级。
        REGISTER_FIRST_IDS = {"33", "73", "15"}  # CO, BR, PL
        if prefer is not None:
            prefer_seq = tuple(prefer)
            recommended_ids = set(prefer_seq)
        else:
            # 优先取 sms.fur.li 实时推荐（bind-after 是 path=1，path=0 作 fallback）
            live = self.fetch_smsfurli_recommended_country_ids(
                proxy=self.proxy,
                prefer_paths=(0, 1),
                max_price_usd=max_price,
            )
            # OpenAI add_phone 流程下，按 sms.fur.li 价格升序为 SA, CA, CO, BR, PL,
            # TH, GH, AO, NG, CM, US。CO/BR/PL 在 add_phone 阶段会被 OpenAI 改成
            # WhatsApp，所以在这里把 path=0 ID 抽出来放到列表末尾。
            if is_openai_addphone and live:
                path1_live = [c for c in live if c not in REGISTER_FIRST_IDS]
                path0_live = [c for c in live if c in REGISTER_FIRST_IDS]
                live = path1_live + path0_live
            user_choice = str(self.default_country or "").strip()
            ordered = []
            seen = set()
            # OpenAI add_phone 流程下，如果用户默认是 CO/BR/PL（path=0），不要把它
            # 顶到第一位——按推荐顺序走，让 SA/CA 先试。其它服务保留原行为。
            demote_default = (
                is_openai_addphone
                and user_choice in REGISTER_FIRST_IDS
            )
            if user_choice and not demote_default:
                ordered.append(user_choice)
                seen.add(user_choice)
            for cid in live:
                if cid not in seen:
                    ordered.append(cid)
                    seen.add(cid)
            # 默认国家被降级时，放到 live 推荐之后、静态白名单之前，作为兜底重试。
            if demote_default and user_choice and user_choice not in seen:
                ordered.append(user_choice)
                seen.add(user_choice)
            for cid in self.SMS_PREFERRED_COUNTRIES_FOR_OPENAI:
                if cid not in seen:
                    ordered.append(cid)
                    seen.add(cid)
            prefer_seq = tuple(ordered)
            recommended_ids = seen
        prefer_index = {cid: idx for idx, cid in enumerate(prefer_seq)}

        # 把推荐国家直接灌入候选池（不依赖 SMSBower 聚合数据）
        rows_by_country = {str(r.get("country") or ""): r for r in rows if r.get("country") is not None}
        for cid in prefer_seq:
            if cid in rows_by_country:
                continue
            # SMSBower top_countries 里没这个国家——可能国家代码没在响应里。
            # 用一个默认占位条目，让它仍出现在候选列表里（实际取号会用 getPricesV3）。
            rows_by_country[cid] = {"country": cid, "price": 0.0, "count": 1}

        candidates = []
        for country_id, row in rows_by_country.items():
            country_id = str(country_id).strip()
            if not country_id:
                continue
            price = row.get("price") or 0
            count = row.get("count") or 0
            try:
                price = float(price)
            except (TypeError, ValueError):
                continue
            try:
                count = int(count)
            except (TypeError, ValueError):
                count = 0
            is_recommended = country_id in recommended_ids
            # 推荐国家无视 max_price/min_stock 粗筛——它们的真实档位价由 getPricesV3 决定
            if not is_recommended:
                if count < min_stock:
                    continue
                if max_price > 0 and price > max_price:
                    continue
            candidates.append({
                "country": country_id,
                "price": price,
                "count": count,
                # 偏好序号小的排前面；不在列表里的统一给一个大值，仍然保留
                "prefer_rank": prefer_index.get(country_id, len(prefer_seq) + 1),
            })

        candidates.sort(key=lambda r: (r["prefer_rank"], r["price"], -r["count"]))
        return candidates

    def get_best_country(self, service: str | None = None, *, min_stock: int = 20, max_price: float = 0) -> str | None:
        """自动选择最优国家：偏好已知支持 SMS 的国家 + 价格 + 库存。

        若 ``max_price``/``min_stock`` 太严格找不到候选，自动放宽 ``min_stock`` 到 1
        再退一步；仍找不到才返回 ``None``。
        """
        candidates = self.iter_candidate_countries(
            service=service, max_price=max_price, min_stock=min_stock
        )
        if candidates:
            return candidates[0]["country"]
        # 放宽 min_stock
        candidates = self.iter_candidate_countries(
            service=service, max_price=max_price, min_stock=1
        )
        if candidates:
            return candidates[0]["country"]
        return None

    def _cache_identity(self, service: str, country: str) -> dict:
        return {
            "api_key_hash": _hash_secret(self.api_key),
            "service": str(service),
            "country": str(country),
        }

    def _load_cache(self, service: str, country: str) -> dict | None:
        global _HERO_SMS_CACHE
        if _HERO_SMS_CACHE is not None:
            cache = _HERO_SMS_CACHE
        else:
            path = hero_sms_cache_file()
            if not path.exists():
                return None
            try:
                cache = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                return None
        identity = self._cache_identity(service, country)
        if any(str(cache.get(key) or "") != str(value) for key, value in identity.items()):
            return None
        elapsed = time.time() - float(cache.get("acquired_at") or 0)
        if elapsed >= HERO_SMS_PHONE_LIFETIME or cache.get("reuse_stopped"):
            self._clear_cache()
            return None
        if self.phone_success_max > 0 and int(cache.get("use_count") or 0) >= self.phone_success_max:
            cache["reuse_stopped"] = True
            cache["stop_reason"] = f"success max reached ({self.phone_success_max})"
            self._save_cache(cache)
            return None
        cache["used_codes"] = set(cache.get("used_codes") or [])
        cache["attempted_sms_keys"] = set(cache.get("attempted_sms_keys") or [])
        _HERO_SMS_CACHE = cache
        return cache

    def _save_cache(self, cache: dict | None) -> None:
        global _HERO_SMS_CACHE
        _HERO_SMS_CACHE = cache
        path = hero_sms_cache_file()
        if cache is None:
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass
            return
        serializable = dict(cache)
        serializable["used_codes"] = sorted(serializable.get("used_codes") or [])
        serializable["attempted_sms_keys"] = sorted(serializable.get("attempted_sms_keys") or [])
        serializable.pop("client", None)
        path.write_text(json.dumps(serializable, ensure_ascii=False), encoding="utf-8")

    def _clear_cache(self) -> None:
        self._save_cache(None)

    def _stop_reuse(self, reason: str) -> None:
        with _HERO_SMS_CACHE_LOCK:
            cache = _HERO_SMS_CACHE
            if not cache:
                return
            cache["reuse_stopped"] = True
            cache["stop_reason"] = reason
            self._save_cache(cache)

    def _request_number_raw(self, service: str, country: str) -> dict:
        common = {"service": service, "country": country}

        # SMSBower 的 maxPrice 不是买家成交价，而是「卖家最高 ASK 上限」。
        # 实测：买家配置 max_price=$0.06 时，CO 等热门国家会返回 NO_NUMBERS——因为
        # 卖家挂牌价已经普遍 >$0.06（哪怕实际成交价只有 $0.034）。所以这里逐级抬高
        # maxPrice 给 SMSBower 找号码的空间，但拿到号后会用 activationCost 跟用户的
        # max_price 对账，超了就抛 "成交价超过上限" 让上层取消并换国家。
        user_cap = float(self.max_price) if self.max_price > 0 else 0.0

        # 候选 maxPrice 梯队（USD）：先尝试用户上限，再逐级放宽。
        # 注意上限只是「能找到卖家」的搜索范围，实际成交价由 activationCost 决定。
        ladder: list[float] = []
        if user_cap > 0:
            ladder.append(round(user_cap, 4))
            for mult in (1.5, 2.5, 4.0):
                v = round(user_cap * mult, 4)
                if v not in ladder:
                    ladder.append(v)
        else:
            ladder = [0.2, 0.5, 1.0]

        v2_error = ""

        def _check_cost(data: dict) -> dict:
            """检查 activationCost ≤ user_cap，超过就触发取消重试。"""
            cost_raw = data.get("activationCost") if isinstance(data, dict) else None
            try:
                cost = float(cost_raw) if cost_raw is not None else None
            except (TypeError, ValueError):
                cost = None
            if user_cap > 0 and cost is not None and cost > user_cap + 1e-6:
                aid = data.get("activationId")
                if aid is not None:
                    try:
                        self._request(
                            {"action": "setStatus", "id": aid, "status": 8},
                            needs_key=True,
                        )
                    except Exception:
                        pass
                raise RuntimeError(
                    f"COST_OVER_CAP: 卖家报价超上限 ${cost:.4f} > 用户上限 ${user_cap:.4f}"
                    f"（country={country}），已立即取消号码 {aid}（未实际付费）"
                )
            return data

        for max_price_try in ladder:
            common["maxPrice"] = max_price_try
            try:
                resp = self._request({"action": "getNumberV2", **common})
                try:
                    data = resp.json()
                except ValueError:
                    data = None
                if isinstance(data, dict) and data.get("activationId"):
                    return _check_cost(data)
                v2_error = resp.text.strip()[:200]
            except Exception as exc:
                v2_error = str(exc)
            if "NO_NUMBERS" not in v2_error and "NO_BALANCE" not in v2_error:
                # 非 NO_NUMBERS 错误（比如余额不足、key 失效）直接退出梯队
                break

        try:
            text = self._request({"action": "getNumber", **common}).text.strip()
            if text.startswith("ACCESS_NUMBER:"):
                parts = text.split(":", 2)
                if len(parts) == 3:
                    return {
                        "activationId": parts[1],
                        "phoneNumber": parts[2],
                        "countryPhoneCode": "",
                        "activationCost": None,
                    }
            raise RuntimeError(text[:200])
        except Exception as exc:
            raise RuntimeError(f"HeroSMS 获取号码失败: V2={v2_error}; V1={exc}") from exc

    @staticmethod
    def _format_phone(number_info: dict) -> str:
        raw = str(number_info.get("phoneNumber") or "").strip()
        country_phone_code = str(number_info.get("countryPhoneCode") or "").strip()
        if raw.startswith("+"):
            return raw
        if country_phone_code and raw.startswith(country_phone_code):
            return f"+{raw}"
        if country_phone_code:
            return f"+{country_phone_code}{raw}"
        return f"+{raw}"

    def get_number(self, *, service: str, country: str = "") -> SmsActivation:
        service_code = str(self.default_service or service or HERO_SMS_DEFAULT_SERVICE).strip()
        country_id = str(country or self.default_country or HERO_SMS_DEFAULT_COUNTRY).strip()
        with _HERO_SMS_VERIFY_LOCK:
            with _HERO_SMS_CACHE_LOCK:
                cache = self._load_cache(service_code, country_id) if self.reuse_phone_to_max else None
                if cache:
                    activation = SmsActivation(
                        activation_id=str(cache["activation_id"]),
                        phone_number=str(cache["phone_number"]),
                        country=country_id,
                        metadata={"reused": True, "use_count": int(cache.get("use_count") or 0)},
                    )
                    self.current_activation = activation
                    return activation

                number_info = self._request_number_raw(service_code, country_id)
                activation_id = str(number_info.get("activationId") or "")
                phone = self._format_phone(number_info)
                if not activation_id or not phone.strip("+"):
                    raise RuntimeError("HeroSMS 返回的号码信息不完整")
                cache = {
                    **self._cache_identity(service_code, country_id),
                    "activation_id": activation_id,
                    "phone_number": phone,
                    "acquired_at": time.time(),
                    "use_count": 0,
                    "used_codes": set(),
                    "attempted_sms_keys": set(),
                    "reuse_stopped": False,
                    "stop_reason": "",
                }
                self._save_cache(cache)
                activation = SmsActivation(
                    activation_id=activation_id,
                    phone_number=phone,
                    country=country_id,
                    metadata={"reused": False, "number_info": number_info},
                )
                self.current_activation = activation
                return activation

    def get_status(self, activation_id: str) -> dict:
        return _parse_hero_status_text(self._request({"action": "getStatus", "id": activation_id}).text)

    def get_status_v2(self, activation_id: str) -> dict:
        resp = self._request({"action": "getStatusV2", "id": activation_id})
        text = resp.text.strip()
        try:
            data = resp.json()
        except ValueError:
            return _parse_hero_status_text(text)
        if isinstance(data, str):
            return _parse_hero_status_text(data)
        if not isinstance(data, dict):
            return {"status": "unknown", "raw": data}
        raw_status = data.get("status")
        if isinstance(raw_status, str):
            parsed = _parse_hero_status_text(raw_status)
            if parsed.get("status") != "unknown":
                return parsed
        for channel in ("sms", "call"):
            item = data.get(channel)
            if isinstance(item, dict):
                candidate = _make_sms_candidate(
                    activation_id,
                    f"getStatusV2.{channel}",
                    item.get("code"),
                    {
                        "channel": channel,
                        "dateTime": item.get("dateTime"),
                        "text": item.get("text"),
                        "from": item.get("from"),
                        "url": item.get("url"),
                        "verificationType": data.get("verificationType"),
                    },
                )
                if candidate:
                    return candidate
        return {"status": "wait_code", "raw": data}

    def get_active_activations(self, start: int = 0, limit: int = 20) -> list:
        data = self._request({"action": "getActiveActivations", "start": start, "limit": limit}).json()
        if isinstance(data, dict) and "data" in data:
            return list(data.get("data") or [])
        return []

    def set_status(self, activation_id: str, status: int) -> str:
        return self._request({"action": "setStatus", "id": activation_id, "status": status}).text.strip()

    def cancel_activation(self, activation_id: str) -> bool:
        try:
            resp = self._request({"action": "cancelActivation", "id": activation_id})
            if resp.status_code == 204 or "ACCESS_CANCEL" in resp.text:
                return True
        except Exception:
            pass
        try:
            return "ACCESS_CANCEL" in self.set_status(activation_id, 8)
        except Exception:
            return False

    def finish_activation(self, activation_id: str) -> bool:
        try:
            resp = self._request({"action": "finishActivation", "id": activation_id})
            text = resp.text.strip()
            return resp.status_code in (200, 204) or "ACCESS" in text
        except Exception:
            try:
                return "ACCESS" in self.set_status(activation_id, 6)
            except Exception:
                return False

    def request_resend_sms(self, activation_id: str) -> bool:
        try:
            self.set_status(activation_id, 3)
            return True
        except Exception:
            return False

    def wait_for_code(self, activation_id: str, *, timeout: int = 180, poll_interval: int = 3) -> dict | None:
        deadline = time.time() + timeout
        start = time.time()
        last_hero_resend = start
        openai_resent = False
        warned_v2 = False
        while time.time() < deadline:
            with _HERO_SMS_CACHE_LOCK:
                cache = _HERO_SMS_CACHE or {}
                used_codes = set(cache.get("used_codes") or [])
                attempted_sms_keys = set(cache.get("attempted_sms_keys") or [])

            for source in ("v2", "v1", "active"):
                try:
                    candidate = None
                    if source == "v2":
                        result = self.get_status_v2(activation_id)
                        if result.get("status") == "cancel":
                            return None
                        if result.get("status") == "ok":
                            candidate = result
                    elif source == "v1":
                        result = self.get_status(activation_id)
                        if result.get("status") == "cancel":
                            return None
                        if result.get("status") == "ok":
                            candidate = _make_sms_candidate(activation_id, "getStatus", result.get("code"))
                    else:
                        for item in self.get_active_activations():
                            if str(item.get("activationId")) == str(activation_id):
                                candidate = _make_sms_candidate(
                                    activation_id,
                                    "getActiveActivations",
                                    item.get("smsCode"),
                                    {
                                        "channel": "sms",
                                        "smsText": item.get("smsText"),
                                        "activationStatus": item.get("activationStatus"),
                                        "repeated": item.get("repeated"),
                                        "dateTime": item.get("dateTime"),
                                        "date": item.get("date") or item.get("smsDate") or item.get("smsTime"),
                                    },
                                )
                                break
                    if candidate and not _candidate_is_attempted(candidate, used_codes, attempted_sms_keys):
                        return candidate
                except Exception as exc:
                    if source == "v2" and not warned_v2:
                        logger.warning("HeroSMS getStatusV2 failed: %s", exc)
                        warned_v2 = True
                    else:
                        logger.debug("HeroSMS status check failed via %s: %s", source, exc)

            elapsed = time.time() - start
            if not openai_resent and elapsed >= 90 and self.openai_resend_callback:
                try:
                    self.openai_resend_callback()
                except Exception as exc:
                    logger.warning("OpenAI phone resend callback failed: %s", exc)
                self.request_resend_sms(activation_id)
                last_hero_resend = time.time()
                openai_resent = True
            elif time.time() - last_hero_resend >= 30:
                self.request_resend_sms(activation_id)
                last_hero_resend = time.time()

            time.sleep(poll_interval)
        return None

    def get_code(self, activation_id: str, *, timeout: int = 120) -> str:
        wait_timeout = timeout
        with _HERO_SMS_CACHE_LOCK:
            cache = _HERO_SMS_CACHE or {}
            if cache and str(cache.get("activation_id")) == str(activation_id):
                remaining = int(HERO_SMS_PHONE_LIFETIME - (time.time() - float(cache.get("acquired_at") or 0)))
                wait_timeout = max(timeout, remaining, 60)
        candidate = self.wait_for_code(activation_id, timeout=wait_timeout)
        self.last_code_result = candidate
        return str((candidate or {}).get("code") or "")

    def cancel(self, activation_id: str) -> bool:
        try:
            return self.cancel_activation(activation_id)
        finally:
            with _HERO_SMS_CACHE_LOCK:
                cache = _HERO_SMS_CACHE
                if cache and str(cache.get("activation_id")) == str(activation_id):
                    self._clear_cache()

    def report_success(self, activation_id: str) -> bool:
        should_finish = False
        should_clear_cache = False
        handled_cached_activation = False
        with _HERO_SMS_CACHE_LOCK:
            cache = _HERO_SMS_CACHE
            if cache and str(cache.get("activation_id")) == str(activation_id):
                handled_cached_activation = True
                cache["use_count"] = int(cache.get("use_count") or 0) + 1
                self._record_last_attempt(cache, failed=False)
                remaining = HERO_SMS_PHONE_LIFETIME - (time.time() - float(cache.get("acquired_at") or 0))
                if not self.reuse_phone_to_max:
                    cache["reuse_stopped"] = True
                    cache["stop_reason"] = "reuse disabled"
                    should_finish = True
                    should_clear_cache = True
                elif self.phone_success_max > 0 and int(cache["use_count"]) >= self.phone_success_max:
                    cache["reuse_stopped"] = True
                    cache["stop_reason"] = f"success max reached ({self.phone_success_max})"
                    should_finish = True
                elif remaining <= 30:
                    cache["reuse_stopped"] = True
                    cache["stop_reason"] = "phone lifetime nearly expired"
                    should_finish = True
                    should_clear_cache = True
                self._save_cache(cache)
                if should_clear_cache:
                    self._clear_cache()
        if handled_cached_activation:
            if should_finish:
                self.finish_activation(activation_id)
            return True
        return self.finish_activation(activation_id)

    def _record_last_attempt(self, cache: dict, *, failed: bool) -> None:
        candidate = self.last_code_result or {}
        code = str(candidate.get("code") or "")
        sms_key = str(candidate.get("sms_key") or "")
        used_codes = set(cache.get("used_codes") or [])
        attempted_sms_keys = set(cache.get("attempted_sms_keys") or [])
        if code:
            used_codes.add(code)
        if sms_key:
            attempted_sms_keys.add(sms_key)
        cache["used_codes"] = used_codes
        cache["attempted_sms_keys"] = attempted_sms_keys
        if failed:
            cache["last_failed_reason"] = "invalid otp"

    def mark_code_failed(self, activation_id: str, reason: str = "") -> None:
        with _HERO_SMS_CACHE_LOCK:
            cache = _HERO_SMS_CACHE
            if cache and str(cache.get("activation_id")) == str(activation_id):
                self._record_last_attempt(cache, failed=True)
                self._save_cache(cache)
        if self.openai_resend_callback:
            try:
                self.openai_resend_callback()
            except Exception:
                pass
        self.request_resend_sms(activation_id)

    def mark_send_succeeded(self, activation_id: str) -> None:
        try:
            self.set_status(activation_id, 1)
        except Exception:
            pass

    def mark_send_failed(self, activation_id: str, reason: str = "") -> None:
        reason_text = str(reason or "").lower()
        if any(keyword in reason_text for keyword in ("limit", "already", "too many", "exceeded", "maximum", "上限", "已达")):
            self._stop_reuse("phone limit reached")
        else:
            self._stop_reuse(reason or "phone rejected")

    def set_resend_callback(self, callback: Callable[[], None] | None) -> None:
        self.openai_resend_callback = callback

    def get_reuse_info(self) -> dict:
        with _HERO_SMS_CACHE_LOCK:
            cache = _HERO_SMS_CACHE or self._load_cache(self.default_service, self.default_country) or {}
            if not cache:
                return {"alive": False}
            remaining = max(0, int(HERO_SMS_PHONE_LIFETIME - (time.time() - float(cache.get("acquired_at") or 0))))
            return {
                "alive": remaining > 0 and not bool(cache.get("reuse_stopped")),
                "phone_number": cache.get("phone_number", ""),
                "use_count": int(cache.get("use_count") or 0),
                "remaining_seconds": remaining,
                "reuse_stopped": bool(cache.get("reuse_stopped")),
                "stop_reason": cache.get("stop_reason", ""),
            }


class SmsBowerProvider(HeroSmsProvider):
    """SMSBower provider — API 兼容 HeroSMS，仅 base URL 不同。

    覆盖 ``_request_number_raw`` 走 SMSBower 专属的 ``getPricesV3``：
    后者返回 ``{country: {service: {operator_id: {price, count, provider_id}}}}``
    多档位结构，能按"实际有库存的最便宜档位"逐级试 ``maxPrice``，比 HeroSMS 的单点
    ``getPrices`` 精准很多，也避免「直接送上限」错失底层便宜号。
    """

    BASE_URL = "https://smsbower.page/stubs/handler_api.php"

    # 用户实测：哥伦比亚(33) 的 $0.054 档位 (provider_id=3160, 14000+ 库存) 是
    # OpenAI add_phone 真的会下 **SMS** 而不是 WhatsApp 的卖家。其它 CO 档位（$0.034
    # 黄金档等）拿来的号码 OpenAI 会强推 WhatsApp 验证，SMSBower 收不到。所以这里
    # 把"国家 -> 必须锁定的 operator 卖家 ID"显式打表，在 getNumberV2 上加 operator 参数。
    COUNTRY_OPERATOR_PIN: dict[str, str] = {
        "33": "3160",
    }

    def _request(self, params: dict, *, needs_key: bool = True, timeout: int = 30) -> requests.Response:
        # SMSBower 所有接口都需要 api_key（包括 getServicesList、getCountries）
        payload = dict(params)
        if needs_key or self.api_key:
            payload["api_key"] = self.api_key
        resp = requests.get(self.BASE_URL, params=payload, timeout=timeout, proxies=self.proxies)
        resp.raise_for_status()
        return resp

    def get_prices_v3(self, *, service: str | None = None, country: str | int | None = None) -> dict:
        """SMSBower 专属，返回带 operator 档位的多档价格结构。

        响应形如：
        ``{"33": {"dr": {"3160": {"count": 14530, "price": 0.054, "provider_id": 3160}, ...}}}``
        """
        params = {"action": "getPricesV3"}
        if service:
            params["service"] = service
        if country not in (None, ""):
            params["country"] = country
        data = self._request(params).json()
        if isinstance(data, dict):
            return data
        raise RuntimeError("SMSBower getPricesV3 返回结构异常")

    def collect_tier_list(self, *, service: str, country: str) -> list[dict]:
        """从 getPricesV3 抽出 (country, service) 下的所有 (price, count, operator) 档位。

        返回按价格升序的列表：
        ``[{"price": 0.027, "count": 89, "operator": "2236"}, ...]``
        档位 ``count <= 0`` 也保留，调用方再过滤——上层有时需要全档目录做诊断。
        """
        try:
            data = self.get_prices_v3(service=service, country=country)
        except Exception as exc:
            logger.warning("getPricesV3 查询失败 country=%s: %s", country, exc)
            return []
        country_block = data.get(str(country)) or data.get(country) or {}
        service_block = country_block.get(service) or {}
        tiers: list[dict] = []
        if isinstance(service_block, dict):
            for op_key, entry in service_block.items():
                if not isinstance(entry, dict):
                    continue
                try:
                    price = float(entry.get("price"))
                except (TypeError, ValueError):
                    continue
                try:
                    count = int(entry.get("count") or 0)
                except (TypeError, ValueError):
                    count = 0
                operator = str(entry.get("provider_id") or op_key or "").strip()
                tiers.append({"price": price, "count": count, "operator": operator})
        tiers.sort(key=lambda t: (t["price"], -t["count"]))
        return tiers

    def _request_number_raw(self, service: str, country: str) -> dict:
        """SMSBower 取号：按 getPricesV3 档位从最便宜到 user_cap 逐级 maxPrice。

        - 严格不超过 ``self.max_price``（用户上限）
        - 不预设 operator——SMSBower 内部会在 ``maxPrice`` 范围内挑卖家
        - 如果整段 user_cap 内都 NO_NUMBERS：抛出明确错误让上层换国家
        - 收到号码后用 ``activationCost`` 复核：超 cap（理论上不会，但 SMSBower 行为
          有时漂移）就立即 cancel，等同 ``COST_OVER_CAP``
        """
        user_cap = float(self.max_price) if self.max_price > 0 else 0.0

        # 抓取当前国家所有档位
        tiers = self.collect_tier_list(service=service, country=country)

        # 用户钉死的 operator（如 CO=33 锁 3160=$0.054 档）——这种档位 OpenAI 真的会下
        # SMS。锁定后只走它，不再下探更便宜的档位（其它档位拿到的号 OpenAI 会强推 WhatsApp）。
        pinned_operator = self.COUNTRY_OPERATOR_PIN.get(str(country).strip())
        pinned_tiers: list[dict] = []
        if pinned_operator:
            pinned_tiers = [t for t in tiers if str(t.get("operator") or "") == pinned_operator]

        if pinned_operator and pinned_tiers:
            # 钉死模式：只允许这一档，不要 fallback 到其它便宜档（OpenAI 会强推 WhatsApp）。
            in_stock = [t for t in pinned_tiers if t["count"] > 0]
            if user_cap > 0:
                usable = [t for t in in_stock if t["price"] <= user_cap + 1e-9]
            else:
                usable = list(in_stock)
            # 即使当下 count=0（NO_NUMBERS），也保留这一档让 getNumberV2 重试——
            # 用户要求"留出两分钟等待时间"，所以即便瞬时无库存也要继续探测。
            if not usable and pinned_tiers:
                usable = list(pinned_tiers)
        else:
            # 没锁 operator → 维持原有"按价格梯队 ASC"
            in_stock = [t for t in tiers if t["count"] > 0]
            if user_cap > 0:
                usable = [t for t in in_stock if t["price"] <= user_cap + 1e-9]
            else:
                usable = list(in_stock)

        # 构建 maxPrice 梯队（按价格 ASC，去重）。
        # 例如档位 [$0.027, $0.034, $0.054]，user_cap=$0.06 → maxPrice 试 $0.027 → $0.034 → $0.054
        ladder: list[float] = []
        seen: set[float] = set()
        for t in usable:
            p = round(t["price"], 4)
            if p not in seen:
                seen.add(p)
                ladder.append(p)
        # 兜底：如果没拿到任何档位（getPricesV3 失败 / 国家无 dr 服务等），用 user_cap 自己作为单一档位试一次
        if not ladder and user_cap > 0:
            ladder = [round(user_cap, 4)]
        # 没有 user_cap 又没有档位：保底 $1（与原实现一致）
        if not ladder:
            ladder = [1.0]

        v2_error = ""
        tried_pretty = ", ".join(f"${p:.4f}" for p in ladder)
        # info-level：钉死模式下用户必须看到 operator=3160 真的被传过去；否则就是 bug。
        if pinned_operator:
            logger.info(
                "SMSBower 取号 country=%s service=%s **operator=%s** (pinned, $%.4f 档)",
                country, service, pinned_operator, ladder[0] if ladder else 0.0,
            )
        else:
            logger.debug(
                "SMSBower 取号梯队 country=%s service=%s cap=%s 档位=%s",
                country, service, user_cap, tried_pretty,
            )

        def _check_cost(data: dict) -> dict:
            cost_raw = data.get("activationCost") if isinstance(data, dict) else None
            try:
                cost = float(cost_raw) if cost_raw is not None else None
            except (TypeError, ValueError):
                cost = None
            if user_cap > 0 and cost is not None and cost > user_cap + 1e-6:
                aid = data.get("activationId")
                if aid is not None:
                    try:
                        self._request(
                            {"action": "setStatus", "id": aid, "status": 8},
                            needs_key=True,
                        )
                    except Exception:
                        pass
                raise RuntimeError(
                    f"COST_OVER_CAP: 卖家报价超上限 ${cost:.4f} > 用户上限 ${user_cap:.4f}"
                    f"（country={country}），已立即取消号码 {aid}（未实际付费）"
                )
            return data

        # 风控原则：单次取号"一把梭"，失败立刻让上层换国家；不再 120s 轮询硬等。
        # CO 钉死的 operator=3160 仍然附带（用户实测这一档能下 SMS），但只试一次档位序列，
        # NO_NUMBERS 就直接抛出去——上层会拿下一个推荐国家继续。
        for max_price_try in ladder:
            params = {
                "action": "getNumberV2",
                "service": service,
                "country": country,
                "maxPrice": max_price_try,
            }
            if pinned_operator:
                # SMSBower 的 getNumberV2 接受 operator 参数把卖家锁定到指定 provider_id。
                params["operator"] = pinned_operator
            try:
                resp = self._request(params)
                try:
                    data = resp.json()
                except ValueError:
                    data = None
                if isinstance(data, dict) and data.get("activationId"):
                    return _check_cost(data)
                v2_error = resp.text.strip()[:200]
            except Exception as exc:
                v2_error = str(exc)
            if "NO_NUMBERS" not in v2_error and "NO_BALANCE" not in v2_error:
                # 非"暂无号码 / 余额不足"的错误直接中止梯队
                break

        # 全部档位用尽
        raise RuntimeError(
            f"SMSBower 在 country={country} 的 user_cap=${user_cap:.4f} 内未找到可用号码"
            f"（档位 {tried_pretty}, operator={pinned_operator or '-'}）：{v2_error or 'NO_NUMBERS'}"
        )


def is_herosms_phone_cache_alive(config: dict | None = None) -> tuple[bool, dict]:
    """Return whether the current HeroSMS cache is reusable for scheduling."""
    config = dict(config or {})
    api_key = str(config.get("herosms_api_key") or "").strip()
    if not api_key:
        return False, {"alive": False}
    provider = HeroSmsProvider(
        api_key,
        default_service=str(config.get("sms_service") or HERO_SMS_DEFAULT_SERVICE),
        default_country=str(config.get("sms_country") or config.get("herosms_country") or HERO_SMS_DEFAULT_COUNTRY),
        phone_success_max=max(0, _safe_int(config.get("register_phone_success_max"), 3)),
    )
    info = provider.get_reuse_info()
    return bool(info.get("alive")), info


# ---------------------------------------------------------------------------
# Factory and browser callback adapter
# ---------------------------------------------------------------------------

def create_sms_provider(provider_key: str, config: dict) -> BaseSmsProvider:
    """Create an SMS provider instance from config."""
    if provider_key in ("sms_activate", "sms_activate_api"):
        api_key = config.get("sms_activate_api_key", "")
        if not api_key:
            raise RuntimeError("SMS-Activate 未配置 API Key")
        return SmsActivateProvider(
            api_key=api_key,
            default_country=config.get("sms_activate_country", config.get("sms_activate_default_country", "")),
            proxy=config.get("sms_proxy") or config.get("proxy") or None,
        )
    if provider_key in ("herosms", "herosms_api"):
        api_key = str(config.get("herosms_api_key", "") or "").strip()
        if not api_key:
            raise RuntimeError("HeroSMS 未配置 API Key")
        return HeroSmsProvider(
            api_key=api_key,
            default_service=str(config.get("sms_service") or config.get("herosms_service") or config.get("herosms_default_service") or HERO_SMS_DEFAULT_SERVICE),
            default_country=str(config.get("sms_country") or config.get("herosms_country") or config.get("herosms_default_country") or HERO_SMS_DEFAULT_COUNTRY),
            max_price=_safe_float(config.get("herosms_max_price"), -1),
            proxy=str(config.get("sms_proxy") or config.get("proxy") or "") or None,
            reuse_phone_to_max=_safe_bool(config.get("register_reuse_phone_to_max"), True),
            phone_success_max=max(0, _safe_int(config.get("register_phone_extra_max") or config.get("register_phone_success_max"), 3)),
        )
    if provider_key in ("smsbower", "smsbower_api"):
        api_key = str(config.get("smsbower_api_key", "") or "").strip()
        if not api_key:
            raise RuntimeError("SMSBower 未配置 API Key")
        return SmsBowerProvider(
            api_key=api_key,
            default_service=str(config.get("sms_service") or config.get("smsbower_service") or config.get("smsbower_default_service") or HERO_SMS_DEFAULT_SERVICE),
            default_country=str(config.get("sms_country") or config.get("smsbower_country") or config.get("smsbower_default_country") or HERO_SMS_DEFAULT_COUNTRY),
            max_price=_safe_float(config.get("smsbower_max_price"), -1),
            proxy=str(config.get("sms_proxy") or config.get("proxy") or "") or None,
            reuse_phone_to_max=_safe_bool(config.get("register_reuse_phone_to_max"), True),
            phone_success_max=max(0, _safe_int(config.get("register_phone_extra_max") or config.get("register_phone_success_max"), 3)),
        )
    raise RuntimeError(f"未知的接码服务: {provider_key}")


class PhoneCallbackController:
    """Callable phone callback with optional lifecycle hooks for advanced providers."""

    def __init__(self, provider_key: str, config: dict, *, service: str, country: str = "", log_fn=None):
        self.provider_key = provider_key
        self.config = dict(config or {})
        self.service = service
        self.country = country
        self.log = log_fn or logger.info
        self.provider: Optional[BaseSmsProvider] = None
        self.activation: Optional[SmsActivation] = None
        self.phase = "need_number"
        self.completed = False
        self._verify_lock_acquired = False
        self.awaiting_external_success = False
        # 国家轮换：第一次拿号失败 (NO_NUMBERS / 默认国家不可用) 时，按价格区间内的
        # 候选国家依次重试，避免被 "默认国家无库存" 卡死。
        self._candidate_countries: list[str] | None = None
        self._tried_countries: set[str] = set()
        # OpenAI add_phone 上不提供 SMS 选项的国家（只有 WhatsApp）。SMSBower 的号
        # 收不到 WhatsApp 验证码，所以下一轮拿号时跳过这些国家。
        self._sms_blocked_countries: set[str] = set()
        # 当前 __call__ 实际选中的国家（block_country_for_sms 用它定位是谁该进黑名单）
        self._last_picked_country: str = ""

    def _provider(self) -> BaseSmsProvider:
        if self.provider is None:
            self.provider = create_sms_provider(self.provider_key, self.config)
        return self.provider

    def _is_pinned_country(self, country: str) -> bool:
        """用户钉死的国家——COUNTRY_OPERATOR_PIN 里有 operator 强制锁的（CO=33）。

        钉死后**永远只在这个国家拿号**：不进 _tried_countries 黑名单，不轮换其它国家，
        NO_NUMBERS 由 provider 内部用 120s 轮询等待。
        """
        if not country:
            return False
        prov_cls = type(self._provider() if self.provider is not None else None)
        pin_map: dict[str, str] = {}
        if prov_cls is not None:
            pin_map = getattr(prov_cls, "COUNTRY_OPERATOR_PIN", None) or {}
        if not pin_map:
            # Fallback: 直接从 SmsBowerProvider 取（兜底）
            pin_map = getattr(SmsBowerProvider, "COUNTRY_OPERATOR_PIN", {}) or {}
        return str(country).strip() in pin_map

    def __call__(self) -> str:
        provider = self._provider()
        if self.phase == "need_number":
            if self.provider_key == "herosms" and not self._verify_lock_acquired:
                _HERO_SMS_VERIFY_LOCK.acquire()
                self._verify_lock_acquired = True

            # 智能国家选择：如果启用了 auto_select_country，自动查询最优国家
            effective_country = self.country
            # 用户首选国家（通常 CO=33）作为"第一枪"提示——operator pin 仍然会附带，
            # 但**不再钉死**：取号失败立刻让上层换推荐国家，避免反复在 CO 上踩 OpenAI
            # 风控（同一注册流程内多次拿 CO 号 / 多次 add_phone 容易被标）。
            preferred_country = effective_country
            if preferred_country and self._is_pinned_country(preferred_country):
                pin_op = (
                    getattr(SmsBowerProvider, "COUNTRY_OPERATOR_PIN", {}) or {}
                ).get(str(preferred_country).strip(), "")
                self.log(
                    f"首选国家 {preferred_country} (operator={pin_op or '-'}) "
                    f"作为第一枪，失败立刻换推荐池"
                )
            auto_select = _safe_bool(self.config.get("herosms_auto_country") or self.config.get("smsbower_auto_country"), False)
            if auto_select and isinstance(provider, HeroSmsProvider):
                self.log("正在查询最优国家（价格最低 + 库存充足）...")
                try:
                    min_stock = _safe_int(self.config.get("herosms_auto_country_min_stock") or self.config.get("smsbower_auto_country_min_stock"), 20)
                    max_price_limit = _safe_float(self.config.get("herosms_auto_country_max_price") or self.config.get("smsbower_auto_country_max_price"), 0)
                    best = provider.get_best_country(
                        service=self.service,
                        min_stock=min_stock,
                        max_price=max_price_limit,
                    )
                    if best:
                        self.log(f"自动选择最优国家: {best}")
                        effective_country = best
                    else:
                        self.log("未找到满足条件的国家，使用默认配置")
                except Exception as exc:
                    self.log(f"智能国家选择失败({exc})，使用默认配置")

            country_label = effective_country or self.config.get("sms_country") or self.config.get("sms_activate_country") or "default"
            self.log(f"已进入 add_phone，准备租用手机号: provider={self.provider_key} service={self.service} country={country_label}")
            self.log(f"正在从 {self.provider_key} 获取手机号...")

            # 用户配置的价格上限（如 SMSBower 0.06 USD），用于自动换国家时仍然遵守上限
            user_max_price = _safe_float(
                self.config.get("smsbower_max_price") or self.config.get("herosms_max_price"),
                0,
            )

            # 第一次尝试用 effective_country（用户选定或 auto-select 结果，
            # 也可能是空串——非 HeroSMS 类的 provider 默认就走自己的内部默认国家）。
            # 用户明确选了某个国家时（如 Colombia=33），那个国家通常是已知能过验证的便宜
            # 选项；SMSBower 偶尔会瞬时返回 NO_NUMBERS，加 1~2 次短暂 backoff 重试再回退到
            # 候选轮换，避免因一次性 NO_NUMBERS 就跳过用户首选国家。
            self.activation = None
            last_exc: Exception | None = None

            first_try_key = effective_country or "__default__"
            already_blocked_default = effective_country and effective_country in self._sms_blocked_countries
            if first_try_key not in self._tried_countries and not already_blocked_default:
                self._tried_countries.add(first_try_key)
                # 风控：用户首选国家也只试 1 次（COST_OVER_CAP 自动给 1 次额外重试不算）。
                # NO_NUMBERS 立刻让出来给推荐池下一个候选——避免反复在 CO 上踩同一个号段。
                default_country_retries = 1
                for default_attempt in range(default_country_retries):
                    try:
                        self.activation = provider.get_number(
                            service=self.service, country=effective_country
                        )
                        self._last_picked_country = effective_country
                        break
                    except Exception as exc:
                        last_exc = exc
                        err_str = str(exc)
                        err_lower = err_str.lower()
                        retryable = "no_numbers" in err_lower or "no numbers" in err_lower
                        # COST_OVER_CAP 已经 server-side 取消号码，没花钱；可以原国家再试一次
                        cost_over_cap = "cost_over_cap" in err_lower or "卖家报价超上限" in err_str
                        if default_attempt < default_country_retries - 1 and (retryable or cost_over_cap):
                            wait = 6
                            reason = "卖家报价超上限（已自动取消未付费）" if cost_over_cap else "暂无号码"
                            self.log(
                                f"国家 {effective_country or '默认'} {reason}，{wait}s 后重试 "
                                f"({default_attempt + 2}/{default_country_retries})..."
                            )
                            time.sleep(wait)
                            continue
                        self.log(
                            f"国家 {effective_country or '默认'} 获取号码失败: {err_str[:160]}"
                        )
                        break
            elif already_blocked_default:
                self.log(
                    f"国家 {effective_country} 已在 SMS 黑名单（OpenAI 只允许 WhatsApp），跳过"
                )

            # NO_NUMBERS / 默认国家无库存 → 自动按价格区间轮换
            if self.activation is None and isinstance(provider, HeroSmsProvider):
                if self._candidate_countries is None:
                    try:
                        rows = provider.iter_candidate_countries(
                            service=self.service,
                            max_price=user_max_price,
                            min_stock=1,
                        )
                        self._candidate_countries = [r["country"] for r in rows]
                        if self._candidate_countries:
                            preview = ",".join(self._candidate_countries[:8])
                            self.log(
                                f"自动准备价格 ≤ {user_max_price or '∞'} 的候选国家共 "
                                f"{len(self._candidate_countries)} 个，前几名: {preview}"
                            )
                    except Exception as exc:
                        self.log(f"准备候选国家列表失败: {exc}")
                        self._candidate_countries = []

                for try_country in (self._candidate_countries or []):
                    if try_country in self._tried_countries:
                        continue
                    if try_country in self._sms_blocked_countries:
                        # OpenAI 在这个国家只放 WhatsApp，跳过
                        self._tried_countries.add(try_country)
                        continue
                    self._tried_countries.add(try_country)
                    self.log(f"自动切换国家 -> {try_country} 重试拿号...")
                    try:
                        self.activation = provider.get_number(service=self.service, country=try_country)
                        if self.activation:
                            self._last_picked_country = try_country
                            effective_country = try_country
                            break
                    except Exception as exc:
                        last_exc = exc
                        err_str = str(exc)
                        if "cost_over_cap" in err_str.lower() or "卖家报价超上限" in err_str:
                            self.log(
                                f"国家 {try_country} 卖家报价超上限（已自动取消未付费）: "
                                f"{err_str[:160]}"
                            )
                        else:
                            self.log(f"国家 {try_country} 拿号失败: {err_str[:160]}")

            if self.activation is None:
                if self._verify_lock_acquired:
                    _HERO_SMS_VERIFY_LOCK.release()
                    self._verify_lock_acquired = False
                # 抛出前清掉本轮 rotation 状态，这样外层 _handle_add_phone_challenge
                # 在 cleanup() + 再次调用 callback() 时能从头开始重新尝试（包括
                # 用户的默认国家），而不是把所有候选都视为已尝试。
                self._tried_countries.clear()
                self._candidate_countries = None
                raise last_exc or RuntimeError(
                    f"{self.provider_key} 在价格上限 {user_max_price or '∞'} USD 内未找到可用号码（所有候选国家都已尝试）"
                )
            self.phase = "need_code"
            reused = bool((self.activation.metadata or {}).get("reused"))
            reuse_label = "复用号码" if reused else "新号码"
            self.log(f"已成功租到号码({reuse_label}): {self.activation.phone_number} (activation_id={self.activation.activation_id})")
            return self.activation.phone_number

        if self.phase == "need_code" and self.activation:
            # 用户要求：OpenAI add_phone OTP 页面已就绪时，最长等 5 分钟接码——SMSBower
            # 的 SA/CA/TH 等推荐国家偶尔需要 1~2 分钟落地，180s 容易"刚好等不到就放弃"。
            self.log(
                f"等待短信验证码... (activation_id={self.activation.activation_id}, 最长 300s)"
            )
            code = provider.get_code(self.activation.activation_id, timeout=300)
            if code:
                self.log(f"收到验证码: {code}")
                if getattr(provider, "auto_report_success_on_code", True):
                    self.report_success()
                else:
                    self.awaiting_external_success = True
            else:
                self.log(f"⚠️ 未收到验证码: activation_id={self.activation.activation_id}")
            return code
        return ""

    def set_resend_callback(self, callback: Callable[[], None] | None) -> None:
        if self.provider is not None:
            self.provider.set_resend_callback(callback)
        else:
            original_provider = self._provider()
            original_provider.set_resend_callback(callback)

    def mark_code_failed(self, reason: str = "") -> None:
        if self.activation and self.provider:
            hook = getattr(self.provider, "mark_code_failed", None)
            if callable(hook):
                hook(self.activation.activation_id, reason=reason)
            self.phase = "need_code"
            self.awaiting_external_success = False

    def mark_send_failed(self, reason: str = "") -> None:
        if self.activation and self.provider:
            hook = getattr(self.provider, "mark_send_failed", None)
            if callable(hook):
                hook(self.activation.activation_id, reason=reason)
            self.awaiting_external_success = False

    def mark_send_succeeded(self) -> None:
        if self.activation and self.provider:
            hook = getattr(self.provider, "mark_send_succeeded", None)
            if callable(hook):
                hook(self.activation.activation_id)

    def report_success(self) -> None:
        if self.activation and self.provider and not self.completed:
            activation_id = self.activation.activation_id
            phone_no = self.activation.phone_number
            self.provider.report_success(activation_id)
            self.completed = True
            self.phase = "done"
            self.awaiting_external_success = False
            self.log(f"短信验证成功，已标记号码完成使用: activation_id={activation_id}")
            # 风控：OpenAI 注册成功后绝对**不能跨账号复用同一个号**。OpenAI 一眼就能
            # 把两个邮箱串到同一个 phone_number 上，触发风控（refresh_token 拿不到 /
            # 邮箱冻结 / 后续 add_phone 被卡）。所以这里强制清掉 HeroSMS/SMSBower 的
            # 进程级复用 cache，让下一个邮箱必然拿全新号码——哪怕 config 里
            # reuse_phone_to_max=True / phone_success_max=3 也覆盖掉。
            try:
                provider = self.provider
                if isinstance(provider, HeroSmsProvider):
                    with _HERO_SMS_CACHE_LOCK:
                        cache = _HERO_SMS_CACHE
                        if cache and str(cache.get("activation_id")) == str(activation_id):
                            provider._clear_cache()
                            self.log(
                                f"已清除复用 cache: phone={phone_no} "
                                f"activation_id={activation_id} (避免跨账号复用)"
                            )
            except Exception as exc:
                self.log(f"清除复用 cache 时出错（忽略）: {exc}")
            # 同步把当前 controller 的 activation 引用断掉，防止后续误用。
            self.activation = None
        if self._verify_lock_acquired:
            _HERO_SMS_VERIFY_LOCK.release()
            self._verify_lock_acquired = False

    def block_country_for_sms(self) -> str:
        """把当前选中的国家加入 SMS 黑名单，下次拿号跳过它。

        OpenAI 在某些国家只放 WhatsApp（SMSBower 的号码收不到 WhatsApp 验证码），
        ``_handle_add_phone_challenge`` 探测到这种情况后会调这里。返回被加入黑名单的
        国家 ID（如果当前没有选中国家则返回空串）。
        """
        cid = str(self._last_picked_country or "").strip()
        if cid:
            self._sms_blocked_countries.add(cid)
        return cid

    def cleanup(self) -> None:
        if self.activation and not self.completed:
            try:
                provider = self._provider()
                if self.awaiting_external_success and not getattr(provider, "auto_report_success_on_code", True):
                    self.report_success()
                else:
                    provider.cancel(self.activation.activation_id)
                    self.log(f"已释放未使用号码: activation_id={self.activation.activation_id}")
            except Exception:
                pass
        # 把当前激活号码状态清掉，但保留 _candidate_countries / _tried_countries，
        # 这样上层 _handle_add_phone_challenge 触发换号重试时能继续轮换而不是反复试同一个国家。
        self.activation = None
        self.completed = False
        self.awaiting_external_success = False
        if self._verify_lock_acquired:
            _HERO_SMS_VERIFY_LOCK.release()
            self._verify_lock_acquired = False


def create_phone_callbacks(
    provider_key: str,
    config: dict,
    *,
    service: str,
    country: str = "",
    log_fn=None,
) -> tuple:
    """Create (phone_callback, cleanup) tuple for browser registration."""
    controller = PhoneCallbackController(
        provider_key,
        config,
        service=service,
        country=country,
        log_fn=log_fn,
    )
    return controller, controller.cleanup
