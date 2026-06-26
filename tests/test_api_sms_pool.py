"""SMS 号码池黑名单 API 与仓储测试。"""
from __future__ import annotations


def test_list_blacklist_empty(client):
    resp = client.get("/api/sms-pool/blacklist")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 0
    assert body["items"] == []


def test_add_blacklist_creates_record(client):
    resp = client.post(
        "/api/sms-pool/blacklist",
        json={
            "phone": "+15822063090",
            "relay_url": "https://mail-api.yuecheng.shop/api/public/message?key=eca_tr_GnJcbk",
            "reason": "oas_error",
            "error_code": "OAS_ERROR",
            "task_id": "task_demo",
            "error_message": "createMemberAccount risk",
        },
    )
    assert resp.status_code == 200
    item = resp.json()
    assert item["phone_e164"] == "+15822063090"
    assert item["relay_host"] == "mail-api.yuecheng.shop"
    assert item["reason"] == "oas_error"
    assert item["fail_count"] == 1


def test_add_blacklist_increments_fail_count(client):
    payload = {"phone": "+15822063090", "reason": "oas_error"}
    client.post("/api/sms-pool/blacklist", json=payload)
    second = client.post("/api/sms-pool/blacklist", json=payload).json()
    assert second["fail_count"] == 2


def test_add_blacklist_rejects_empty_phone(client):
    resp = client.post("/api/sms-pool/blacklist", json={"phone": "   "})
    assert resp.status_code == 400


def test_remove_blacklist_ok(client):
    client.post("/api/sms-pool/blacklist", json={"phone": "+15822063090"})
    resp = client.delete("/api/sms-pool/blacklist/+15822063090")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    list_resp = client.get("/api/sms-pool/blacklist")
    assert list_resp.json()["total"] == 0


def test_remove_blacklist_missing(client):
    resp = client.delete("/api/sms-pool/blacklist/+19999999999")
    assert resp.status_code == 404


def test_clear_blacklist(client):
    for phone in ("+15822063090", "+15822063091"):
        client.post("/api/sms-pool/blacklist", json={"phone": phone})
    resp = client.delete("/api/sms-pool/blacklist")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["removed"] == 2
    assert client.get("/api/sms-pool/blacklist").json()["total"] == 0


# ── Repository unit tests ───────────────────────────────────────────────────


def test_repository_filter_pool_drops_blacklisted():
    from infrastructure.sms_pool_repository import SmsPoolBlacklistRepository

    repo = SmsPoolBlacklistRepository()
    repo.add(phone="+15822063090", reason="oas_error")

    pool = [
        {"phone_e164": "+15822063090", "phone": "5822063090", "relay_url": "https://ex"},
        {"phone_e164": "+15822064144", "phone": "5822064144", "relay_url": "https://ex"},
    ]
    kept, skipped = repo.filter_pool(pool)
    assert [item["phone_e164"] for item in kept] == ["+15822064144"]
    assert [item["phone_e164"] for item in skipped] == ["+15822063090"]
    assert skipped[0]["skipped_reason"] == "blacklisted"


def test_repository_filter_pool_no_blacklist_pass_through():
    from infrastructure.sms_pool_repository import SmsPoolBlacklistRepository

    repo = SmsPoolBlacklistRepository()
    pool = [{"phone_e164": "+15822063090", "relay_url": "x"}]
    kept, skipped = repo.filter_pool(pool)
    assert kept == pool
    assert skipped == []


def test_repository_is_blacklisted_normalizes_input():
    from infrastructure.sms_pool_repository import SmsPoolBlacklistRepository

    repo = SmsPoolBlacklistRepository()
    repo.add(phone="+15822063090")
    # 没有 + 前缀 / 含空格 / 含括号也能命中
    assert repo.is_blacklisted("+15822063090")
    assert repo.is_blacklisted("15822063090") is False  # 严格区分 + 前缀
    assert repo.is_blacklisted("+1 (582) 206-3090")
