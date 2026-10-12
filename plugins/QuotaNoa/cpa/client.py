from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import urlparse

import httpx

from ..config import CpaConfig

try:
    from nonebot.log import logger
except Exception:  # pragma: no cover
    import logging

    logger = logging.getLogger("QuotaNoa.cpa.client")

# 只允许额度巡检用到的上游。不要把 /api-call 做成聊天里的任意代发。
_API_CALL_ALLOWLIST: tuple[tuple[str, str], ...] = (
    ("api.anthropic.com", "/api/oauth/usage"),
    ("chatgpt.com", "/backend-api/wham/usage"),
    ("chatgpt.com", "/backend-api/wham/rate-limit-reset-credits"),
    ("chatgpt.com", "/backend-api/wham/rate-limit-reset-credits/consume"),
    ("api.kimi.com", "/coding/v1/usages"),
    ("api.kimi.com", "/coding/v1/me"),
    ("api.kimi.ai", "/coding/v1/usages"),
    ("api.kimi.ai", "/coding/v1/me"),
    ("cli-chat-proxy.grok.com", "/v1/billing"),
    ("daily-cloudcode-pa.googleapis.com", "/v1internal:retrieveUserQuotaSummary"),
    ("daily-cloudcode-pa.sandbox.googleapis.com", "/v1internal:retrieveUserQuotaSummary"),
    ("cloudcode-pa.googleapis.com", "/v1internal:retrieveUserQuotaSummary"),
    ("cloudcode-pa.googleapis.com", "/v1internal:retrieveUserQuota"),
    ("daily-cloudcode-pa.googleapis.com", "/v1internal:loadCodeAssist"),
    ("daily-cloudcode-pa.sandbox.googleapis.com", "/v1internal:loadCodeAssist"),
    ("cloudcode-pa.googleapis.com", "/v1internal:loadCodeAssist"),
)


