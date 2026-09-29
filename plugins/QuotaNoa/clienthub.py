"""远程客户端 Hub：独立 FastAPI 服务端（不与 NoneBot 共享同一 FastAPI 实例）。

- 监听参数来自 `.env` 的 ``QUOTANOA_CLIENT_*``（``ClientServerConfig``）；改动需重启。
- 客户端注册表来自 ``data/quotanoa_client.json``（缺失=空，**不自动生成**）。
- 协议 v2。刷新能力以**客户端本地配置**为准：服务端只做「额外关闭」
  （注册表 ``allow_refresh`` 且会话上报 ``capabilities.refresh``），
  客户端即使收到刷新请求也会在本地再次拒绝。

本模块属根模块，只依赖 ``config`` / ``protocol``；对 fastapi/uvicorn 的导入一律延迟。
"""

import asyncio
import hashlib
import secrets
import unicodedata
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from .config import ClientRegistry, ClientServerConfig
from .protocol import (
    PROTOCOL_VERSION,
    ClientCapabilities,
    CodexRefreshPayload,
    CodexRefreshResult,
    Envelope,
    HelloPayload,
    QuotaQueryPayload,
    QuotaQueryResult,
    ServerHelloPayload,
    normalize_client_name,
    valid_client_name,
)

try:  # pragma: no cover - 运行时环境相关
    from nonebot.log import logger
except Exception:  # pragma: no cover
    import logging

    logger = logging.getLogger("QuotaNoa.clienthub")

#: 服务端版本，随插件版本更新。
SERVER_VERSION = "0.3.0"

#: WebSocket 关闭码（避免在模块级导入 fastapi/starlette）。
_CLOSE_POLICY_VIOLATION = 1008
_CLOSE_GOING_AWAY = 1001


class HubError(Exception):
    """远程客户端查询/刷新失败，消息可直接发给管理员。"""


@dataclass
class PendingRequest:
    action: str
    future: "asyncio.Future[dict[str, Any]]"


