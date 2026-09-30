"""QuotaNoa 的 JSON 配置：定义、校验与原子写入。

NoneBot 的 `.env` 只保留 ``QUOTANOA_CONFIG_FILE`` 与 ``QUOTANOA_CLIENT_CONFIG_FILE``
（分别指向主配置与客户端配置）；插件其余配置（CPA 实例、火山账号、渲染设置、
别名文件路径）都从主 JSON 读取。

远程客户端（Server 模式）的服务端监听设置与客户端列表**独立**存放于
``data/quotanoa_client.json``（``server`` 段 + 顶层 ``clients``），不再写入主配置。

CPA 支持多个实例：``cpa.instances[]`` 中每一项都是一个独立连接，自带连接与
额度查询设置。``cpa.admins`` / ``cpa.codex_refresh_admin`` 是全局权限名单，
不随实例区分。

本模块只做“纯数据”工作：解析、校验、默认值、原子写入。热重载与快照管理在
``state.py``。
"""

from __future__ import annotations

import copy
import json
import os
import re
import shutil
import time
import unicodedata
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from pydantic import BaseModel, Field, field_validator

from .model import (
    ALL_CHANNELS_TOKEN,
    is_all_channels,
    is_channel_name,
    normalize_channel,
)
from .protocol import normalize_client_name, valid_client_name

DEFAULT_CONFIG_FILE = "data/quotanoa_config.json"
#: 远程客户端（Server 模式）配置：服务端监听段 + 客户端列表独立存放，不再写入主配置。
DEFAULT_CLIENT_CONFIG_FILE = "data/quotanoa_client.json"
DEFAULT_ALIASES_FILE = "data/quotanoa_aliases.json"

#: ``/quotanoa config fix`` 修补前备份旧配置的目录（相对当前工作目录）。
#: 备份文件名形如 ``quotanoa_config_<日期>-<时间>_bak.json``。
DEFAULT_BACKUP_DIR = "data/backup"

DEFAULT_THEME = "default"
DEFAULT_CARDS_PER_ROW = 3
MIN_CARDS_PER_ROW = 1
MAX_CARDS_PER_ROW = 6

DEFAULT_MAX_CARDS_PER_CHANNEL = 40
MIN_MAX_CARDS_PER_CHANNEL = 1
MAX_MAX_CARDS_PER_CHANNEL = 200

DEFAULT_CPA_BASE_URL = "http://127.0.0.1:8317"
#: 渠道级缓存默认时长（秒）。10 分钟：正常查询命中缓存，``--fresh`` 强制刷新。
DEFAULT_REFRESH_CACHE_TTL = 600.0

#: 实例名 / 渠道账号名的通用长度上限。
MAX_NAME_LEN = 32

#: 远程客户端（Server 模式）默认监听设置（写入客户端配置的 ``server`` 段）。
DEFAULT_CLIENT_SERVER_NAME = "Server"
DEFAULT_CLIENT_HOST = "127.0.0.1"
DEFAULT_CLIENT_PORT = 8320
DEFAULT_CLIENT_REQUEST_TIMEOUT = 40.0
DEFAULT_CLIENT_WS_MAX_SIZE = 1_048_576
DEFAULT_CLIENT_MAX_ACCOUNTS = 200


class ConfigError(ValueError):
    """配置解析 / 校验 / 写入失败，消息可直接发给管理员。

    继承 ``ValueError``，便于命令层沿用统一的非法取值处理。
    """


# --------------------------------------------------------------------------- #
# 名称校验（实例名等）
# --------------------------------------------------------------------------- #


def normalize_name(value: str) -> str:
    """规范化名称：去首尾空白 + Unicode NFC。"""
    return unicodedata.normalize("NFC", (value or "").strip())


def valid_name(value: str) -> bool:
    """名称是否合法：1–32 字符，不含空白、``/`` ``\\`` ``:``、控制字符，不以 ``-`` 开头。

    ``:`` 被禁用是因为缓存键与跨实例展示都用它作分隔符。
    """
    name = normalize_name(value)
    if not name or len(name) > MAX_NAME_LEN:
        return False
    if name.startswith("-"):
        return False
    if any(ch in name for ch in "/\\:") or any(ch.isspace() for ch in name):
        return False
    if any(unicodedata.category(ch).startswith("C") for ch in name):
        return False
    return True


class Config(BaseModel):
    """NoneBot 环境配置。

    `.env` 里只保留这两项（主配置路径与客户端配置路径）；其余配置都在
    ``data/quotanoa_config.json`` 与 ``data/quotanoa_client.json``。
    """

    quotanoa_config_file: str = DEFAULT_CONFIG_FILE
    quotanoa_client_config_file: str = DEFAULT_CLIENT_CONFIG_FILE

    @field_validator("quotanoa_config_file")
    @classmethod
    def _normalize_path(cls, value: str) -> str:
        return str(value or "").strip() or DEFAULT_CONFIG_FILE

    @field_validator("quotanoa_client_config_file")
    @classmethod
    def _normalize_client_path(cls, value: str) -> str:
        return str(value or "").strip() or DEFAULT_CLIENT_CONFIG_FILE


