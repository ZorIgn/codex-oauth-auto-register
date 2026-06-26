# Codex OAuth Auto Register

> 一个面向 Codex OAuth、CPA 账号导出和接码轮询流程的本地账号注册与管理工具。
>
> 这个项目来自一次真实业务链路的复盘：我原本希望用自己的注册机持续给闲鱼自动发货系统供货，但高峰订单下注册产量不够稳定，最后主链路改成「CPA JSON 批量导入 + 本地库存中心管理 + 闲鱼自动发货」。本仓库保留为供给侧工具，用于后续账号注册、OAuth 验证、导出和二次开发。

## 项目定位

`Codex OAuth Auto Register` 不是一个通用营销后台，也不再沿用原项目的发布说明。它现在被整理为一个独立的工程仓库，重点解决以下问题：

- 自动化注册或导入 AI 平台账号。
- 对 ChatGPT / Codex 相关账号执行 OAuth 验证与状态回写。
- 将账号导出为 CPA JSON 等库存系统可消费的格式。
- 通过接码服务自动取号、轮询短信验证码、处理失败重试。
- 为库存履约系统提供账号有效性检查和后续扩展基础。

## 核心能力

- **账号注册编排**：支持协议模式、浏览器模式和可扩展的平台插件。
- **Codex OAuth 验证**：提供账号级 OAuth start / complete 流程，便于判断账号是否可用于 Codex 相关操作。
- **CPA 导出**：把账号整理成 CPA 格式，方便导入库存中心或卡密履约系统。
- **接码自动轮询**：封装 SMSPool、SMSBower、SMSCloud、Hero-SMS 等接码渠道的取号、等待验证码和失败处理。
- **账号状态管理**：保存账号生命周期、有效性、token 状态、导出状态和平台扩展信息。
- **本地 Web UI**：提供账号列表、注册任务、配置管理、任务日志和导出入口。
- **扩展接口**：保留平台插件结构，方便继续接入新平台或替换邮箱、代理、验证码、接码 provider。

## 与闲鱼履约项目的关系

配套项目：[`xianyu-codex-commerce-suite`](https://github.com/ZorIgn/xianyu-codex-commerce-suite)

两个项目的分工如下：

```text
Codex OAuth Auto Register
  负责账号注册、OAuth 验证、CPA 导出、接码轮询

xianyu-codex-commerce-suite
  负责闲鱼接入、库存导入、订单出库、自动发货、买家触发激活
```

实际使用时，可以先在本项目里注册或整理账号，再导出 CPA JSON，最后导入到闲鱼履约项目的库存中心。由于真实订单场景对供货速度要求很高，生产链路更推荐使用批量 CPA 导入作为主供给方式，本项目作为补货和验证工具使用。

## 目录结构

```text
.
├─ api/                         # FastAPI 路由
├─ application/                 # 应用服务与业务编排
├─ core/                        # 通用执行器、provider、账号模型和基础能力
├─ domain/                      # 领域对象
├─ infrastructure/              # 数据库与运行时适配
├─ platforms/                   # 各平台插件与平台专用能力
├─ providers/                   # 邮箱、代理、验证码、接码 provider
├─ frontend/                    # Web 管理端源码
├─ static/                      # 已构建的 Web UI 静态资源
├─ tests/                       # 单元测试与集成测试
├─ main.py                      # 本地后端入口
├─ requirements.txt             # Python 依赖
└─ .env.example                 # 环境变量示例
```

## 快速开始

### 1. 准备环境

建议放在非系统盘运行，例如 `E:\account`。

需要：

- Python 3.11+
- Node.js 18+（仅开发前端时需要）
- Git
- 可选：Docker、浏览器自动化运行环境、代理服务

### 2. 创建虚拟环境

```powershell
cd E:\account\codex-oauth-auto-register
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
```

### 3. 配置环境变量

复制示例配置：

```powershell
copy .env.example .env
```

然后按需填写：

- 邮箱 provider 配置
- 验证码 provider 配置
- 接码平台 API Key
- 代理配置
- Codex / ChatGPT 相关扩展配置

不要把 `.env`、账号数据库、导出的账号 JSON 或运行日志提交到 Git。

### 4. 启动后端

```powershell
.\.venv\Scripts\activate
python main.py
```

默认会启动本地 Web 服务，具体端口以控制台输出为准。

## CPA 导出流程

典型流程：

1. 在 Web UI 中注册或导入账号。
2. 对需要交付的账号执行有效性检查或 Codex OAuth 验证。
3. 在账号列表里选择导出 CPA。
4. 将导出的 JSON 导入 `xianyu-codex-commerce-suite` 的库存中心。
5. 由库存中心负责订单出库、自动发货和激活状态机。

## Codex OAuth 流程

本项目保留了 Codex OAuth 相关接口和页面能力，目标是验证账号是否满足后续 Codex 使用或激活要求。

一般流程：

1. 选择账号。
2. 发起 OAuth start。
3. 在浏览器或本地流程中完成授权。
4. 调用 OAuth complete。
5. 写回账号 token、有效性和导出状态。

具体实现分布在：

- `api/accounts.py`
- `application/ctf_plus.py`
- `platforms/chatgpt/oauth.py`
- `platforms/chatgpt/register.py`
- `platforms/chatgpt/switch.py`

## 接码轮询

接码能力用于手机号注册和短信验证码场景。当前整理过的渠道包括：

- SMSPool
- SMSBower
- SMSCloud
- Hero-SMS

相关代码：

- `platforms/gopay/sms_channel.py`
- `gopay-auto-protocol/smscloud_client.py`
- `providers/sms/`

接码平台 API Key 应只放在 `.env` 或运行环境中，不要写死在源码里。

## 安全说明

这个仓库已经按公开/协作仓库标准做过一次清理：

- 不提交 `.env`。
- 不提交本地数据库。
- 不提交账号 JSON、CPA 库存、cookies、session token。
- 不提交 HAR、日志、浏览器 profile、运行缓存。
- 不提交真实 GoPay 工作账号文件。
- `.gitignore` 已覆盖常见敏感产物。

如果继续二次开发，提交前建议至少执行：

```powershell
rg -n --hidden --glob '!**/.git/**' "sk-|ghp_|github_pat_|access_token|refresh_token|__Secure-next-auth|_m_h5_tk|eyJhbGci"
Get-ChildItem -Recurse -Force -File -Include .env,*.db,*.sqlite,*.sqlite3,*.log,acc*.json,*.har
```

发现真实 token、账号库或 cookies 时，必须先移除并重写本地提交历史后再推送。

## 参考项目

本项目是二次开发整理后的独立工程，部分结构和能力参考了以下项目：

- [`asz798838958/aBaiAutoplus`](https://github.com/asz798838958/aBaiAutoplus)
- [`lxf746/any-auto-register`](https://github.com/lxf746/any-auto-register)
- [`dreamhunter2333/cloudflare_temp_email`](https://github.com/dreamhunter2333/cloudflare_temp_email)
- [`lxf746/any2api`](https://github.com/lxf746/any2api)

感谢原作者的开源工作。本仓库与上述项目独立维护。

## 免责声明

本项目仅用于学习、研究和本地自动化流程验证。请遵守目标平台服务条款、当地法律法规和第三方服务规则。使用本项目造成的账号风险、服务限制或其他后果由使用者自行承担。