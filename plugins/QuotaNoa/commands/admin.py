"""/quotanoa admin：管理员名单（quotanoa_admin）管理，仅超级用户可用。

名单存于 ``data/quotanoa_config.json`` 顶层 ``quotanoa_admin``：名单内 user id 可用
``/quotanoa`` 与 ``/cpa`` 的绝大多数功能；Codex 重置（``cpa.codex_refresh_admin``）
与本名单自身的增删不在此列，始终限超级用户。
"""

from __future__ import annotations

from nonebot.adapters import Bot, Event
from nonebot_plugin_alconna import Query, UniMessage

from .. import state
from ..config import ConfigError

from .common import _require_superuser, _text, _without
from .quota import quota


def _admins_raw() -> list[str]:
    """读当前 ``quotanoa_admin`` 原始列表（trim、去空、去重、保序）。

    ``state.update_config`` 对列表是**整体替换**而非逐元素合并，增删必须
    先读全量、在副本上变更、再整表写回。
    """
    return list(state.get_snapshot().quotanoa_admin)


def _write_admins(users: list[str]) -> None:
    state.update_config({"quotanoa_admin": users})


def _overview() -> str:
    snapshot = state.get_snapshot()
    admins = snapshot.quotanoa_admin
    lines = [
        "【quotanoa 管理员名单】（仅超级用户可增删）",
        (
            f"quotanoa_admin：{', '.join(admins)}"
            if admins
            else "quotanoa_admin：（未设置。添加：/quotanoa admin add <用户ID>）"
        ),
        f"codex_refresh_admin（只读）：{', '.join(snapshot.cpa.codex_refresh_admin) or '（未设置）'}"
        "——Codex 重置权限由 cpa.codex_refresh_admin 单独控制，不在此处管理。",
        "SUPERUSERS 始终拥有全部权限，无需加入名单。",
    ]
    return "\n".join(lines)


@quota.assign("admin.list")
async def quota_admin_list(bot: Bot, event: Event) -> None:
    if not await _require_superuser(bot, event):
        await UniMessage("仅超级用户（SUPERUSERS）可管理管理员名单。").finish()
        return
    await UniMessage(_overview()).finish()


@quota.assign("admin.add")
async def quota_admin_add(
    bot: Bot,
    event: Event,
    user: Query[str] = Query("admin.add.user"),
) -> None:
    if not await _require_superuser(bot, event):
        await UniMessage("仅超级用户（SUPERUSERS）可管理管理员名单。").finish()
        return
    uid = _text(user)
    if not uid:
        await UniMessage("请提供用户ID。").finish()
        return
    admins = _admins_raw()
    if uid in admins:
        await UniMessage("已在名单中。").finish()
        return
    try:
        _write_admins([*admins, uid])
    except ConfigError as exc:
        await UniMessage(f"写入配置失败：{exc}").finish()
        return
    await UniMessage(f"已将 {uid} 加入 quotanoa_admin。查看：/quotanoa admin list").finish()


@quota.assign("admin.remove")
async def quota_admin_remove(
    bot: Bot,
    event: Event,
    user: Query[str] = Query("admin.remove.user"),
) -> None:
    if not await _require_superuser(bot, event):
        await UniMessage("仅超级用户（SUPERUSERS）可管理管理员名单。").finish()
        return
    uid = _text(user)
    if not uid:
        await UniMessage("请提供用户ID。").finish()
        return
    admins = _admins_raw()
    if uid not in admins:
        await UniMessage("不在名单中。").finish()
        return
    try:
        _write_admins([item for item in admins if item != uid])
    except ConfigError as exc:
        await UniMessage(f"写入配置失败：{exc}").finish()
        return
    await UniMessage(f"已将 {uid} 从 quotanoa_admin 移除。查看：/quotanoa admin list").finish()


@quota.assign("admin", additional=_without("admin.list", "admin.add", "admin.remove"))
async def quota_admin(bot: Bot, event: Event) -> None:
    if not await _require_superuser(bot, event):
        await UniMessage("仅超级用户（SUPERUSERS）可管理管理员名单。").finish()
        return
    await UniMessage(_overview()).finish()
