from __future__ import annotations

import re
import unicodedata
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

PROTOCOL_VERSION = 2
ALLOWED_ACTIONS = frozenset({"quota.query", "codex.refresh"})
MAX_CLIENT_NAME_LEN = 32
MAX_ACCOUNTS = 200
MAX_WINDOWS = 32
CLIENT_NAME_RE = re.compile(r"^[^\s/\\]{1,32}$")

QuotaAction = Literal["quota.query", "codex.refresh"]


def normalize_client_name(value: str) -> str:
    """规范化客户端名称：去首尾空白 + Unicode NFC。"""
    return unicodedata.normalize("NFC", (value or "").strip())


def valid_client_name(value: str) -> bool:
    """客户端名称是否合法：1–32 字符，不以 - 开头，无空白与 / \\，无控制字符。"""
    name = normalize_client_name(value)
    if not name or len(name) > MAX_CLIENT_NAME_LEN:
        return False
    if name.startswith("-"):
        return False
    if any(ch in name for ch in "/\\") or any(ch.isspace() for ch in name):
        return False
    if any(unicodedata.category(ch).startswith("C") for ch in name):
        return False
    return True


class ClientCapabilities(BaseModel):
    model_config = {"extra": "ignore"}

    refresh: bool = False
    channels: list[str] = Field(default_factory=list)


class HelloPayload(BaseModel):
    model_config = {"extra": "ignore"}

    client_name: str = ""
    agent_version: str = ""
    protocol_version: int = PROTOCOL_VERSION
    os: str = ""
    capabilities: ClientCapabilities = Field(default_factory=ClientCapabilities)


class ServerHelloPayload(BaseModel):
    model_config = {"extra": "ignore"}

    server_name: str = "Server"
    server_version: str = "0.3.0"
    protocol_version: int = PROTOCOL_VERSION
    session_id: str = ""


class QuotaQueryPayload(BaseModel):
    model_config = {"extra": "ignore"}

    platform: str | None = None
    account: str | None = None
    fresh: bool = False

    @field_validator("platform", "account", mode="before")
    @classmethod
    def empty_to_none(cls, value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None


class Envelope(BaseModel):
    model_config = {"extra": "ignore"}

    version: int = PROTOCOL_VERSION
    type: Literal["request", "response", "hello", "error"]
    id: str = ""
    action: str | None = None
    payload: dict[str, Any] | None = None
    ok: bool | None = None
    result: dict[str, Any] | None = None
    error: str | None = None

    @field_validator("id", mode="before")
    @classmethod
    def stringify_id(cls, value: Any) -> str:
        return str(value or "")


class QuotaWindowDTO(BaseModel):
    model_config = {"extra": "ignore"}

    id: str = ""
    label: str = ""
    used_percent: float | None = None
    remaining_percent: float | None = None
    remaining: float | None = None
    limit: float | None = None
    reset_label: str = "-"
    reset_at: float | None = None
    reset_note: str = ""
    direction: str = "remaining"


class AccountQuotaDTO(BaseModel):
    model_config = {"extra": "ignore"}

    platform: str
    name: str
    plan: str = ""
    status: str = "unknown"
    error: str = ""
    windows: list[QuotaWindowDTO] = Field(default_factory=list)
    disabled: bool = False
    cooling: bool = False
    client_name: str = ""
    subscription_expires_at: float | None = None
    subscription_expires_label: str = ""
    reset_credits: int | None = None
    plan_badges: list[tuple[str, str]] = Field(default_factory=list)
    subscription_badges: list[tuple[str, str]] = Field(default_factory=list)

    @field_validator("windows")
    @classmethod
    def limit_windows(cls, value: list[QuotaWindowDTO]) -> list[QuotaWindowDTO]:
        return value[:MAX_WINDOWS]


class QuotaQueryResult(BaseModel):
    model_config = {"extra": "ignore"}

    client_name: str
    queried_at: str = ""
    cached: bool = False
    accounts: list[AccountQuotaDTO] = Field(default_factory=list)

    @field_validator("accounts")
    @classmethod
    def limit_accounts(cls, value: list[AccountQuotaDTO]) -> list[AccountQuotaDTO]:
        return value[:MAX_ACCOUNTS]


class CodexRefreshPayload(BaseModel):
    model_config = {"extra": "ignore"}

    account: str

    @field_validator("account", mode="before")
    @classmethod
    def validate_account(cls, value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            raise ValueError("account 不能为空")
        return text


class CodexRefreshResult(BaseModel):
    model_config = {"extra": "ignore"}

    message: str
    remaining_credits: int | None = None
