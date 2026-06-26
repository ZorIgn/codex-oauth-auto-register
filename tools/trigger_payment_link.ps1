# 对已有账号触发 payment_link action（协议模式 checkout）。
# 用法: .\tools\trigger_payment_link.ps1 -AccountId 81

param(
    [Parameter(Mandatory=$true)][int]$AccountId,
    [string]$BaseUrl = "http://127.0.0.1:8000"
)

$smsPool = @"
+16862102198----https://mail-api.yuecheng.shop/api/public/message?key=eca_tr_zh8PB3P6vnYwjdVgC9xfUVqQ
+15722188973----https://mail-api.yuecheng.shop/api/public/message?key=eca_tr_FHrJZJVydYUE7iFdzYLhmB26
"@

$body = @{
    params = @{
        plan            = "plus"
        country         = "US"
        currency        = "USD"
        auto_checkout   = "true"
        payment_method  = "paypal"
        checkout_mode   = "protocol"
        headless        = "true"
        sms_pool        = $smsPool
        checkout_timeout = 300
    }
} | ConvertTo-Json -Depth 5

Write-Host "POST $BaseUrl/api/actions/chatgpt/$AccountId/payment_link"
$r = Invoke-WebRequest `
    -Uri "$BaseUrl/api/actions/chatgpt/$AccountId/payment_link" `
    -Method POST `
    -ContentType "application/json" `
    -Body $body `
    -TimeoutSec 30 `
    -UseBasicParsing
Write-Host "STATUS=$($r.StatusCode)"
$task = $r.Content | ConvertFrom-Json
Write-Host "task_id=$($task.id)"
Write-Host "status=$($task.status)"
$task | ConvertTo-Json -Depth 5