# --------------------------------------------------------------------------- #
# 强类型取值助手（容忍人为手写 JSON 的类型偏差）
# --------------------------------------------------------------------------- #


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text or default


def _as_float(value: Any, default: float) -> float:
    if isinstance(value, bool):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _as_str_list(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple, set)):
        return ()
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = _as_str(item)
        if text and text not in seen:
            seen.add(text)
            cleaned.append(text)
    return tuple(cleaned)


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _strip_trailing_slash(value: str) -> str:
    return value.rstrip("/")


# --------------------------------------------------------------------------- #
# 各配置段
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CpaInstance:
    """单个 CLIProxyAPI 实例：独立连接 + 独立额度查询设置。"""

    name: str
    base_url: str = DEFAULT_CPA_BASE_URL
    management_key: str = ""
    timeout: float = 15.0
    oauth_poll_interval: float = 3.0
    oauth_timeout: float = 1800.0
    quota_timeout: float = 25.0
    quota_concurrency: int = 4
    #: 实例级缓存时长（秒）；``0`` 表示跟随 ``refreshcache.default``。
    quota_cache_ttl: float = 0.0
    quota_image: bool = True


@dataclass(frozen=True)
class CpaConfig:
    """CPA 全局设置 + 多个实例。

    ``admins`` / ``codex_refresh_admin`` 是全局权限名单；``instances`` 是连接列表。
    """

    admins: tuple[str, ...] = ()
    codex_refresh_admin: tuple[str, ...] = ()
    instances: tuple[CpaInstance, ...] = ()

    def get(self, name: str) -> CpaInstance | None:
        """按名称取实例（名称已规范化）；不存在返回 None。"""
        wanted = normalize_name(name)
        for instance in self.instances:
            if instance.name == wanted:
                return instance
        return None

    def names(self) -> tuple[str, ...]:
        return tuple(instance.name for instance in self.instances)


@dataclass(frozen=True)
class VolcengineAccount:
    """火山方舟 Coding Plan 查询凭据（控制面 AccessKey）。"""

    name: str
    access_key_id: str
    secret_access_key: str
    region: str = "cn-beijing"


@dataclass(frozen=True)
class VolcengineConfig:
    accounts: tuple[VolcengineAccount, ...] = ()


@dataclass(frozen=True)
class WorkbuddyServer:
    """单个 WorkBuddy2API 网关（单端口同时提供 API 与控制台）。

    - ``base_url`` 形如 ``http://127.0.0.1:7863``。
    - 鉴权为**控制台账号 + 密码**（``username`` / ``password``）：插件先
      ``POST /api/login`` 换取会话 token，再 ``GET /api/config`` 读取网关的
      ``api_key``，最后用它调 ``/v1/quota``。会话自动缓存与过期重登。
    - ``api_key`` 为**可选直连覆盖**：填了就跳过登录，直接用它与 ``/v1/quota``
      通信（适用于已知道网关密钥、或未开控制台鉴权的场景）。
    """

    name: str
    base_url: str
    username: str = ""
    password: str = ""
    api_key: str = ""
    timeout: float = 30.0


@dataclass(frozen=True)
class WorkbuddyConfig:
    servers: tuple[WorkbuddyServer, ...] = ()


@dataclass(frozen=True)
class QoderServer:
    """单个 Qoder2OAPI 代理。

    - ``base_url`` 形如 ``http://127.0.0.1:8000``。
    - ``api_key`` 为代理 API Key（Bearer），来自 data/api_key.txt 或环境变量。
    """

    name: str
    base_url: str = "http://127.0.0.1:8000"
    api_key: str = ""
    timeout: float = 30.0


@dataclass(frozen=True)
class QoderConfig:
    servers: tuple[QoderServer, ...] = ()


@dataclass(frozen=True)
class RenderConfig:
    """额度图渲染设置。theme 保持原始字符串，由渲染层解析为 canonical 名。"""

    theme: str = DEFAULT_THEME
    cards_per_row: int = DEFAULT_CARDS_PER_ROW
    max_cards_per_channel: int = DEFAULT_MAX_CARDS_PER_CHANNEL


