"""/quotanoa client：远程客户端（Server 模式）实例与服务端开关管理。

服务端监听设置与客户端列表均存于 ``data/quotanoa_config.json`` 的 ``server`` 段与
顶层 ``clients`` 列表（首建即生成，服务器模式默认关）。命令仅超级用户 / admins 可用。
"""

from __future__ import annotations

import secrets

from nonebot_plugin_alconna import Arparma, Query, UniMessage

from .. import state
from ..clienthub import get_hub
from ..config import ClientInstance, ConfigError
from ..cpa.format import mask_secret
from ..protocol import normalize_client_name, valid_client_name

from .common import _text
from .quota import quota


def _clients() -> tuple[ClientInstance, ...]:
    return state.get_snapshot().clients


def _write_clients(clients: tuple[ClientInstance, ...]) -> None:
    state.update_config({"clients": [client.to_dict() for client in clients]})


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
    clients = _clients()
    if any(item.name == cname for item in clients):
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
        _write_clients((*clients, entry))
    except ConfigError as exc:
        await UniMessage(f"写入配置失败：{exc}").finish()
        return
    cfg = state.get_snapshot().server
    lines = [
        f"已创建远程客户端「{cname}」。",
        f"  连接地址：{cfg.ws_url()}",
        f"  连接密钥：{secret}",
        f"  allow_refresh：{allow}",
        f"  配置文件：{state.snapshot_path() or '（内存模式）'}",
    ]
    if not cfg.enabled:
        lines.append(
            "  ⚠ 服务端未启用：发送 /quotanoa client server on 或把 "
            "data/quotanoa_config.json 的 server.enabled 设为 true。"
        )
    lines.append("把连接地址与密钥填入客户端 config.json（client.server_url / client.key）。")
    await UniMessage("\n".join(lines)).finish()


@quota.assign("client.list")
async def client_list() -> None:
    clients = _clients()
    cfg = state.get_snapshot().server
    hub = get_hub()
    online = set(hub.online_names())
    lines = [
        "【远程客户端】",
        f"服务端：{'已启用' if cfg.enabled else '未启用'}  {cfg.host}:{cfg.port}  {cfg.ws_url()}",
    ]
    if not clients:
        lines.append("  （尚未创建客户端。新增：/quotanoa client add <名称>）")
    else:
        for client in clients:
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
    client = next((item for item in _clients() if item.name == cname), None)
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
    clients = _clients()
    client = next((item for item in clients if item.name == cname), None)
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
    updated = tuple(
        ClientInstance(
            name=item.name,
            key=(secret if item.name == cname else item.key),
            allow_refresh=item.allow_refresh,
            note=item.note,
        )
        for item in clients
    )
    try:
        _write_clients(updated)
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
    clients = _clients()
    if not any(item.name == cname for item in clients):
        await UniMessage(f"没有名为「{cname}」的客户端。查看：/quotanoa client list").finish()
        return
    if not arp.find("client.remove.yes"):
        await UniMessage(
            f"即将删除客户端「{cname}」。确认请发送：\n/quotanoa client remove {cname} --yes"
        ).finish()
        return
    remaining = tuple(item for item in clients if item.name != cname)
    try:
        _write_clients(remaining)
    except ConfigError as exc:
        await UniMessage(f"写入失败：{exc}").finish()
        return
    await UniMessage(f"已删除客户端「{cname}」。").finish()


# --------------------------------------------------------------------------- #
# 服务端开关（独立于客户端是否存在）
# --------------------------------------------------------------------------- #


async def _set_server_enabled(enabled: bool) -> None:
    cfg = state.get_snapshot().server
    if cfg.enabled == enabled:
        await UniMessage(f"服务端已处于{'开启' if enabled else '关闭'}状态。").finish()
        return
    try:
        state.update_config({"server": {"enabled": enabled}})
    except ConfigError as exc:
        await UniMessage(f"写入配置失败：{exc}").finish()
        return
    action = "已开启" if enabled else "已关闭"
    await UniMessage(
        f"远程客户端服务端{action}（{cfg.host}:{cfg.port}）。\n"
        f"{'客户端现在可以连接。' if enabled else '已断开全部客户端连接。'}"
    ).finish()


@quota.assign("client.server.on")
async def client_server_on() -> None:
    await _set_server_enabled(True)


@quota.assign("client.server.off")
async def client_server_off() -> None:
    await _set_server_enabled(False)


@quota.assign("client.server.show")
async def client_server_show() -> None:
    cfg = state.get_snapshot().server
    hub = get_hub()
    lines = [
        "【远程客户端服务端】",
        f"  enabled：{cfg.enabled}",
        f"  server_name：{cfg.server_name}",
        f"  listen：{cfg.host}:{cfg.port}",
        f"  ws_url：{cfg.ws_url()}",
        f"  request_timeout：{cfg.request_timeout:g}s",
        f"  ws_max_size：{cfg.ws_max_size}",
        f"  max_accounts：{cfg.max_accounts}",
        f"  在线客户端：{', '.join(hub.online_names()) or '（无）'}",
    ]
    await UniMessage("\n".join(lines)).finish()
