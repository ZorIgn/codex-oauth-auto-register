"""重复跑 protocol 模式 checkout 直到走到 paypal_signup（绕过间歇 TLS/stripe error）。"""
import json
import sys
import time
import urllib.request

SMS_POOL = (
    "+15822063090----https://mail-api.yuecheng.shop/api/public/message?key=eca_tr_GnJcbkEBhEzX9IzHYD9mCMSX\n"
)

body = json.dumps({
    "params": {
        "plan": "plus", "country": "US", "currency": "USD",
        "payment_method": "paypal", "auto_checkout": "true",
        "checkout_mode": "protocol", "headless": "false",
        "checkout_timeout": 180,
        "sms_pool": SMS_POOL,
    }
}).encode()

max_attempts = int(sys.argv[1]) if len(sys.argv) > 1 else 5
wait_sec = int(sys.argv[2]) if len(sys.argv) > 2 else 60

for attempt in range(max_attempts):
    req = urllib.request.Request(
        "http://127.0.0.1:8000/api/actions/chatgpt/52/payment_link",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    r = urllib.request.urlopen(req)
    task_id = json.loads(r.read())["task_id"]
    print(f"[{attempt + 1}] task: {task_id}")
    time.sleep(wait_sec)
    r2 = urllib.request.urlopen("http://127.0.0.1:8000/api/tasks/" + task_id)
    d = json.loads(r2.read())
    err = (d.get("error") or "")[:300]
    status = d.get("status")
    print(f"    status={status}, error={err}")
    if "accessToken" in err or "paypal_signup" in err.lower():
        print("    -> reached signup stage, stopping retry")
        break
    if status in ("completed", "success"):
        print("    -> SUCCESS")
        break