@dataclass(frozen=True)
class RefreshCacheConfig:
    """各渠道查询结果缓存时长（秒）。

    ``channels`` 以 canonical 渠道名为键；未列出的渠道用 ``default``。
    ``0`` 表示该渠道不缓存。
    """

    default: float = DEFAULT_REFRESH_CACHE_TTL
    channels: Mapping[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class OnebotV11FeatureConfig:
    """OneBot V11 适配器专属特性开关。

    ``forward_message``：``True`` 时在 OneBot V11 环境下把额度查询的多条结果
    合并为一条转发消息（合并转发）；非 OneBot V11 适配器自动忽略。
    """

    forward_message: bool = False


# --------------------------------------------------------------------------- #
# 远程客户端（Server 模式）
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ClientServerConfig:
    """独立客户端服务端（Server 模式）监听设置。

    来自 ``data/quotanoa_client.json`` 的 ``server`` 段；``enabled`` 支持热切换，
    监听参数（host/port 等）变更需重启生效。
    """

    enabled: bool = False
    server_name: str = DEFAULT_CLIENT_SERVER_NAME
    host: str = DEFAULT_CLIENT_HOST
    port: int = DEFAULT_CLIENT_PORT
    request_timeout: float = DEFAULT_CLIENT_REQUEST_TIMEOUT
    ws_max_size: int = DEFAULT_CLIENT_WS_MAX_SIZE
    max_accounts: int = DEFAULT_CLIENT_MAX_ACCOUNTS

    def ws_url(self) -> str:
        return f"ws://{self.host}:{self.port}/v1/client/ws"


@dataclass(frozen=True)
class ClientInstance:
    """一个已注册的远程客户端。"""

    name: str
    key: str = ""
    allow_refresh: bool = False
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "key": self.key,
            "allow_refresh": self.allow_refresh,
            "note": self.note,
        }


@dataclass(frozen=True)
class ClientRegistry:
    """远程客户端的**内存视图**（持久化在 ``data/quotanoa_client.json`` 的 ``clients``）。"""

    clients: tuple[ClientInstance, ...] = ()

    def get(self, name: str) -> ClientInstance | None:
        """按名称取客户端（名称已规范化）；不存在返回 None。"""
        wanted = normalize_client_name(name)
        for client in self.clients:
            if client.name == wanted:
                return client
        return None

    def names(self) -> tuple[str, ...]:
        return tuple(client.name for client in self.clients)

    def to_dict(self) -> dict[str, Any]:
        return {"clients": [client.to_dict() for client in self.clients]}


@dataclass(frozen=True)
class ClientConfigSnapshot:
    """远程客户端（Server 模式）配置快照。

    持久化在独立文件 ``data/quotanoa_client.json``：``server`` 段为服务端监听设置，
    ``clients`` 为已注册客户端列表。整体替换，不在原地修改。
    """

    server: ClientServerConfig = field(default_factory=ClientServerConfig)
    clients: tuple[ClientInstance, ...] = ()
    raw: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "server": {
                "enabled": self.server.enabled,
                "server_name": self.server.server_name,
                "host": self.server.host,
                "port": self.server.port,
                "request_timeout": self.server.request_timeout,
                "ws_max_size": self.server.ws_max_size,
                "max_accounts": self.server.max_accounts,
            },
            "clients": [client.to_dict() for client in self.clients],
        }


