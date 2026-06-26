# Codex OAuth Auto Register

> 基于 aBaiAutoplus / any-auto-register 的二次开发版本，重点服务于 Codex OAuth 验证、CPA 库存导出、接码自动轮询和本地账号供给流程。
>
> 这个仓库也是 `xianyu-codex-commerce-suite` 的配套注册机项目。最初我想用自己的注册机给闲鱼自动发货链路稳定供货，但真实跑单时产量跟不上高峰订单，所以最终主链路改成「批量 CPA JSON 导入 + 本地库存中心出库」；这个仓库保留为注册、验证、导出和后续二次开发的基础设施。

<p align=center>
  <b>Codex OAuth / CPA 导出 / 接码轮询增强版 · 多平台 AI 账号自动注册与管理</b>
</p>

<p align=center>
  <a href=https://github.com/ZorIgn/codex-oauth-auto-register><img src=https://img.shields.io/badge/project-Codex%20OAuth%20Auto%20Register-blue?style=for-the-badge alt=Project /></a>
  <a href=https://github.com/asz798838958/aBaiAutoplus><img src=https://img.shields.io/badge/based%20on-aBaiAutoplus-FFB003?style=for-the-badge alt=aBaiAutoplus /></a>
  <a href=https://github.com/lxf746/any-auto-register><img src=https://img.shields.io/badge/upstream-any--auto--register-green?style=for-the-badge alt=any-auto-register /></a>
  <a href=LICENSE><img src=https://img.shields.io/badge/license-see%20LICENSE-orange?style=for-the-badge alt=License /></a>
</p>

<p align=center>
  <b>ChatGPT / Codex / Cursor / Kiro / Grok / Windsurf / Trae 等 AI 平台账号注册与管理</b><br/>
  <b>协议 / 浏览器双模式 · 接码轮询 · CPA 导出 · Codex OAuth 激活辅助</b>
</p>

> 警告：本项目仅供学习和研究使用，不得用于任何商业用途，也不得用于违反目标平台服务条款（ToS）的行为。使用本项目所产生的一切后果由使用者自行承担。

