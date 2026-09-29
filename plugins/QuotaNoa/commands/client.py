"""/quotanoa client：远程客户端（Server 模式）实例管理。

`data/quotanoa_client.json` 只在创建客户端实例时生成；服务端监听参数由 `.env`
的 ``QUOTANOA_CLIENT_*`` 控制。命令仅超级用户 / admins 可用（继承根命令权限）。
"""

from __future__ import annotations

import secrets

from nonebot_plugin_alconna import Arparma, Query, UniMessage

from .. import state
from ..clienthub import get_hub
from ..config import ClientInstance, ClientRegistry, ConfigError
from ..cpa.format import mask_secret
from ..protocol import normalize_client_name, valid_client_name

from .common import _text
from .quota import quota


@quota.assign("client.add")
async def client_add(
    arp: Arparma,
    name: Query[str] = Query("client.add.name"),
    key: Query[str] = Query("client.add.key.key"),
    note: Query[str] = Query("client.add.note.note"),
) -> None:
    raw = _text(name)
    cname = normalize_client_name(raw)
    if not valid_client_name(cname):
        await UniMessage(f"客户端名称非法：{raw}（1–32 字符，不能含空白或 / \\）").finish()
        return
    registry = state.get_client_registry()
    if registry.get(cname) is not None:
        await UniMessage(
            f"客户端「{cname}」已存在。查看：/quotanoa client list；"
            f"轮换密钥：/quotanoa client key {cname} --rotate"
        ).finish()
        return
    secret = _text(key) if (key.available and _text(key)) else secrets.token_urlsafe(48)
    allow = bool(arp.find("client.add.allow_refresh"))
    entry = ClientInstance(
        name=cname,
        key=secret,
        allow_refresh=allow,
        note=(_text(note) if note.available else ""),
    )
    try:
        state.save_client_registry(ClientRegistry(clients=(*registry.clients, entry)))
    except ConfigError as exc:
        await UniMessage(f"写入客户端配置失败：{exc}").finish()
        return
    cfg = state.client_server_config()
    lines = [
        f"已创建远程客户端「{cname}」。",
        f"  连接地址：{cfg.ws_url()}",
        f"  连接密钥：{secret}",
        f"  allow_refresh：{allow}",
        f"  配置文件：{state.client_config_path()}",
    ]
    if not cfg.enabled:
        lines.append("  ⚠ 服务端未启用：请在 .env 设置 QUOTANOA_CLIENT_SERVER_ENABLED=true 后重启。")
    lines.append("把连接地址与密钥填入客户端 config.json（client.server_url / client.key）。")
    await UniMessage("\n".join(lines)).finish()


@quota.assign("client.list")
async def client_list() -> None:
    registry = state.get_client_registry()
    cfg = state.client_server_config()
    hub = get_hub()
    online = set(hub.online_names())
    lines = [
        "【远程客户端】",
        f"服务端：{'已启用' if cfg.enabled else '未启用'}  {cfg.host}:{cfg.port}  {cfg.ws_url()}",
    ]
    if not registry.clients:
        lines.append("  （尚未创建客户端。新增：/quotanoa client add <名称>）")
    else:
        for client in registry.clients:
            if client.name in online:
                status = hub.client_status(client.name)
                extra = (
                    f"  agent={status.get('agent_version') or '?'}"
                    f" 协议={status.get('protocol_version')}"
                    f" 刷新={'on' if status.get('refresh') else 'off'}"
                )
                lines.append(f"  {client.name}  在线  allow_refresh={client.allow_refresh}{extra}")
            else:
                lines.append(f"  {client.name}  离线  allow_refresh={client.allow_refresh}")
    await UniMessage("\n".join(lines)).finish()


@quota.assign("client.show")
async def client_show(name: Query[str] = Query("client.show.name")) -> None:
    cname = normalize_client_name(_text(name))
    client = state.get_client_registry().get(cname)
    if client is None:
        await UniMessage(f"没有名为「{cname}」的客户端。查看：/quotanoa client list").finish()
        return
    status = get_hub().client_status(cname)
    lines = [
        f"【客户端 {client.name}】",
        f"  online：{bool(status.get('online'))}",
        f"  allow_refresh：{client.allow_refresh}",
        f"  key：{mask_secret(client.key) if client.key else '（未设置）'}",
        f"  note：{client.note or '（无）'}",
    ]
    if status.get("online"):
        lines.append(f"  agent_version：{status.get('agent_version') or '?'}")
        lines.append(f"  protocol_version：{status.get('protocol_version')}")
        lines.append(f"  refresh（客户端上报）：{status.get('refresh')}")
        channels = status.get("channels") or []
        if channels:
            lines.append(f"  channels：{', '.join(channels)}")
    await UniMessage("\n".join(lines)).finish()


@quota.assign("client.key")
async def client_key(
    arp: Arparma,
    name: Query[str] = Query("client.key.name"),
) -> None:
    cname = normalize_client_name(_text(name))
    registry = state.get_client_registry()
    client = registry.get(cname)
    if client is None:
        await UniMessage(f"没有名为「{cname}」的客户端。查看：/quotanoa client list").finish()
        return
    if not arp.find("client.key.rotate"):
        shown = mask_secret(client.key) if client.key else "（未设置）"
        await UniMessage(
            f"客户端「{cname}」当前密钥：{shown}\n"
            f"轮换：/quotanoa client key {cname} --rotate"
        ).finish()
        return
    secret = secrets.token_urlsafe(48)
    updated = ClientRegistry(
        clients=tuple(
            ClientInstance(
                name=item.name,
                key=(secret if item.name == cname else item.key),
                allow_refresh=item.allow_refresh,
                note=item.note,
            )
            for item in registry.clients
        )
    )
    try:
        state.save_client_registry(updated)
    except ConfigError as exc:
        await UniMessage(f"写入失败：{exc}").finish()
        return
    await UniMessage(
        f"已轮换客户端「{cname}」密钥：{secret}\n请同步更新客户端配置并重连。"
    ).finish()


@quota.assign("client.remove")
async def client_remove(
    arp: Arparma,
    name: Query[str] = Query("client.remove.name"),
) -> None:
    cname = normalize_client_name(_text(name))
    registry = state.get_client_registry()
    if registry.get(cname) is None:
        await UniMessage(f"没有名为「{cname}」的客户端。查看：/quotanoa client list").finish()
        return
    if not arp.find("client.remove.yes"):
        await UniMessage(
            f"即将删除客户端「{cname}」。确认请发送：\n/quotanoa client remove {cname} --yes"
        ).finish()
        return
    remaining = ClientRegistry(clients=tuple(c for c in registry.clients if c.name != cname))
    try:
        state.save_client_registry(remaining)
    except ConfigError as exc:
        await UniMessage(f"写入失败：{exc}").finish()
        return
    await UniMessage(f"已删除客户端「{cname}」。").finish()