@dataclass(frozen=True)
class ConfigSnapshot:
    """一次性完整配置快照。整体替换，不在原地修改。"""

    cpa: CpaConfig = field(default_factory=CpaConfig)
    volcengine: VolcengineConfig = field(default_factory=VolcengineConfig)
    workbuddy: WorkbuddyConfig = field(default_factory=WorkbuddyConfig)
    qoder: QoderConfig = field(default_factory=QoderConfig)
    render: RenderConfig = field(default_factory=RenderConfig)
    refreshcache: RefreshCacheConfig = field(default_factory=RefreshCacheConfig)
    onebot_v11_feature: OnebotV11FeatureConfig = field(default_factory=OnebotV11FeatureConfig)
    #: 渠道置顶顺序（通用，对所有适配器生效）：命中的渠道按此顺序排在最前，
    #: 未命中当前查询的渠道自动忽略；值已归一到 canonical 渠道名。
    pin_channel: tuple[str, ...] = ()
    #: ``/quotanoa`` 无参默认查询的额外渠道（在本地渠道之外追加）。
    quotanoa_additional_channel: tuple[str, ...] = ()
    #: ``/cpa quota`` 无参默认查询的额外渠道（在全部 CPA 平台之外追加）。
    cpa_additional_channel: tuple[str, ...] = ()
    #: 别名文件路径；默认由本模块的 ``DEFAULT_ALIASES_FILE`` 决定，
    #: JSON 里的 ``aliases_file`` 仅作可选覆盖（旧配置兼容），不再写入生成文件。
    aliases_file: str = DEFAULT_ALIASES_FILE
    #: 原始 JSON 字典（用于读-改-写时保留未知字段）。
    raw: Mapping[str, Any] = field(default_factory=dict)

    def cache_ttl(self, channel: str = "", *, fallback: float | None = None) -> float:
        """渠道级缓存 TTL（秒）。

        优先级：``refreshcache.channels[渠道]`` → ``fallback``（如 CPA 实例级
        ``quota_cache_ttl``，``<= 0`` 视为未设置）→ ``refreshcache.default``。
        """
        canonical = normalize_channel(channel) if channel else ""
        if canonical:
            ttl = self.refreshcache.channels.get(canonical)
            if ttl is not None:
                return ttl
        if fallback is not None and fallback > 0:
            return fallback
        return self.refreshcache.default

    def to_dict(self) -> dict[str, Any]:
        return {
            "cpa": {
                "admins": list(self.cpa.admins),
                "codex_refresh_admin": list(self.cpa.codex_refresh_admin),
                "instances": [
                    {
                        "name": instance.name,
                        "base_url": instance.base_url,
                        "management_key": instance.management_key,
                        "timeout": instance.timeout,
                        "oauth_poll_interval": instance.oauth_poll_interval,
                        "oauth_timeout": instance.oauth_timeout,
                        "quota_timeout": instance.quota_timeout,
                        "quota_concurrency": instance.quota_concurrency,
                        "quota_cache_ttl": instance.quota_cache_ttl,
                        "quota_image": instance.quota_image,
                    }
                    for instance in self.cpa.instances
                ],
            },
            "volcengine": {
                "accounts": [
                    {
                        "name": account.name,
                        "access_key_id": account.access_key_id,
                        "secret_access_key": account.secret_access_key,
                        "region": account.region,
                    }
                    for account in self.volcengine.accounts
                ]
            },
            "workbuddy": {
                "servers": [
                    {
                        "name": server.name,
                        "base_url": server.base_url,
                        "username": server.username,
                        "password": server.password,
                        "api_key": server.api_key,
                        "timeout": server.timeout,
                    }
                    for server in self.workbuddy.servers
                ]
            },
            "qoder": {
                "servers": [
                    {
                        "name": server.name,
                        "base_url": server.base_url,
                        "api_key": server.api_key,
                        "timeout": server.timeout,
                    }
                    for server in self.qoder.servers
                ]
            },
            "refreshcache": {
                "default": self.refreshcache.default,
                "channels": dict(self.refreshcache.channels),
            },
            "render": {
                "theme": self.render.theme,
                "cards_per_row": self.render.cards_per_row,
                "max_cards_per_channel": self.render.max_cards_per_channel,
            },
            "onebot-v11-feature": {
                "forward-message": self.onebot_v11_feature.forward_message,
            },
            "pin-channel": list(self.pin_channel),
            "quotanoa_additional_channel": list(self.quotanoa_additional_channel),
            "cpa_additional_channel": list(self.cpa_additional_channel),
        }


def default_config_dict() -> dict[str, Any]:
    """返回默认配置的原始字典（用于首次生成文件）。"""
    return ConfigSnapshot().to_dict()


def default_client_config_dict() -> dict[str, Any]:
    """返回默认客户端配置的原始字典（用于首次生成 ``quotanoa_client.json``）。"""
    return ClientConfigSnapshot().to_dict()


def _parse_cpa_instance(entry: Any) -> CpaInstance | None:
    data = _as_mapping(entry)
    raw_name = _as_str(data.get("name"))
    name = normalize_name(raw_name)
    if not name:
        return None
    if not valid_name(name):
        raise ConfigError(f"cpa.instances 中实例名称非法：{raw_name}（1–32 字符，不能含空白或 / \\ :）")
    if is_channel_name(name):
        raise ConfigError(
            f"实例名称不能与渠道名称同名：{raw_name}。"
            "实例是代理多平台的网关，请换一个名字（如 Home、Office）。"
        )
    return CpaInstance(
        name=name,
        base_url=_strip_trailing_slash(_as_str(data.get("base_url"), DEFAULT_CPA_BASE_URL)) or DEFAULT_CPA_BASE_URL,
        management_key=_as_str(data.get("management_key")),
        timeout=max(1.0, _as_float(data.get("timeout"), 15.0)),
        oauth_poll_interval=max(1.0, _as_float(data.get("oauth_poll_interval"), 3.0)),
        oauth_timeout=max(1.0, _as_float(data.get("oauth_timeout"), 1800.0)),
        quota_timeout=max(1.0, _as_float(data.get("quota_timeout"), 25.0)),
        quota_concurrency=max(1, _as_int(data.get("quota_concurrency"), 4)),
        quota_cache_ttl=max(0.0, _as_float(data.get("quota_cache_ttl"), 0.0)),
        quota_image=_as_bool(data.get("quota_image"), True),
    )


def _parse_additional_channels(value: Any) -> tuple[str, ...]:
    """解析额外渠道列表：归一到 canonical 渠道名，去重，丢弃未知项。

    若列表里含 ``all``（或 ``*``），直接返回 ``("all",)`` 表示全部渠道
    （等价于命令的 ``/quotanoa all``），不再展开其它条目。
    """
    items = _as_str_list(value)
    if any(is_all_channels(item) for item in items):
        return (ALL_CHANNELS_TOKEN,)
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in items:
        canonical = normalize_channel(item)
        if not canonical or canonical in seen:
            continue
        seen.add(canonical)
        cleaned.append(canonical)
    return tuple(cleaned)


