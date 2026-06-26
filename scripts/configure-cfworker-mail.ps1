param(
  [Parameter(Mandatory=$true)]
  [string]$ApiUrl,

  [Parameter(Mandatory=$true)]
  [string]$AdminToken,

  [string]$Domain = "woaiwanyuanshen.dpdns.org",
  [string]$ABaiUrl = "http://127.0.0.1:8010",
  [string]$ProxyUrl = "http://127.0.0.1:7890",
  [string]$CpaUrl = "http://127.0.0.1:8317",
  [string]$CpaKey = "sk-123456"
)

$ErrorActionPreference = "Stop"

function Invoke-Json {
  param(
    [string]$Url,
    [string]$Method = "GET",
    $Body = $null
  )

  $args = @{
    Uri = $Url
    Method = $Method
    UseBasicParsing = $true
  }

  if ($null -ne $Body) {
    $args.ContentType = "application/json"
    $args.Body = ($Body | ConvertTo-Json -Depth 12)
  }

  (Invoke-WebRequest @args).Content
}

$ApiUrl = $ApiUrl.TrimEnd("/")

Invoke-Json "$ABaiUrl/api/config" "PUT" @{
  data = @{
    default_executor = "headed"
    cpa_api_url = $CpaUrl
    cpa_api_key = $CpaKey
  }
} | Write-Host

try {
  Invoke-Json "$ABaiUrl/api/proxies" "POST" @{
    url = $ProxyUrl
    region = "US"
  } | Write-Host
} catch {
  Write-Host "Proxy may already exist: $($_.Exception.Message)"
}

Invoke-Json "$ABaiUrl/api/provider-settings" "POST" @{
  provider_type = "mailbox"
  provider_key = "cfworker_admin_api"
  display_name = "woaiwanyuanshen Cloudflare Mail"
  auth_mode = "token"
  enabled = $true
  is_default = $true
  config = @{
    cfworker_api_url = $ApiUrl
    cfworker_domain = $Domain
    cfworker_fingerprint = ""
  }
  auth = @{
    cfworker_admin_token = $AdminToken
  }
  metadata = @{}
} | Write-Host

Invoke-Json "$ABaiUrl/api/provider-settings" "POST" @{
  provider_type = "captcha"
  provider_key = "local_solver"
  display_name = "Local Solver"
  auth_mode = ""
  enabled = $true
  is_default = $true
  config = @{
    solver_url = "http://127.0.0.1:8889"
  }
  auth = @{}
  metadata = @{}
} | Write-Host

$test = Invoke-Json "$ABaiUrl/api/provider-settings/test" "POST" @{
  provider_type = "mailbox"
  provider_key = "cfworker_admin_api"
  config = @{
    cfworker_api_url = $ApiUrl
    cfworker_domain = $Domain
    cfworker_fingerprint = ""
  }
  auth = @{
    cfworker_admin_token = $AdminToken
  }
}

Write-Host $test