def allowed_api_call_url(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        return False
    host = parsed.hostname.lower()
    path = parsed.path
    return any(host == allowed_host and path == allowed_path for allowed_host, allowed_path in _API_CALL_ALLOWLIST)


def allowed_configured_api_call_url(url: str) -> bool:
    """``generic.probes`` 专用放行：URL 只来自配置文件，永远不来自聊天输入。

    与 :func:`allowed_api_call_url` 的白名单不同，这里不限定主机与路径（用户要在
    配置里声明任意上游探测），但仍要求绝对 URL 且带主机名，避免相对路径或空地址。
    """
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    return True


class CPAError(Exception):
    """Management API 调用失败，消息可直接发给管理员。"""


def management_root(base_url: str) -> str:
    url = base_url.rstrip("/")
    suffix = "/v0/management"
    if url.endswith(suffix):
        return url
    return f"{url}{suffix}"


class ManagementClient:
    def __init__(self, base_url: str, key: str, timeout: float) -> None:
        self._root = management_root(base_url)
        self._key = key
        self._client = httpx.AsyncClient(
            base_url=self._root,
            timeout=httpx.Timeout(timeout),
            headers={
                "Authorization": f"Bearer {key}",
                "X-Management-Key": key,
                "Accept": "application/json",
                "User-Agent": "QuotaNoa-Bot/0.1.0",
            },
        )

    @classmethod
    def from_config(cls, cfg: Any) -> ManagementClient:
        return cls(cfg.base_url, cfg.management_key, cfg.timeout)

    async def aclose(self) -> None:
        await self._client.aclose()

    def _ensure_ready(self) -> None:
        if not self._key:
            raise CPAError("该 CPA 实例未配置管理密钥（management_key），无法调用管理接口。")

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        timeout: float | None = None,
    ) -> httpx.Response:
        self._ensure_ready()
        try:
            response = await self._client.request(
                method, path, params=params, json=json, timeout=timeout
            )
        except httpx.RequestError as exc:
            raise CPAError(f"无法连接 CLIProxyAPI：{exc}") from exc

        if response.status_code == 401:
            raise CPAError("管理密钥无效（401）。请检查该实例的 management_key。")
        if response.status_code == 403:
            detail = _response_detail(response)
            raise CPAError(
                f"管理接口拒绝访问（403）{detail}。"
                "若 Bot 与 CPA 不在同一台机器，需要 allow-remote 或 MANAGEMENT_PASSWORD。"
            )
        if response.status_code >= 400:
            raise CPAError(
                f"CLIProxyAPI 返回 HTTP {response.status_code}{_response_detail(response)}"
            )
        return response

    async def request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        timeout: float | None = None,
    ) -> Any:
        response = await self.request(
            method, path, params=params, json=json, timeout=timeout
        )
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise CPAError("CLIProxyAPI 返回了无法解析的 JSON。") from exc

    async def probe(self) -> dict[str, str]:
        """GET /config 仅用于探活与读取版本头，调用方不得把 body 发进聊天。"""
        response = await self.request("GET", "/config")
        return {
            "version": response.headers.get("X-CPA-VERSION", ""),
            "commit": response.headers.get("X-CPA-COMMIT", ""),
            "build_date": response.headers.get("X-CPA-BUILD-DATE", ""),
            "support_plugin": response.headers.get("X-CPA-SUPPORT-PLUGIN", ""),
        }

    async def latest_version(self) -> str | None:
        try:
            data = await self.request_json("GET", "/latest-version")
        except CPAError:
            return None
        if isinstance(data, dict):
            value = data.get("latest-version")
            return str(value) if value else None
        return None

    async def list_auth_files(
        self,
        *,
        name: str | None = None,
        auth_index: str | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {}
        if name:
            params["name"] = name
        if auth_index:
            params["auth_index"] = auth_index
        data = await self.request_json("GET", "/auth-files", params=params or None)
        files = data.get("files") if isinstance(data, dict) else None
        return files if isinstance(files, list) else []

    async def auth_models(self, name: str) -> list[Any]:
        data = await self.request_json("GET", "/auth-files/models", params={"name": name})
        models = data.get("models") if isinstance(data, dict) else None
        return models if isinstance(models, list) else []

    async def patch_auth_status(
        self,
        name: str,
        disabled: bool,
        auth_index: str | None = None,
    ) -> Any:
        body: dict[str, Any] = {"name": name, "disabled": disabled}
        if auth_index:
            body["auth_index"] = auth_index
        return await self.request_json("PATCH", "/auth-files/status", json=body)

    async def delete_auth_file(self, name: str) -> Any:
        return await self.request_json("DELETE", "/auth-files", params={"name": name})

    async def reset_quota(self, auth_index: str) -> Any:
        return await self.request_json("POST", "/reset-quota", json={"auth_index": auth_index})

    async def start_login(self, path: str) -> dict[str, Any]:
        data = await self.request_json("GET", path)
        return data if isinstance(data, dict) else {}

    async def auth_status(self, state: str) -> dict[str, Any]:
        data = await self.request_json("GET", "/get-auth-status", params={"state": state})
        return data if isinstance(data, dict) else {}

    async def cancel_oauth(self, state: str) -> Any:
        return await self.request_json("DELETE", "/oauth-session", params={"state": state})

    async def oauth_callback(
        self,
        *,
        redirect_url: str | None = None,
        provider: str | None = None,
        state: str | None = None,
        code: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {}
        if redirect_url:
            body["redirect_url"] = redirect_url
        if provider:
            body["provider"] = provider
        if state:
            body["state"] = state
        if code:
            body["code"] = code
        if not body:
            raise CPAError("回调内容为空。")
        data = await self.request_json("POST", "/oauth-callback", json=body)
        return data if isinstance(data, dict) else {}

    async def api_call(
        self,
        auth_index: str,
        method: str,
        url: str,
        *,
        header: dict[str, str] | None = None,
        data: str | None = None,
        timeout: float | None = None,
        allow_configured: bool = False,
    ) -> dict[str, Any]:
        """内部代发上游请求。不要把原始 body 回传到聊天。

        ``allow_configured=True`` 只给 ``generic.probes`` 使用：URL 来自配置文件
        而非聊天输入，因此跳过内置白名单（协议不限）。调用方必须保证 URL 不可能
        被终端用户控制。
        """
        if allow_configured:
            if not allowed_configured_api_call_url(url):
                raise CPAError("generic.probes 的 url 必须是带主机名的绝对地址。")
            if url not in _logged_configured_urls:
                _logged_configured_urls.add(url)
                logger.warning("QuotaNoa 正在按 generic.probes 配置探测非白名单上游：%s", url)
        elif not allowed_api_call_url(url):
            raise CPAError("拒绝调用未列入额度白名单的上游地址。")
        payload: dict[str, Any] = {
            "auth_index": auth_index,
            "method": method,
            "url": url,
        }
        if header:
            payload["header"] = header
        if data is not None:
            payload["data"] = data
        result = await self.request_json("POST", "/api-call", json=payload, timeout=timeout)
        return result if isinstance(result, dict) else {}

    async def quota_fetch(
        self,
        auth_index: str,
        *,
        plugin_id: str = "",
        provider: str = "",
    ) -> dict[str, Any]:
        """``POST /quota/fetch``：CPA 的厂商无关归一化额度。

        没有可用额度 provider 时 CPA 返回 ``501``；这里映射为**空字典**而不是
        异常，让 :func:`cpa.generic.fill_generic` 能继续降级到信号 / 探测。
        """
        body: dict[str, Any] = {"auth_index": auth_index}
        if plugin_id:
            body["plugin_id"] = plugin_id
        if provider:
            body["provider"] = provider
        try:
            data = await self.request_json("POST", "/quota/fetch", json=body)
        except CPAError as exc:
            if "HTTP 501" in str(exc):
                return {}
            raise
        return data if isinstance(data, dict) else {}

    async def list_plugins(self) -> list[dict[str, Any]]:
        data = await self.request_json("GET", "/plugins")
        plugins = data.get("plugins") if isinstance(data, dict) else None
        return plugins if isinstance(plugins, list) else []


_clients: dict[str, ManagementClient] = {}
_retired: list[ManagementClient] = []

#: 已告警过的 generic.probes 非白名单上游（避免每次查询都刷日志）。
_logged_configured_urls: set[str] = set()


def get_client(name: str) -> ManagementClient:
    """按实例名取（或惰性创建）管理客户端。

    未配置该实例时抛 ``CPAError``，消息可直接发给管理员。
    """
    from .. import state

    instance = state.get_snapshot().cpa.get(name)
    if instance is None:
        known = "、".join(state.get_snapshot().cpa.names()) or "（未配置）"
        raise CPAError(f"没有名为「{name}」的 CPA 实例。已配置实例：{known}")
    client = _clients.get(instance.name)
    if client is None:
        client = ManagementClient.from_config(instance)
        _clients[instance.name] = client
    return client


def reset_client(name: str | None = None) -> None:
    """丢弃缓存的客户端，下次 get_client() 按最新快照重建。

    传 ``name`` 只重建该实例，传 None 重建全部。旧客户端放入 _retired 并调度
    异步关闭，避免长期持有打开的连接池。
    """
    if name is None:
        retired = list(_clients.values())
        _clients.clear()
    else:
        client = _clients.pop(name, None)
        retired = [client] if client is not None else []
    if retired:
        _retired.extend(retired)
        _schedule_close_retired()


def _schedule_close_retired() -> None:
    if not _retired:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    clients = list(_retired)
    _retired.clear()

    async def _close_all() -> None:
        for client in clients:
            try:
                await client.aclose()
            except Exception:
                pass

    loop.create_task(_close_all())


async def close_client() -> None:
    clients = list(_retired)
    _retired.clear()
    clients.extend(_clients.values())
    _clients.clear()
    for client in clients:
        try:
            await client.aclose()
        except Exception:
            pass


def _response_detail(response: httpx.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        text = (response.text or "").strip()
        return f"：{text[:200]}" if text else ""
    if isinstance(data, dict):
        for key in ("error", "message", "status"):
            value = data.get(key)
            if value and key != "status":
                return f"：{value}"
            if key == "status" and value not in (None, "ok"):
                return f"：{value}"
    return ""
