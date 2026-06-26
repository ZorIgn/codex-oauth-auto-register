from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from application.task_commands import TaskCommandsService
from application.tasks_query import TasksQueryService

router = APIRouter(prefix="/tasks", tags=["task-commands"])
command_service = TaskCommandsService()
query_service = TasksQueryService()


class RegisterTaskRequest(BaseModel):
    platform: str
    email: Optional[str] = None
    password: Optional[str] = None
    count: int = 1
    concurrency: int = 1
    proxy: Optional[str] = None
    executor_type: str = "protocol"
    captcha_solver: str = "auto"
    extra: dict = Field(default_factory=dict)


class PhoneBindTaskRequest(BaseModel):
    platform: str = "chatgpt"
    ids: list[int] = Field(default_factory=list)
    fallback_ids: list[int] = Field(default_factory=list)
    phone_lines: str
    browser_mode: str = "camoufox_headed"
    bit_profile_id: str = ""
    proxy: str = ""
    concurrency: int = 1


class CodexOAuthTaskRequest(BaseModel):
    platform: str = "chatgpt"
    account_id: int = 0
    ids: list[int] = Field(default_factory=list)
    browser_mode: str = "camoufox_headed"
    bit_profile_id: str = ""
    proxy: str = ""
    concurrency: int = 1


class GoPayPayChatGptTaskRequest(BaseModel):
    """GoPay 协议付款 ChatGPT Plus。

    chatgpt_account_ids: 必填，要付款的 ChatGPT 账号 id 列表（串行处理）
    gopay_account_id: 可选，指定 GoPay 号；为空则自动从池里挑余额 ≥ 1 的
    cashier_url_override: 可选，跳过 generate_plus_link 协议步骤
    midtrans_url_override: 可选，跳过浏览器抓 URL 步骤（直接用这个）
    country/currency: 默认 ID/IDR
    headless: 浏览器无头（建议 false 让用户看见进度）
    grab_timeout: 浏览器等用户跳到 Midtrans 的最大秒数
    herosms_api_key: Hero-SMS 接码平台 API key，付款 OTP 用；不传则回退环境变量 OPAI_HEROSMS_API_KEY
    """

    chatgpt_account_ids: list[int] = Field(default_factory=list)
    gopay_account_id: int = 0
    cashier_url_override: str = ""
    midtrans_url_override: str = ""
    country: str = "ID"
    currency: str = "IDR"
    headless: bool = False
    checkout_mode: str = "camoufox_headed"
    bit_profile_id: str = ""
    envelope_url: str = ""
    concurrency: int = 1
    register_count: int = 0
    register_extra: dict = Field(default_factory=dict)
    proxy: Optional[str] = None
    grab_timeout: int = 300
    phone_ttl_seconds: int = 1200
    auto_register_gopay: bool = True
    gopay_pin: str = "000000"
    sms_provider: str = "herosms"
    smspool_api_key: str = ""
    smsbower_api_key: str = ""
    herosms_api_key: str = ""
    # 拿号价格上限（USD），herosms 与 smspool 共用。空串走插件默认（0.11）。
    max_price: str = ""
    # GoPay 号来源开关：auto（先池后注册）/ pool（只用号池，没号失败）/
    # register（强制现注册新号，忽略号池/指定号）。
    gopay_source: str = "auto"


@router.post("/register")
def create_register_task(body: RegisterTaskRequest):
    return command_service.create_register_task(body.model_dump())


@router.post("/phone-bind")
def create_phone_bind_task(body: PhoneBindTaskRequest):
    return command_service.create_phone_bind_task(body.model_dump())


@router.post("/codex-oauth")
def create_codex_oauth_task(body: CodexOAuthTaskRequest):
    return command_service.create_codex_oauth_task(body.model_dump())


@router.post("/gopay-pay-chatgpt")
def create_gopay_pay_chatgpt_task(body: GoPayPayChatGptTaskRequest):
    return command_service.create_gopay_pay_chatgpt_task(body.model_dump())


@router.post("/{task_id}/cancel")
def cancel_task(task_id: str):
    task = command_service.cancel_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    return task


@router.get("/{task_id}/logs/stream")
async def stream_logs(task_id: str, since: int = 0):
    if not query_service.get_task(task_id):
        raise HTTPException(404, "任务不存在")
    return StreamingResponse(
        command_service.stream_task_events(task_id, since=since),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