def _parse_cpa(raw: Any) -> CpaConfig:
    data = _as_mapping(raw)
    raw_instances = data.get("instances")
    instances: list[CpaInstance] = []
    seen: set[str] = set()
    if isinstance(raw_instances, (list, tuple)):
        for entry in raw_instances:
            instance = _parse_cpa_instance(entry)
            if instance is None:
                continue
            if instance.name in seen:
                raise ConfigError(f"cpa.instances 中实例名称重复：{instance.name}")
            seen.add(instance.name)
            instances.append(instance)
    return CpaConfig(
        admins=_as_str_list(data.get("admins")),
        codex_refresh_admin=_as_str_list(data.get("codex_refresh_admin")),
        instances=tuple(instances),
    )


def _parse_volcengine(raw: Any) -> VolcengineConfig:
    data = _as_mapping(raw)
    raw_accounts = data.get("accounts")
    accounts: list[VolcengineAccount] = []
    seen: set[str] = set()
    if isinstance(raw_accounts, (list, tuple)):
        for item in raw_accounts:
            entry = _as_mapping(item)
            name = _as_str(entry.get("name"))
            if not name or name in seen:
                continue
            seen.add(name)
            accounts.append(
                VolcengineAccount(
                    name=name,
                    access_key_id=_as_str(entry.get("access_key_id")),
                    secret_access_key=_as_str(entry.get("secret_access_key")),
                    region=_as_str(entry.get("region"), "cn-beijing") or "cn-beijing",
                )
            )
    return VolcengineConfig(accounts=tuple(accounts))


def _parse_workbuddy(raw: Any) -> WorkbuddyConfig:
    data = _as_mapping(raw)
    raw_servers = data.get("servers")
    servers: list[WorkbuddyServer] = []
    seen: set[str] = set()
    if isinstance(raw_servers, (list, tuple)):
        for item in raw_servers:
            entry = _as_mapping(item)
            raw_name = _as_str(entry.get("name"))
            name = normalize_name(raw_name)
            base_url = _strip_trailing_slash(_as_str(entry.get("base_url")))
            if not name or not base_url or name in seen:
                continue
            if not valid_name(name):
                raise ConfigError(
                    f"workbuddy.servers 中网关名称非法：{raw_name}（1–{MAX_NAME_LEN} 字符，不能含空白或 / \\ :）"
                )
            if is_channel_name(name):
                raise ConfigError(
                    f"workbuddy.servers 网关名称不能与渠道名称同名：{raw_name}。"
                    "请换一个名字（如 wb-main、wb-backup）。"
                )
            seen.add(name)
            servers.append(
                WorkbuddyServer(
                    name=name,
                    base_url=base_url,
                    username=_as_str(entry.get("username")),
                    password=_as_str(entry.get("password")),
                    api_key=_as_str(entry.get("api_key")),
                    timeout=max(1.0, _as_float(entry.get("timeout"), 30.0)),
                )
            )
    return WorkbuddyConfig(servers=tuple(servers))


def _parse_qoder(raw: Any) -> QoderConfig:
    data = _as_mapping(raw)
    raw_servers = data.get("servers")
    servers: list[QoderServer] = []
    seen: set[str] = set()
    if isinstance(raw_servers, (list, tuple)):
        for item in raw_servers:
            entry = _as_mapping(item)
            raw_name = _as_str(entry.get("name"))
            name = normalize_name(raw_name)
            base_url = _strip_trailing_slash(
                _as_str(entry.get("base_url"), "http://127.0.0.1:8000")
            )
            if not name or not base_url or name in seen:
                continue
            if not valid_name(name):
                raise ConfigError(
                    f"qoder.servers 中代理名称非法：{raw_name}（1–{MAX_NAME_LEN} 字符，不能含空白或 / \\ :）"
                )
            if is_channel_name(name):
                raise ConfigError(
                    f"qoder.servers 代理名称不能与渠道名称同名：{raw_name}。"
                    "请换一个名字（如 qoder-main、qoder-backup）。"
                )
            seen.add(name)
            servers.append(
                QoderServer(
                    name=name,
                    base_url=base_url,
                    api_key=_as_str(entry.get("api_key")),
                    timeout=max(1.0, _as_float(entry.get("timeout"), 30.0)),
                )
            )
    return QoderConfig(servers=tuple(servers))


def _parse_render(raw: Any) -> RenderConfig:
    data = _as_mapping(raw)
    theme = _as_str(data.get("theme"), DEFAULT_THEME) or DEFAULT_THEME
    cards = _as_int(data.get("cards_per_row"), DEFAULT_CARDS_PER_ROW)
    cards = max(MIN_CARDS_PER_ROW, min(MAX_CARDS_PER_ROW, cards))
    max_cards = _as_int(data.get("max_cards_per_channel"), DEFAULT_MAX_CARDS_PER_CHANNEL)
    max_cards = max(MIN_MAX_CARDS_PER_CHANNEL, min(MAX_MAX_CARDS_PER_CHANNEL, max_cards))
    return RenderConfig(theme=theme, cards_per_row=cards, max_cards_per_channel=max_cards)