@dataclass
class ClientSession:
    name: str
    websocket: Any
    session_id: str
    pending: dict[str, PendingRequest] = field(default_factory=dict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    agent_version: str = ""
    protocol_version: int = PROTOCOL_VERSION
    capabilities: ClientCapabilities | None = None

    @property
    def refresh_supported(self) -> bool:
        return bool(self.capabilities and self.capabilities.refresh)


class Hub:
    """远程客户端会话管理器。"""

    def __init__(self) -> None:
        self._sessions: dict[str, ClientSession] = {}
        self._lock = asyncio.Lock()
        self._server: Any = None
        self._task: "asyncio.Task[None] | None" = None
        self._cfg: ClientServerConfig | None = None
        self._registry: ClientRegistry | None = None

    # ------------------------------------------------------------------ #
    # 生命周期 / 配置
    # ------------------------------------------------------------------ #

    def configure(self, cfg: ClientServerConfig, registry: ClientRegistry) -> None:
        """更新监听设置与注册表（监听参数变化需重启才能生效）。"""
        self._cfg = cfg
        self._registry = registry

    def create_app(self, cfg: ClientServerConfig, registry: ClientRegistry) -> Any:
        from fastapi import FastAPI, WebSocket

        app = FastAPI(
            docs_url=None,
            redoc_url=None,
            openapi_url=None,
            title="QuotaNoa Client Hub",
        )

        @app.get("/health")
        async def health() -> dict[str, bool]:
            return {"ok": True}

        @app.websocket("/v1/client/ws")
        async def client_ws(websocket: WebSocket) -> None:
            await self.handle_socket(websocket, cfg, registry)

        return app

    async def start(self, cfg: ClientServerConfig, registry: ClientRegistry) -> None:
        """启动独立 uvicorn 服务端（``cfg.enabled`` 为假时直接返回）。"""
        if not cfg.enabled:
            return
        if self._task is not None and not self._task.done():
            return
        import uvicorn

        self._cfg = cfg
        self._registry = registry
        app = self.create_app(cfg, registry)
        config = uvicorn.Config(
            app,
            host=cfg.host,
            port=cfg.port,
            log_level="info",
            ws_max_size=max(1024, cfg.ws_max_size),
            ws_max_queue=8,
            ws_ping_interval=20,
            ws_ping_timeout=20,
            timeout_graceful_shutdown=10,
            limit_concurrency=100,
            proxy_headers=False,
            server_header=False,
            access_log=False,
            lifespan="off",
        )
        self._server = uvicorn.Server(config)
        self._task = asyncio.create_task(self._server.serve(), name="quotanoa-client-hub")
        logger.info(f"QuotaNoa 远程客户端服务端监听 {cfg.host}:{cfg.port}")

    async def stop(self) -> None:
        async with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            await _fail_pending(session, "服务端已关闭")
            await _safe_close(session.websocket, _CLOSE_GOING_AWAY)
        if self._server is not None:
            self._server.should_exit = True
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=8)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
            self._task = None
        self._server = None

    # ------------------------------------------------------------------ #
    # 查询 / 刷新
    # ------------------------------------------------------------------ #

    def online_names(self) -> list[str]:
        return sorted(self._sessions)

    def known_names(self, registry: ClientRegistry) -> set[str]:
        names: set[str] = set()
        cfg = self._cfg
        if cfg is not None:
            names.add(normalize_client_name(cfg.server_name))
        names.update(normalize_client_name(name) for name in registry.names())
        names.update(self._sessions)
        names.discard("")
        return names

    def client_status(self, name: str) -> dict[str, Any]:
        wanted = normalize_client_name(name)
        session = self._sessions.get(wanted)
        if session is None:
            return {"name": wanted, "online": False}
        channels = list(session.capabilities.channels) if session.capabilities else []
        return {
            "name": wanted,
            "online": True,
            "agent_version": session.agent_version,
            "protocol_version": session.protocol_version,
            "refresh": session.refresh_supported,
            "channels": channels,
        }

    async def query_quota(
        self,
        name: str,
        *,
        platform: str | None = None,
        account: str | None = None,
        fresh: bool = False,
        timeout: float | None = None,
    ) -> QuotaQueryResult:
        cfg = self._cfg
        payload = QuotaQueryPayload(platform=platform, account=account, fresh=fresh)
        data = await self._request("quota.query", name, payload.model_dump(), timeout=timeout)
        try:
            return QuotaQueryResult.model_validate(data)
        except ValidationError as exc:
            raise HubError(f"客户端 {name} 返回无法解析：{exc}") from exc

    async def refresh_codex(
        self,
        name: str,
        account: str,
        *,
        timeout: float | None = None,
    ) -> CodexRefreshResult:
        instance = self._registry.get(name) if self._registry else None
        if instance is None:
            raise HubError(f"未知客户端：{name}")
        if not instance.allow_refresh:
            raise HubError(f"客户端 {name} 未启用刷新能力。")
        wanted = normalize_client_name(name)
        session = self._sessions.get(wanted)
        if session is None:
            raise HubError(f"客户端 {name} 未连接。")
        if not session.refresh_supported:
            raise HubError(f"客户端 {name} 未上报刷新能力。")
        payload = CodexRefreshPayload(account=account)
        data = await self._request("codex.refresh", wanted, payload.model_dump(), timeout=timeout)
        try:
            return CodexRefreshResult.model_validate(data)
        except ValidationError as exc:
            raise HubError(f"客户端 {name} 返回无法解析：{exc}") from exc

    async def _request(
        self,
        action: str,
        name: str,
        payload: dict[str, Any],
        *,
        timeout: float | None,
    ) -> dict[str, Any]:
        cfg = self._cfg
        wanted = normalize_client_name(name)
        req_id = uuid.uuid4().hex
        future: "asyncio.Future[dict[str, Any]]" = asyncio.get_running_loop().create_future()
        async with self._lock:
            session = self._sessions.get(wanted)
            if session is None:
                raise HubError(f"客户端 {name} 未连接。")
            session.pending[req_id] = PendingRequest(action=action, future=future)
        message = Envelope(
            version=PROTOCOL_VERSION,
            type="request",
            id=req_id,
            action=action,
            payload=payload,
        )
        noun = "刷新" if action == "codex.refresh" else "查询"
        try:
            async with session.lock:
                await session.websocket.send_json(message.model_dump())
                wait = timeout if timeout is not None else (cfg.request_timeout if cfg else 40.0)
                data = await asyncio.wait_for(future, timeout=max(1.0, wait))
            return data
        except asyncio.TimeoutError as exc:
            raise HubError(f"客户端 {name} {noun}超时。") from exc
        except HubError:
            raise
        except Exception as exc:
            raise HubError(f"客户端 {name} {noun}失败：{exc}") from exc
        finally:
            session.pending.pop(req_id, None)

    # ------------------------------------------------------------------ #
    # WebSocket 处理
    # ------------------------------------------------------------------ #

    async def handle_socket(
        self,
        websocket: Any,
        cfg: ClientServerConfig,
        registry: ClientRegistry,
    ) -> None:
        from fastapi import WebSocketDisconnect

        headers = getattr(websocket, "headers", {}) or {}
        name, error = _authenticate(headers, cfg, registry)
        if error or not name:
            await _safe_close(websocket, _CLOSE_POLICY_VIOLATION)
            logger.warning(f"拒绝客户端连接：{error}")
            return
        session = ClientSession(name=name, websocket=websocket, session_id=uuid.uuid4().hex)
        async with self._lock:
            if name in self._sessions:
                await _safe_close(websocket, _CLOSE_POLICY_VIOLATION)
                logger.warning(f"拒绝同名客户端：{name}")
                return
            await websocket.accept()
            self._sessions[name] = session
        logger.info(f"客户端已连接：{name}")
        try:
            hello = Envelope(
                version=PROTOCOL_VERSION,
                type="hello",
                id="",
                payload=ServerHelloPayload(
                    server_name=cfg.server_name,
                    server_version=SERVER_VERSION,
                    session_id=session.session_id,
                ).model_dump(),
            )
            await websocket.send_json(hello.model_dump())
            while True:
                raw = await websocket.receive_json()
                if not await self._on_message(session, raw):
                    break
        except WebSocketDisconnect:
            pass
        except Exception as exc:  # noqa: BLE001 - 单会话异常不影响其它客户端
            logger.warning(f"客户端 {name} 连接异常：{exc}")
        finally:
            await self._drop(session, "客户端已断开")

    async def _on_message(self, session: ClientSession, raw: Any) -> bool:
        """处理一条消息；返回 False 表示调用方应停止该会话的接收循环。"""
        try:
            envelope = Envelope.model_validate(raw)
        except ValidationError:
            return True
        if envelope.version != PROTOCOL_VERSION:
            return True
        if envelope.type == "hello":
            if not self._record_hello(session, envelope):
                logger.warning(f"客户端 {session.name} 协议版本不兼容，关闭连接。")
                await _safe_close(session.websocket, _CLOSE_POLICY_VIOLATION)
                return False
            return True
        if envelope.type != "response" or not envelope.id:
            return True
        pending = session.pending.get(envelope.id)
        if pending is None or pending.future.done():
            return True
        if envelope.ok is False:
            pending.future.set_exception(HubError(envelope.error or "客户端返回失败"))
            return True
        result = envelope.result or {}
        if pending.action == "codex.refresh":
            pending.future.set_result(dict(result))
            return True
        # quota.query：校验身份并清理敏感字段。
        try:
            parsed = QuotaQueryResult.model_validate(result)
        except ValidationError as exc:
            pending.future.set_exception(HubError(f"客户端返回无法解析：{exc}"))
            return True
        if parsed.client_name and parsed.client_name != session.name:
            pending.future.set_exception(HubError("客户端返回的名称与连接身份不一致。"))
            return True
        parsed.client_name = session.name
        if self._cfg is not None and len(parsed.accounts) > self._cfg.max_accounts:
            parsed.accounts = parsed.accounts[: self._cfg.max_accounts]
        pending.future.set_result(parsed.model_dump())
        return True

    def _record_hello(self, session: ClientSession, envelope: Envelope) -> bool:
        """记录客户端能力。返回协议版本是否兼容（不兼容则调用方断开连接）。"""
        try:
            hello = HelloPayload.model_validate(envelope.payload or {})
        except ValidationError:
            return True
        if hello.client_name and normalize_client_name(hello.client_name) != session.name:
            logger.warning(
                f"客户端 {session.name} 上报名称与连接身份不一致：{hello.client_name}"
            )
        session.agent_version = _clean_text(hello.agent_version, 64)
        session.protocol_version = hello.protocol_version
        session.capabilities = ClientCapabilities(
            refresh=bool(hello.capabilities.refresh),
            channels=[_clean_text(item, 32) for item in hello.capabilities.channels[:32]],
        )
        logger.info(
            f"客户端 {session.name} 握手：版本={session.agent_version or '?'} "
            f"协议={hello.protocol_version} 刷新={'on' if session.capabilities.refresh else 'off'}"
        )
        return hello.protocol_version == PROTOCOL_VERSION

    async def _drop(self, session: ClientSession, reason: str) -> None:
        async with self._lock:
            if self._sessions.get(session.name) is session:
                self._sessions.pop(session.name, None)
        await _fail_pending(session, reason)
        logger.info(f"客户端已断开：{session.name}")