> 致谢与来源：本项目是在 [`asz798838958/aBaiAutoplus`](https://github.com/asz798838958/aBaiAutoplus) 基础上的二次开发；原项目又基于 [`lxf746/any-auto-register`](https://github.com/lxf746/any-auto-register) 的插件化注册框架。感谢原作者和上游作者的开源工作。本仓库与原项目、上游项目各自独立维护。

多平台账号自动注册与管理系统，支持插件化扩展，内置 Web UI 与桌面客户端。本版本重点整理 Codex OAuth 验证、CPA 导出、本地库存供给、接码自动轮询和账号有效性复核相关链路。

## 项目来源与二次开发说明

参考与继承：

- 原改造项目：[`asz798838958/aBaiAutoplus`](https://github.com/asz798838958/aBaiAutoplus)
- 上游注册框架：[`lxf746/any-auto-register`](https://github.com/lxf746/any-auto-register)
- Cloudflare 临时邮箱参考：[`dreamhunter2333/cloudflare_temp_email`](https://github.com/dreamhunter2333/cloudflare_temp_email)
- Any2API 联动参考：[`lxf746/any2api`](https://github.com/lxf746/any2api)
- 配套闲鱼自动发货仓储项目：[`ZorIgn/xianyu-codex-commerce-suite`](https://github.com/ZorIgn/xianyu-codex-commerce-suite)

本仓库新增或重点整理的能力：

- 新增 Codex OAuth 验证流程接口，支持账号级 `start / complete` 激活链路。
- 新增 Codex OAuth token 持久化与本地账号状态回写能力，便于后续库存管理和有效性检查。
- 新增 CPA 格式导出能力，可对接仓储中心或其他卡密库存系统。
- 扩展接码轮询能力，包含 SMSPool、SMSBower、SMSCloud、Hero-SMS 等渠道的自动取号、轮询和失败处理。
- 整理本地 Codex Desktop / CLI 账号切换与验证相关工具，方便验证账号是否可用于 Codex 邀请、激活或后续人工复核。
- 保留 GoPay / ChatGPT Plus 协议化付款相关实验代码，但发布版本不包含本地密钥、账号库、运行日志或个人环境配置。

## 目录

- [相比上游的新增能力](#相比上游的新增能力)
- [功能特性](#功能特性)
- [支持的平台](#支持的平台)
- [界面预览](#界面预览)
- [技术栈](#技术栈)
- [快速开始](#快速开始)
- [桌面版下载](#桌面版下载)
- [Docker 部署](#docker-部署)
- [GoPay 付款 ChatGPT Plus](#gopay-付款-chatgpt-plus)
- [邮箱服务配置](#邮箱服务配置)
- [验证码服务配置](#验证码服务配置)
- [代理池配置](#代理池配置)
- [接码服务配置](#接码服务配置)
- [账号生命周期管理](#账号生命周期管理)
- [注册成功率仪表盘](#注册成功率仪表盘)
- [Any2API 联动](#any2api-联动)
- [项目结构](#项目结构)
- [插件开发](#插件开发)
- [安全说明](#安全说明)
- [常见问题](#常见问题)
- [参与贡献](#参与贡献)
- [License](#license)

## 相比上游的新增能力

本项目在 [`any-auto-register`](https://github.com/lxf746/any-auto-register) 基础上重点扩展：

| 新增能力                       | 说明                                                                          |
| ------------------------------ | ----------------------------------------------------------------------------- |
| � **GoPay 付款 ChatGPT Plus** | 印尼 GoPay 协议化付款，自动完成 ChatGPT Plus 订阅的「生成支付链接 → Midtrans 收银台 → GoPay 14 步 API 付款」全链路 |
| � **GoPay 账号自动注册**      | 印尼手机号 + PIN 协议注册 GoPay 账号，支持接码渠道轮换                         |
| 🧾 **接码渠道扩展**            | 在原有 SMS-Activate / HeroSMS 之外，新增 SMSPool、SMSBower 渠道                |
| 🌐 **C 端 / 管理端独立 API**   | `customer_portal_api/` 提供可独立部署的多租户门户后端                          |

> 其余平台注册、邮箱 / 验证码 / 代理 provider、生命周期管理、成功率仪表盘等能力沿用并兼容上游框架。

## 功能特性

- **多平台支持**：ChatGPT、Cursor、Kiro、Trae.ai、Tavily、Grok、Blink、Cerebras、OpenBlockLabs、Windsurf、GoPay，支持自定义插件扩展（Anything 通用适配器）
- **多邮箱服务**：MoeMail（自建）、Laoudo、DuckMail、Testmail、Cloudflare Worker 自建邮箱、Freemail、TempMail.lol、Temp-Mail Web、DuckDuckGo Email
- **多执行模式**：API 协议（无浏览器）、无头浏览器、有头浏览器（各平台按需支持）
- **验证码服务**：YesCaptcha、2Captcha、本地 Solver（Camoufox）
- **接码服务**：SMS-Activate、HeroSMS、SMSPool、SMSBower
- **代理池管理**：静态代理轮询 + 动态代理 API 提取 + 旋转网关代理，成功率统计、自动禁用失效代理
- **账号生命周期**：定时有效性检测、token 自动续期、trial 过期预警
- **注册成功率仪表盘**：按平台、按天、按代理的成功率统计，错误聚合分析
- **并发注册**：可配置并发数
- **实时日志**：SSE 实时推送注册日志到前端
- **账号导出**：支持 JSON、CSV、CPA、Sub2API、Kiro-Go、Any2API 多种格式
- **Any2API 联动**：注册完成后自动推送账号到 Any2API 网关，注册即可用
- **平台扩展操作**：各平台可自定义操作（如 Kiro 账号切换、Trae Pro 升级链接生成、GoPay 付款 Plus）

## 支持的平台

| 平台          | 协议模式 | 浏览器模式 | OAuth | 备注                         |
| ------------- | :------: | :--------: | :---: | ---------------------------- |
| ChatGPT       |    ✅    |     ✅     |  ✅   | Plus 支付链接 / PayPal 结账  |
| Cursor        |    ✅    |     ✅     |  ✅   | 需手机验证                   |
| Kiro          |    ✅    |     ✅     |  ✅   | 支持账号切换                 |
| Trae.ai       |    ✅    |     ✅     |  ✅   | Pro 升级链接生成             |
| Grok          |    ✅    |     ✅     |  ✅   |                              |
| Windsurf      |    ✅    |     ✅     |  ✅   | Trial 链接生成               |
| Tavily        |    ✅    |     ✅     |  ✅   |                              |
| Blink         |    ✅    |     ✅     |  ✅   |                              |
| Cerebras      |    ✅    |     ✅     |  ✅   |                              |
| OpenBlockLabs |    ✅    |     ✅     |  ✅   |                              |
| GoPay         |    ✅    |     —      |  —    | 印尼 GoPay，手机 + PIN，付款 Plus |
| Anything      |    ✅    |     ✅     |  —    | 通用适配器，配置即接入新平台 |

> 各平台实际支持的执行器以插件 `supported_executors` 声明为准，可在 Web UI「平台能力」页查看与覆盖。

## 界面预览

> 📸 _截图将随版本迭代持续更新。_

### QQ群交流
点击链接加入群聊【GPT PLUS交流群aBaiAutoplus】：https://qm.qq.com/q/MfuBG14aI
![gopay注册生成gptplus](assets/screenshots/QQ群交流.jpg)

### gopay注册生成gptplus

![gopay注册生成gptplus](assets/screenshots/gopay注册生成gptplus.png)

### PayPal注册gptplus

![PayPal注册gptplus](assets/screenshots/PayPal注册gptplus.png)

### PayPal注册gptplus

![PayPal注册gptplus](assets/screenshots/PayPal注册gptplus2.png)

### 设置

![设置](assets/screenshots/设置2.png)
![设置](assets/screenshots/设置.png)

## 技术栈

| 层级         | 技术                                    |
| ------------ | --------------------------------------- |
| 后端         | FastAPI + SQLite（SQLModel）            |
| 前端         | React + TypeScript + Vite + TailwindCSS |
| HTTP         | curl_cffi / tls_client（浏览器指纹伪装） |
| 浏览器自动化 | Playwright / Camoufox / BitBrowser      |
| 桌面端       | Electron（内置后端 + 前端）             |

## 快速开始

### 环境要求

- Python 3.11+
- Node.js 18+

### 安装

#### macOS / Linux

```bash
# 克隆项目
git clone https://github.com/asz798838958/aBaiAutoplus.git
cd aBaiAutoplus

# 创建虚拟环境
python3 -m venv .venv
source .venv/bin/activate

# 安装后端依赖
pip install -r requirements.txt

# 构建前端
cd frontend
npm install
npm run build
cd ..
```

#### Windows

```bat
:: 克隆项目
git clone https://github.com/asz798838958/aBaiAutoplus.git
cd aBaiAutoplus

:: 创建虚拟环境
python -m venv .venv
.venv\Scripts\activate

:: 安装后端依赖
pip install -r requirements.txt

:: 构建前端
cd frontend
npm install
npm run build
cd ..
```

### 安装浏览器（可选，无头/有头浏览器模式需要）

```bash
# Playwright 浏览器
python3 -m playwright install chromium

# Camoufox（用于本地 Turnstile Solver）
python3 -m camoufox fetch
```

### 配置环境变量（可选）

复制示例文件并按需填写：

```bash
cp .env.example .env
```

所有第三方 API key（接码、验证码、代理）均通过环境变量或 Web UI 配置，**仓库内不包含任何真实密钥**。

### 启动

#### macOS / Linux

```bash
.venv/bin/python3 -m uvicorn main:app --port 8000
```

#### Windows

```bat
.venv\Scripts\python -m uvicorn main:app --port 8000
```

浏览器访问 `http://localhost:8000`

说明：

- 启动入口统一为 `main:app`
- 后端接口统一位于 `/api/*`
- 生产模式下前端构建产物由后端直接托管，访问 `http://localhost:8000` 即可
- 开发模式下前端独立运行在 `http://localhost:5173`，通过 Vite 代理转发 API 请求
- C 端 / 管理端独立 API 项目见 [customer_portal_api/README.md](customer_portal_api/README.md)

### 开发模式（前端热更新）

```bash
cd frontend
npm run dev
# 访问 http://localhost:5173
```

## 桌面版下载

> 🚀 **零配置一键启动**：不想折腾 Python 和 Node.js？直接下载桌面客户端，双击即可使用。

| 平台                              | 下载                                                                                |
| --------------------------------- | ----------------------------------------------------------------------------------- |
| 🍎 macOS（Intel / Apple Silicon） | [前往 Releases 下载 `.dmg`](https://github.com/asz798838958/aBaiAutoplus/releases/latest) |
| 🪟 Windows                        | [前往 Releases 下载 `.exe`](https://github.com/asz798838958/aBaiAutoplus/releases/latest) |

桌面客户端基于 Electron 打包，内置完整的 Python 后端 + React 前端，开箱即用。每次发布新版本（`v*` tag）会自动构建并发布到 Releases。

如需源码运行或自行打包，参考上方 [快速开始](#快速开始) 与 `electron/` 目录。

## Docker 部署

### 从源码构建

```bash
git clone https://github.com/asz798838958/aBaiAutoplus.git
cd aBaiAutoplus
docker compose up -d --build
```

`docker-compose.yml` 示例：

```yaml
services:
  app:
    build: .
    ports:
      - "8000:8000"   # FastAPI / Web UI
      - "6080:6080"   # noVNC (headed 浏览器预览)
      - "8889:8889"   # Turnstile Solver
    environment:
      - DISPLAY=:99
      - ACCOUNT_MANAGER_DATABASE_URL=sqlite:////app/data/account_manager.db
      # 可选：设置访问密码，不设置则无密码保护
      # - APP_PASSWORD=changeme
      # 可选：设置 VNC 密码
      # - VNC_PASSWORD=changeme
    volumes:
      - ./data:/app/data   # 持久化 SQLite 数据库
    restart: unless-stopped
```

### 访问地址

| 服务   | 地址                             | 说明                        |
| ------ | -------------------------------- | --------------------------- |
| Web UI | `http://localhost:8000`          | 主界面                      |
| noVNC  | `http://localhost:6080/vnc.html` | 可视化浏览器（headed 模式） |
| Solver | `http://localhost:8889`          | Turnstile 验证码求解器      |

> 云服务器部署时，请确保安全组/防火墙放行 8000、6080、8889 端口；公网部署务必设置 `APP_PASSWORD` 访问密码。

### 常用命令

```bash
docker compose logs -f      # 查看日志
docker compose restart      # 重启
docker compose down         # 停止
```

## GoPay 付款 ChatGPT Plus

这是本项目相对上游的核心扩展功能：用印尼 GoPay 协议化付款，自动完成 ChatGPT Plus 订阅。

### 流水线

整条链路分三步（实现见 `application/gopay_pay_chatgpt.py`）：

1. **协议** — 调用 `generate_plus_link(country=ID, currency=IDR)` 拿到 ChatGPT 的 `cashier_url`（Stripe hosted checkout）
2. **浏览器** — 打开 `cashier_url`，等页面跳转到 Midtrans 收银台域，抓取 `midtrans_url`
3. **协议** — 用 GoPay 账号调用 `GoPayPayment.pay(midtrans_url, account)` 完成 14 步 Midtrans API 付款

付款成功后对应 ChatGPT 账号会被标记为 `subscribed`。

### 使用方式

在 Web UI 的「GoPay 付款 Plus」页面操作，或通过 API：

- `POST /api/tasks/gopay-pay-chatgpt` — 创建付款任务（任务类型 `gopay_pay_chatgpt`）

主要参数：

| 参数                   | 说明                                                              |
| ---------------------- | ----------------------------------------------------------------- |
| `chatgpt_account_ids`  | 要付款的 ChatGPT 账号 id 列表；留空且填了 `register_count` 时先注册 |
| `register_count`       | 未选账号时，先注册 N 个 ChatGPT 账号再付款                         |
| `gopay_account_id`     | 指定 GoPay 付款账号；留空按 `gopay_source` 策略自动选/注册         |
| `gopay_source`         | `auto`（先用池后注册）/ `pool`（只用池）/ `register`（强制注册）  |
| `sms_provider`         | GoPay 注册接码渠道：`herosms` / `smspool` / `smsbower`            |
| `country` / `currency` | 默认 `ID` / `IDR`                                                 |
| `checkout_mode`        | 浏览器后端：`camoufox` / `bitbrowser_*`                          |
| `envelope_url`         | 可选，付款前先领红包补 GoPay 余额                                 |
| `concurrency`          | 多账号并发数                                                      |

> GoPay 账号注册与付款依赖印尼手机号接码（HeroSMS / SMSPool / SMSBower），请先在「全局配置」配置对应渠道的 API key。GoPay PIN 默认值可通过任务参数 `gopay_pin` 覆盖。

## 邮箱服务配置

注册时需要选择一种邮箱服务用于接收验证码。邮箱、验证码和接码配置都由后端 provider catalog 驱动，前端「全局配置」页采用列表式 CRUD：左侧显示已添加的 provider，右侧统一编辑名称、认证方式和字段；「新增 Provider」下拉框只展示后端已接入但尚未加入的 provider。

### MoeMail（推荐）

基于开源项目 [cloudflare_temp_email](https://github.com/dreamhunter2333/cloudflare_temp_email) 自建的临时邮箱服务，无需配置任何参数，系统自动注册临时账号并生成邮箱。在注册页选择 **MoeMail**，填写你部署的实例地址（默认使用公共实例）。

### Laoudo

使用固定的自有域名邮箱，稳定性最高，适合长期使用。

| 参数       | 说明                                         |
| ---------- | -------------------------------------------- |
| 邮箱地址   | 完整邮箱地址，如 `user@example.com`          |
| Account ID | 邮箱账号 ID（在 Laoudo 面板查看）            |
| JWT Token  | 登录后从浏览器 Cookie 或接口获取的认证 Token |

### Cloudflare Worker 自建邮箱

基于 [cloudflare_temp_email](https://github.com/dreamhunter2333/cloudflare_temp_email) 自行部署的邮箱服务，完全自主可控。

| 参数        | 说明                                                                  |
| ----------- | --------------------------------------------------------------------- |
| API URL     | Worker 的后端 API 地址，如 `https://api.your-domain.com`              |
| Admin Token | 管理员密码，在 Worker 环境变量 `ADMIN_PASSWORDS` 中配置               |
| 域名        | 收件邮箱的域名，如 `your-domain.com`（需配置 MX 记录指向 Cloudflare） |
| Fingerprint | 可选，Worker 开启 fingerprint 验证时填写                              |

### Freemail

基于 Cloudflare Worker 自建的邮箱服务，支持管理员令牌和用户名密码两种认证方式。

| 参数       | 说明                 |
| ---------- | -------------------- |
| API URL    | Freemail 服务地址    |
| 管理员令牌 | 管理员认证令牌       |
| 用户名     | 可选，用户名密码认证 |
| 密码       | 可选，用户名密码认证 |

### Testmail

`testmail.app` 的 namespace 邮箱模式，自动生成 `{namespace}.{随机tag}@inbox.testmail.app`，适合并发任务。

| 参数       | 说明                                     |
| ---------- | ---------------------------------------- |
| API URL    | 默认 `https://api.testmail.app/api/json` |
| Namespace  | 你的 namespace，例如 `3xw8n`             |
| Tag Prefix | 可选，给随机 tag 增加前缀                |
| API Key    | testmail.app 控制台里的 API Key          |

### 其他公共邮箱

- **DuckMail / TempMail.lol / Temp-Mail Web**：公共临时邮箱，无需配置，部分地区需代理
- **DuckDuckGo Email**：生成 `@duck.com` 私密别名，需在全局配置填写转发邮箱的 IMAP 信息

## 验证码服务配置

| 服务        | 说明                                                                    |
| ----------- | ----------------------------------------------------------------------- |
| YesCaptcha  | 需填写 Client Key，在 [yescaptcha.com](https://yescaptcha.com) 注册获取 |
| 2Captcha    | 需填写 API Key，在 [2captcha.com](https://2captcha.com) 注册获取        |
| 本地 Solver | 使用 Camoufox 本地解码，需先执行 `python3 -m camoufox fetch`            |

## 代理池配置

### 静态代理

在代理管理页手动添加固定代理地址，系统按成功率加权轮询。连续失败 5 次的代理自动禁用。

### 动态代理驱动

如果数据库中已配置并启用 `proxy` provider，注册时会优先尝试动态代理，失败或未配置时自动回退到静态代理池。

| Provider     | 说明                                                                              |
| ------------ | --------------------------------------------------------------------------------- |
| API 提取代理 | 通过 HTTP API 动态提取代理 IP，适用于大多数代理商的 API 提取接口                  |
| 旋转网关代理 | 固定入口地址，每次请求自动分配不同出口 IP，适用于 BrightData、Oxylabs、IPRoyal 等 |

## 接码服务配置

部分平台注册需要手机号验证（如 Cursor、GoPay），可配置接码服务自动完成：

| 服务         | 说明                                                            |
| ------------ | --------------------------------------------------------------- |
| SMS-Activate | 需填写 API Key，可配置默认国家                                  |
| HeroSMS      | 需填写 API Key，可配置服务代码、国家 ID、最高单价、号码复用策略 |
| SMSPool      | 需填写 API Key，可配置国家 / 服务 ID / 价格上限                 |
| SMSBower     | 需填写 API Key，可配置服务代码、国家 ID                         |

添加方法：在 Web UI「全局配置 → 接码服务」点击「新增接码 Provider」，选择对应服务，填写 API Key 并按需设为默认。注册任务会优先使用任务参数里的 `sms_provider`，未指定时使用默认接码 Provider。

> 🔐 接码 API key 通过环境变量（如 `OPAI_SMSPOOL_API_KEY`、`OPAI_SMSBOWER_API_KEY`）或 Web UI 配置，仓库内不含任何真实密钥。

## 账号生命周期管理

系统内置后台生命周期管理器，自动执行：

- **有效性检测**：每 6 小时检测活跃账号是否仍有效，失效标记为 invalid
- **Token 自动续期**：每 12 小时刷新即将过期的 token（当前支持 ChatGPT）
- **Trial 过期预警**：扫描 trial 账号，即将过期的标记预警，已过期的自动更新状态

手动触发 API：

- `POST /api/lifecycle/check` — 有效性检测
- `POST /api/lifecycle/refresh` — token 刷新
- `POST /api/lifecycle/warn` — 过期预警
- `GET /api/lifecycle/status` — 查看管理器状态

## 注册成功率仪表盘

- `GET /api/stats/overview` — 全局概览（总注册数、成功率、状态分布）
- `GET /api/stats/by-platform` — 按平台统计成功率
- `GET /api/stats/by-day?days=30` — 按天注册趋势
- `GET /api/stats/by-proxy` — 代理成功率排行
- `GET /api/stats/errors?days=7` — 失败错误聚合

## Any2API 联动

配合 [Any2API](https://github.com/lxf746/any2api) 项目使用，注册完成后自动推送账号到网关，实现注册即可用。

在全局配置中设置 `any2api_url`（如 `http://localhost:8099`）和 `any2api_password` 后，每次注册成功会自动推送：

| 平台     | 推送目标                  |
| -------- | ------------------------- |
| Kiro     | `kiroAccounts` 账号池     |
| Grok     | `grokTokens` token 池     |
| Cursor   | `cursorConfig` cookie     |
| ChatGPT  | `chatgptConfig` token     |
| Blink    | `blinkConfig` 凭证        |
| Windsurf | `windsurfAccounts` 账号池 |

未配置 `any2api_url` 时此功能静默跳过。也可手动导出：

- `POST /api/accounts/export/any2api` — 导出为 Any2API admin.json 格式
- `POST /api/accounts/export/kiro-go` — 导出为 Kiro-Go config.json 格式

## 项目结构

```
.
├── main.py                 # FastAPI 入口
├── Dockerfile              # Docker 构建
├── docker-compose.yml      # Docker Compose 编排
├── requirements.txt        # Python 依赖
├── api/                    # HTTP 路由层（账号 / 任务 / 配置 / 代理 / 统计 …）
├── application/            # 应用服务层
│   ├── gopay_pay_chatgpt.py    # GoPay 付款 ChatGPT Plus 编排器（本项目扩展）
│   ├── tasks.py / task_commands.py  # 任务编排与执行
│   └── ...
├── domain/                 # 领域模型
├── infrastructure/         # 仓储与运行时适配
├── core/                   # 基础能力
│   ├── base_platform.py    # 平台基类
│   ├── base_mailbox.py     # 邮箱服务基类
│   ├── base_captcha.py     # 验证码服务基类
│   ├── base_sms.py         # 接码服务基类
│   ├── registration/       # 注册流程编排（适配器 + 流程）
│   ├── lifecycle.py        # 账号生命周期管理
│   ├── proxy_pool.py       # 代理池（静态 + 动态）
│   ├── registry.py         # 平台插件注册表
│   └── any2api_sync.py     # Any2API 自动推送
├── platforms/              # 平台插件层
│   ├── chatgpt/            # ChatGPT（注册 / Plus 支付 / PayPal 结账）
│   ├── gopay/              # GoPay 注册 + 接码渠道（本项目扩展）
│   ├── gopay-deploy/       # GoPay 协议付款核心（Gojek / Midtrans）
│   └── {platform}/         # 其他平台插件
├── providers/              # Provider 插件层（mailbox / captcha / sms / proxy）
├── services/               # 后台服务（Solver 进程管理 / 任务执行器）
├── customer_portal_api/    # C 端 / 管理端独立 API
├── electron/               # Electron 桌面端打包
├── tests/                  # 测试
└── frontend/               # React 前端
```

## 插件开发

添加新平台需要以下步骤：

### 1. 新建平台目录

在 `platforms/` 下新建目录，必须包含 `__init__.py` 和 `plugin.py`（`pkgutil.iter_modules` 只扫描带 `__init__.py` 的 Python 包）：

```
platforms/myplatform/
├── __init__.py
├── plugin.py              # 平台适配层（必须）
├── protocol_mailbox.py    # 协议模式注册逻辑（按需）
├── browser_register.py    # 浏览器注册逻辑（按需）
└── browser_oauth.py       # 浏览器 OAuth 逻辑（按需）
```

### 2. 实现 plugin.py

```python
from core.base_platform import BasePlatform, Account, AccountStatus, RegisterConfig
from core.base_mailbox import BaseMailbox
from core.registration import ProtocolMailboxAdapter, OtpSpec, RegistrationResult
from core.registry import register


@register
class MyPlatform(BasePlatform):
    name = "myplatform"
    display_name = "My Platform"
    version = "1.0.0"

    def __init__(self, config: RegisterConfig = None, mailbox: BaseMailbox = None):
        super().__init__(config)
        self.mailbox = mailbox

    def build_protocol_mailbox_adapter(self):
        """协议模式注册适配器"""
        return ProtocolMailboxAdapter(
            result_mapper=lambda ctx, result: RegistrationResult(
                email=result["email"],
                password=result.get("password", ""),
                status=AccountStatus.REGISTERED,
            ),
            worker_builder=lambda ctx, artifacts: __import__(
                "platforms.myplatform.protocol_mailbox",
                fromlist=["MyWorker"],
            ).MyWorker(proxy=ctx.proxy, log_fn=ctx.log),
            register_runner=lambda worker, ctx, artifacts: worker.run(
                email=ctx.identity.email,
                password=ctx.password,
                otp_callback=artifacts.otp_callback,
            ),
            otp_spec=OtpSpec(wait_message="等待验证码邮件..."),
        )

    def check_valid(self, account: Account) -> bool:
        return bool(account.token)
```

### 3. 声明平台能力

平台能力优先使用插件类属性声明，也可以在 Web UI 的「平台能力」页面覆盖：

```python
class MyPlatform(BasePlatform):
    supported_executors = ["protocol"]
    supported_identity_modes = ["mailbox"]
    supported_oauth_providers = []
    capabilities = []
```

系统启动时会自动扫描 `platforms/` 目录加载所有带 `@register` 装饰器的插件。

## 安全说明

本项目处理账号凭证、token 和第三方 API key，请遵循以下安全实践：

- **不要提交真实凭证**：账号导出文件（`acc*.json`）、数据库（`*.db`）、抓包/调试 dump（`*_inspect.txt`、`otp_*.txt`、`*.har`）均应在 `.gitignore` 中忽略，请勿强制提交。
- **密钥走环境变量**：所有接码 / 验证码 / 代理的 API key 通过环境变量或 Web UI 配置，不要写死进源码。参考 [.env.example](.env.example)。
- **公网部署加固**：Docker 部署务必设置 `APP_PASSWORD`；`customer_portal_api` 生产环境必须修改默认 `PORTAL_JWT_SECRET` 与管理员密码，并收敛 `PORTAL_CORS_ORIGINS`。
- **凭证轮换**：若怀疑凭证泄露，第一时间在对应平台后台吊销 / 重置。

## 常见问题

### 验证码失败怎么办？

1. 确认验证码 provider 已正确配置（YesCaptcha Client Key 或本地 Solver）
2. 协议模式下优先使用远程验证码服务（YesCaptcha / 2Captcha）
3. 浏览器模式下 Camoufox 会自动尝试点击 Turnstile checkbox，失败时回退到远程 Solver
4. 持续失败时检查代理 IP 质量——高风险 IP 会触发更严格的验证

### 代理被封 / 注册失败率高？

1. 在代理管理页查看各代理的成功率，禁用低成功率代理
2. 使用住宅代理而非数据中心代理，通过率显著更高
3. 降低并发数，避免同一 IP 短时间内大量请求
4. 不同平台对 IP 的敏感度不同，可按平台分配代理池

### 浏览器模式需要什么额外配置？

```bash
python3 -m playwright install chromium   # Playwright 浏览器
python3 -m camoufox fetch                # Camoufox（反指纹浏览器）
```

浏览器模式支持 `headless`（无头）和 `headed`（有头）两种，在注册页的执行器选项中选择。

### 用 BitBrowser（比特浏览器）替代 Camoufox

ChatGPT 注册 / 生成支付链接 / PayPal 自动结账全程都支持把浏览器后端从 Camoufox 切换到 [BitBrowser](https://www.bitbrowser.cn/)。其 profile 持久化（cookie / localStorage / 浏览历史）能让风险评分更友好。

**前提**：本机安装并启动 BitBrowser 客户端（默认 API 端口 `127.0.0.1:54345`），在 GUI 里手工创建 profile 并记录 profile ID。

**使用**：注册任务页或「生成支付链接」表单选择执行器 `bitbrowser_headed` / `bitbrowser_hidden` / `bitbrowser_headless`，填 `bit_profile_id`。

| 模式                  | 行为                                 | 反爬通过率              |
| --------------------- | ------------------------------------ | ----------------------- |
| `bitbrowser_headed`   | 显示真实窗口（最像人）               | 高                      |
| `bitbrowser_hidden`   | 窗口移到屏幕外但仍真实渲染（占 GPU） | 高（推荐 PayPal）       |
| `bitbrowser_headless` | 真 `--headless=new`（性能最好）      | 中（hCaptcha 容易识别） |

环境变量：`BIT_PROFILE_ID`（默认 profile）、`BIT_API_URL`（默认 `http://127.0.0.1:54345`）、`BIT_API_TOKEN`（企业版需要，社区版留空）。

### Solver 启动超时怎么办？

`[Solver] 启动超时` 表示本地 Turnstile Solver 在 30 秒内没通过健康检查，主服务仍会继续启动。

1. 本地先执行 `python3 -m camoufox fetch`，再在「全局配置」页点击「重启 Solver」
2. 不依赖本地 Solver 时，配置 YesCaptcha 或 2Captcha，注册任务里选远程验证码服务
3. 检查 8889 端口是否被占用

### ARM 镜像构建失败怎么办？

若日志出现 `src/pages/*.tsx ... TS6133/TS7006`，实际失败点是前端 TypeScript 构建。先本地 `cd frontend && npm run build` 确认通过，再 `docker compose build --no-cache`。

## 参与贡献

欢迎提交 Issue 和 Pull Request。

1. Fork 本仓库
2. 创建特性分支：`git checkout -b feature/my-feature`
3. 提交更改：`git commit -m 'feat: add my feature'`
4. 推送分支：`git push origin feature/my-feature`
5. 提交 Pull Request

提交规范建议使用 [Conventional Commits](https://www.conventionalcommits.org/)：`feat:` / `fix:` / `docs:` / `refactor:` / `test:`。详见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## License

本项目采用 [AGPL-3.0](LICENSE) 许可证。个人学习和研究可自由使用；商业使用需遵守 AGPL-3.0 条款（衍生作品须开源）。

本项目基于 [`lxf746/any-auto-register`](https://github.com/lxf746/any-auto-register)（同样为 AGPL-3.0）二次开发，衍生代码遵循相同许可证。