def _parse_refreshcache(raw: Any) -> RefreshCacheConfig:
    data = _as_mapping(raw)
    default = max(0.0, _as_float(data.get("default"), DEFAULT_REFRESH_CACHE_TTL))
    channels: dict[str, float] = {}
    for key, value in _as_mapping(data.get("channels")).items():
        canonical = normalize_channel(str(key))
        if not canonical:
            continue
        channels[canonical] = max(0.0, _as_float(value, default))
    return RefreshCacheConfig(default=default, channels=channels)


def _parse_onebot_v11_feature(raw: Any) -> OnebotV11FeatureConfig:
    """解析 ``onebot-v11-feature`` 段。

    容忍三种写法：对象 ``{"forward-message": true}``、裸布尔 ``true``（等价于开启
    合并转发）、以及缺失（默认关闭）。``forward_message`` 也接受 ``forward_message``
    下划线写法与宽松真值。
    """
    if raw is None:
        return OnebotV11FeatureConfig()
    if not isinstance(raw, Mapping):
        # 裸值（true/false/"on" 等）：直接作为 forward-message 开关。
        return OnebotV11FeatureConfig(forward_message=_as_bool(raw, False))
    value = raw.get("forward-message")
    if value is None:
        value = raw.get("forward_message")
    return OnebotV11FeatureConfig(forward_message=_as_bool(value, False))


def _parse_pin_channel(value: Any) -> tuple[str, ...]:
    """解析 ``pin-channel``：归一到 canonical 渠道名，按原顺序去重，丢弃未知项。

    这是通用顺序配置（非 ``all`` 语义），故不含 ``quotanoa_additional_channel``
    里对 ``all`` 的特判。
    """
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in _as_str_list(value):
        canonical = normalize_channel(item)
        if not canonical or canonical in seen:
            continue
        seen.add(canonical)
        cleaned.append(canonical)
    return tuple(cleaned)


def snapshot_from_raw(raw: Mapping[str, Any]) -> ConfigSnapshot:
    """把原始 JSON 字典转换为强类型快照。

    注意：远程客户端（Server 模式）的 ``server`` / ``clients`` 已迁到独立文件，
    这里即使存在也只是**忽略**（不再解析），请用 ``client_snapshot_from_raw``。
    """
    if not isinstance(raw, Mapping):
        raise ConfigError("配置根节点必须是 JSON 对象。")
    aliases_file = _as_str(raw.get("aliases_file"), DEFAULT_ALIASES_FILE) or DEFAULT_ALIASES_FILE
    return ConfigSnapshot(
        cpa=_parse_cpa(raw.get("cpa")),
        volcengine=_parse_volcengine(raw.get("volcengine")),
        workbuddy=_parse_workbuddy(raw.get("workbuddy")),
        qoder=_parse_qoder(raw.get("qoder")),
        render=_parse_render(raw.get("render")),
        refreshcache=_parse_refreshcache(raw.get("refreshcache")),
        onebot_v11_feature=_parse_onebot_v11_feature(raw.get("onebot-v11-feature")),
        pin_channel=_parse_pin_channel(raw.get("pin-channel")),
        quotanoa_additional_channel=_parse_additional_channels(
            raw.get("quotanoa_additional_channel")
        ),
        cpa_additional_channel=_parse_additional_channels(raw.get("cpa_additional_channel")),
        aliases_file=aliases_file,
        raw=dict(raw),
    )


def client_snapshot_from_raw(raw: Mapping[str, Any]) -> ClientConfigSnapshot:
    """把客户端配置文件的原始 JSON 字典转换为强类型快照。

    ``server`` 段与顶层 ``clients`` 列表分别解析；名称非法 / 重复会抛 ``ConfigError``。
    """
    if not isinstance(raw, Mapping):
        raise ConfigError("客户端配置根节点必须是 JSON 对象。")
    return ClientConfigSnapshot(
        server=_parse_server(raw.get("server")),
        clients=_parse_clients(raw.get("clients")),
        raw=dict(raw),
    )


# --------------------------------------------------------------------------- #
# 文件读写
# --------------------------------------------------------------------------- #