_hub = Hub()


def get_hub() -> Hub:
    return _hub


# --------------------------------------------------------------------------- #
# 握手与工具
# --------------------------------------------------------------------------- #


def _authenticate(
    headers: Mapping[str, str],
    cfg: ClientServerConfig,
    registry: ClientRegistry,
) -> tuple[str, str]:
    raw_name = headers.get("x-cpa-client-name") or headers.get("X-CPA-Client-Name") or ""
    name = normalize_client_name(str(raw_name))
    authorization = headers.get("authorization") or headers.get("Authorization") or ""
    token = ""
    if str(authorization).lower().startswith("bearer "):
        token = str(authorization)[7:].strip()
    if not valid_client_name(name):
        return "", "客户端名称非法"
    if cfg.server_name and name == normalize_client_name(cfg.server_name):
        return "", "名称已被本机客户端占用"
    instance = registry.get(name) if registry else None
    if instance is None:
        return "", "客户端未注册"
    if not instance.key or not token or not _tokens_match(token, instance.key):
        return "", "鉴权失败"
    return name, ""


def _tokens_match(provided: str, expected: str) -> bool:
    left = hashlib.sha256(provided.encode("utf-8")).digest()
    right = hashlib.sha256(expected.encode("utf-8")).digest()
    return secrets.compare_digest(left, right)


def _clean_text(value: Any, limit: int) -> str:
    """清理客户端上报的展示字符串：去控制字符、折叠空白、限长。"""
    text = str(value or "")
    text = "".join(ch for ch in text if not unicodedata.category(ch).startswith("C"))
    text = " ".join(text.split())
    if limit > 0 and len(text) > limit:
        text = text[:limit]
    return text


async def _fail_pending(session: ClientSession, reason: str) -> None:
    pending = list(session.pending.values())
    session.pending.clear()
    for item in pending:
        if not item.future.done():
            item.future.set_exception(HubError(reason))


async def _safe_close(websocket: Any, code: int) -> None:
    try:
        await websocket.close(code=code)
    except Exception:  # noqa: BLE001
        pass
