"""自动测试脚本：通过 /api/actions 触发 PayPal 协议 checkout，流式抓 task 日志。

依赖 uvicorn 在 127.0.0.1:8000 已启动。Windows PowerShell 调用：
    .venv\\Scripts\\python tools/selfrun_paypal_checkout.py [account_id]
"""
from __future__ import annotations

import json
import sys
import time

import urllib.request
import urllib.error


BASE = "http://127.0.0.1:8000"

SMS_POOL = "\n".join([
    "+15722191763----https://mail-api.yuecheng.shop/api/public/message?key=eca_tr_j8QvRHaKYrNS6SyttU9eriaN",
    "+15574448918----https://mail-api.yuecheng.shop/sms-record?token=eca_tr_SoaLQpDRAPjhzMUBUKBLHqoK",
    "+15822940577----https://mail-api.yuecheng.shop/api/get_sms?key=eca_tr_4K5Da3bMbIK7dyv36rBxfNCY",
    "+15722188973----https://mail-api.yuecheng.shop/api/public/message?key=eca_tr_FHrJZJVydYUE7iFdzYLhmB26",
    "+18262563474----https://mail-api.yuecheng.shop/api/public/message?key=eca_tr_4bukMF26tmGRDnp7cWT15SHe",
    "+19439433197----https://mail-api.yuecheng.shop/api/get_sms?key=eca_tr_7NqncjGGeUtnzrX5VWdkPx1Q",
])


def http_post(path: str, payload: dict) -> dict:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        BASE + path, data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def http_get(path: str) -> dict:
    with urllib.request.urlopen(BASE + path, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def wait_server(retries: int = 20) -> None:
    for i in range(retries):
        try:
            with urllib.request.urlopen(BASE + "/api/health", timeout=3) as r:
                if r.status == 200:
                    print(f"[selfrun] backend up after {i + 1}s")
                    return
        except Exception:
            pass
        time.sleep(1)
    raise SystemExit("[selfrun] backend not reachable on " + BASE)


def main() -> None:
    account_id = int(sys.argv[1]) if len(sys.argv) > 1 else 70

    wait_server()

    params = {
        "country": "US",
        "currency": "USD",
        "plan": "plus",
        "auto_checkout": "true",
        "payment_method": "paypal",
        "headless": "false",
        "checkout_mode": "protocol",
        "checkout_timeout": 240,
        "sms_pool": SMS_POOL,
    }
    print(f"[selfrun] POST /api/actions/chatgpt/{account_id}/payment_link  with {len(SMS_POOL.splitlines())} SMS rows")
    task = http_post(
        f"/api/actions/chatgpt/{account_id}/payment_link",
        {"params": params},
    )
    if "id" not in task:
        print(f"[selfrun] action returned (sync?): {json.dumps(task, ensure_ascii=False)[:500]}")
        return
    task_id = task["id"]
    print(f"[selfrun] task_id={task_id} status={task.get('status')}")

    cursor = 0
    deadline = time.monotonic() + 360.0
    last_print = time.monotonic()
    while time.monotonic() < deadline:
        try:
            events = http_get(f"/api/tasks/{task_id}/events?since={cursor}&limit=200")
        except urllib.error.HTTPError as exc:
            print(f"[selfrun] event poll HTTP {exc.code}: {exc.reason}")
            time.sleep(1)
            continue
        items = events.get("items") or []
        for item in items:
            cursor = max(cursor, int(item.get("id") or 0))
            line = (item.get("line") or "").strip()
            if line:
                print(f"  {line}")
        last_print = time.monotonic() if items else last_print
        try:
            current = http_get(f"/api/tasks/{task_id}")
        except Exception as exc:
            print(f"[selfrun] task GET failed: {exc}")
            time.sleep(1)
            continue
        status = current.get("status")
        if status in ("succeeded", "failed", "cancelled", "interrupted"):
            print(f"[selfrun] terminal status={status} error={current.get('error') or ''}")
            return
        time.sleep(1.5)

    print("[selfrun] timeout 6min reached, giving up.")


if __name__ == "__main__":
    main()