def read_config_file(path: Path) -> dict[str, Any]:
    """读取并解析 JSON 配置。文件不存在或非法时抛 ConfigError。"""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ConfigError(f"配置文件不存在：{path}") from exc
    except OSError as exc:
        raise ConfigError(f"无法读取配置文件：{exc}") from exc
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ConfigError(f"配置文件不是合法 JSON：{exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError("配置文件根节点必须是 JSON 对象。")
    return data


def atomic_write_json(path: Path, data: Mapping[str, Any]) -> None:
    """以 tmp + replace 方式原子写入 JSON。"""
    temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        temp_path.write_text(payload, encoding="utf-8")
        os.replace(temp_path, path)
    except OSError as exc:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise ConfigError(f"无法写入配置文件：{exc}") from exc


def ensure_config_file(path: Path) -> dict[str, Any]:
    """确保配置文件存在并返回其内容；缺失时生成默认文件。"""
    if path.is_file():
        return read_config_file(path)
    data = default_config_dict()
    atomic_write_json(path, data)
    return data


def ensure_client_config_file(path: Path) -> dict[str, Any]:
    """确保客户端配置文件存在并返回其内容；缺失时生成默认文件。"""
    if path.is_file():
        return read_config_file(path)
    data = default_client_config_dict()
    atomic_write_json(path, data)
    return data


def _parse_server(raw: Any) -> ClientServerConfig:
    """解析 ``server`` 段（远程客户端服务端监听设置）。"""
    data = _as_mapping(raw)
    server_name = normalize_client_name(_as_str(data.get("server_name"), DEFAULT_CLIENT_SERVER_NAME))
    if not valid_client_name(server_name):
        server_name = DEFAULT_CLIENT_SERVER_NAME
    host = _as_str(data.get("host"), DEFAULT_CLIENT_HOST) or DEFAULT_CLIENT_HOST
    port = max(1, min(65535, _as_int(data.get("port"), DEFAULT_CLIENT_PORT)))
    timeout = max(1.0, _as_float(data.get("request_timeout"), DEFAULT_CLIENT_REQUEST_TIMEOUT))
    ws_max_size = max(1024, _as_int(data.get("ws_max_size"), DEFAULT_CLIENT_WS_MAX_SIZE))
    max_accounts = max(1, _as_int(data.get("max_accounts"), DEFAULT_CLIENT_MAX_ACCOUNTS))
    return ClientServerConfig(
        enabled=_as_bool(data.get("enabled"), False),
        server_name=server_name,
        host=host,
        port=port,
        request_timeout=timeout,
        ws_max_size=ws_max_size,
        max_accounts=max_accounts,
    )


def _parse_clients(raw: Any) -> tuple[ClientInstance, ...]:
    """解析顶层 ``clients`` 列表。"""
    clients: list[ClientInstance] = []
    seen: set[str] = set()
    if isinstance(raw, (list, tuple)):
        for item in raw:
            entry = _as_mapping(item)
            raw_name = _as_str(entry.get("name"))
            name = normalize_client_name(raw_name)
            if not name:
                continue
            if not valid_client_name(name):
                raise ConfigError(
                    f"clients 中客户端名称非法：{raw_name}（1–{MAX_NAME_LEN} 字符，不能含空白或 / \\）"
                )
            if name in seen:
                raise ConfigError(f"clients 中客户端名称重复：{name}")
            seen.add(name)
            clients.append(
                ClientInstance(
                    name=name,
                    key=_as_str(entry.get("key")),
                    allow_refresh=_as_bool(entry.get("allow_refresh"), False),
                    note=_as_str(entry.get("note")),
                )
            )
    return tuple(clients)


def deep_merge(base: Mapping[str, Any], patch: Mapping[str, Any]) -> dict[str, Any]:
    """递归合并 patch 到 base（返回新 dict，不修改入参）。"""
    merged: dict[str, Any] = dict(base)
    for key, value in patch.items():
        current = merged.get(key)
        if isinstance(current, Mapping) and isinstance(value, Mapping):
            merged[key] = deep_merge(current, value)
        else:
            merged[key] = value
    return merged


# --------------------------------------------------------------------------- #
# 配置修补（/quotanoa config fix：补齐缺失项并备份旧文件）
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RepairResult:
    """``/quotanoa config fix`` 的结果。

    - ``changed``：是否补齐了缺失项（False 表示配置已完整，未写盘）。
    - ``added_keys``：补入的键路径（点号分隔，如 ``onebot-v11-feature.forward-message``）。
    - ``backup_path``：修补前的备份文件路径；未写盘时为 None。
    - ``path``：被修补的配置文件路径；内存模式为 None。
    """

    changed: bool
    added_keys: tuple[str, ...] = ()
    backup_path: Path | None = None
    path: Path | None = None


def default_backup_dir() -> Path:
    """备份目录的绝对路径（常量相对当前工作目录解析）。"""
    return Path(DEFAULT_BACKUP_DIR).expanduser().resolve()


def _missing_defaults(raw: Mapping[str, Any], defaults: Mapping[str, Any]) -> dict[str, Any]:
    """递归收集 ``raw`` 相对 ``defaults`` 缺失的键（返回嵌套结构）。

    只在 ``raw`` 缺少键时补入默认值；已存在的键（无论类型对错）一律保留，
    不做覆盖或纠正。
    """
    missing: dict[str, Any] = {}
    for key, default_value in defaults.items():
        if key not in raw:
            missing[key] = copy.deepcopy(default_value)
            continue
        current = raw[key]
        if isinstance(current, Mapping) and isinstance(default_value, Mapping):
            nested = _missing_defaults(current, default_value)
            if nested:
                missing[key] = nested
    return missing


def _merge_missing(base: Mapping[str, Any], missing: Mapping[str, Any]) -> dict[str, Any]:
    """把 ``missing`` 深层并入 ``base``（返回新 dict，不修改入参）。"""
    merged: dict[str, Any] = dict(base)
    for key, value in missing.items():
        current = merged.get(key)
        if isinstance(current, Mapping) and isinstance(value, Mapping):
            merged[key] = _merge_missing(current, value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def flatten_missing_keys(missing: Mapping[str, Any], prefix: str = "") -> tuple[str, ...]:
    """把嵌套的缺失结构拍平成点号键路径列表（有序）。"""
    keys: list[str] = []
    for key, value in missing.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, Mapping):
            keys.extend(flatten_missing_keys(value, path))
        else:
            keys.append(path)
    return tuple(keys)


def config_missing_defaults(raw: Mapping[str, Any]) -> dict[str, Any]:
    """返回 ``raw`` 相对默认配置缺失的键（嵌套结构；空 dict 表示已完整）。"""
    return _missing_defaults(_as_mapping(raw), default_config_dict())


def repair_config_data(raw: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """补齐 ``raw`` 缺失的默认键，返回 ``(修补后数据, 缺失结构)``。

    缺失结构为空时原样返回 ``raw`` 的浅拷贝。不修改入参。
    """
    missing = config_missing_defaults(raw)
    if not missing:
        return dict(raw), {}
    return _merge_missing(raw, missing), missing


def backup_config_file(path: Path, *, backup_dir: Path) -> Path:
    """把现有配置文件复制到 ``backup_dir``，文件名 ``<名>_<日期>-<时间>_bak.json``。

    同一秒内多次备份自动追加序号避免覆盖。失败抛 ``ConfigError``。
    """
    if not path.is_file():
        raise ConfigError(f"配置文件不存在，无法备份：{path}")
    try:
        backup_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ConfigError(f"无法创建备份目录：{backup_dir}（{exc}）") from exc
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = backup_dir / f"{path.stem}_{stamp}_bak.json"
    counter = 1
    while target.exists():
        target = backup_dir / f"{path.stem}_{stamp}_{counter}_bak.json"
        counter += 1
    try:
        shutil.copy2(path, target)
    except OSError as exc:
        raise ConfigError(f"备份配置失败：{exc}") from exc
    return target


def repair_config_file(path: Path, *, backup_dir: Path | None = None) -> RepairResult:
    """补齐 ``path`` 缺失的配置项：先备份旧文件，再原子写入补全后的内容。

    配置已完整时不做任何写盘，返回 ``changed=False``。修补后若仍无法解析
    （原有错误，如实例重名）则抛 ``ConfigError``，**不写入**，避免破坏现场。
    """
    raw = read_config_file(path)
    merged, missing = repair_config_data(raw)
    if not missing:
        return RepairResult(changed=False, path=path)
    # 补全后先校验；原有语义错误会在此抛出，避免写入半成品。
    snapshot_from_raw(merged)
    target_dir = backup_dir if backup_dir is not None else default_backup_dir()
    backup = backup_config_file(path, backup_dir=target_dir)
    atomic_write_json(path, merged)
    return RepairResult(
        changed=True,
        added_keys=flatten_missing_keys(missing),
        backup_path=backup,
        path=path,
    )


def client_config_missing_defaults(raw: Mapping[str, Any]) -> dict[str, Any]:
    """返回客户端配置相对默认配置缺失的键（嵌套结构；空 dict 表示已完整）。"""
    return _missing_defaults(_as_mapping(raw), default_client_config_dict())


def repair_client_config_data(raw: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """补齐客户端配置缺失的默认键，返回 ``(修补后数据, 缺失结构)``。

    缺失结构为空时原样返回 ``raw`` 的浅拷贝。不修改入参。
    """
    missing = client_config_missing_defaults(raw)
    if not missing:
        return dict(raw), {}
    return _merge_missing(raw, missing), missing


def repair_client_config_file(path: Path, *, backup_dir: Path | None = None) -> RepairResult:
    """补齐客户端配置文件缺失项：先备份旧文件，再原子写入补全后的内容。

    配置已完整时不做任何写盘，返回 ``changed=False``。修补后若仍无法解析
    （如客户端重名）则抛 ``ConfigError``，**不写入**。
    """
    raw = read_config_file(path)
    merged, missing = repair_client_config_data(raw)
    if not missing:
        return RepairResult(changed=False, path=path)
    client_snapshot_from_raw(merged)
    target_dir = backup_dir if backup_dir is not None else default_backup_dir()
    backup = backup_config_file(path, backup_dir=target_dir)
    atomic_write_json(path, merged)
    return RepairResult(
        changed=True,
        added_keys=flatten_missing_keys(missing),
        backup_path=backup,
        path=path,
    )
