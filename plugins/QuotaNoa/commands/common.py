"""命令层公共工具：权限、文本解析、凭证查找。

被 `quota.py`（/quotanoa 根）与 `cpa.py`（/cpa 管理根）共享。

凭证操作分两种调用形态：

- **实例内**：``/cpa auth … <实例> <查询词>``，用 ``_require_one(instance, query)``；
- **跨实例**：``/quotanoa alias …`` / ``/quotanoa reset`` 不指定实例，用
  ``_require_one_across(query)`` 在全部实例中搜索，返回 ``(实例名, 凭证)``。
"""

from __future__ import annotations

from typing import Any

from nonebot.adapters import Bot, Event
from nonebot.permission import SUPERUSER, Permission
from nonebot_plugin_alconna import Image, Query, UniMessage

from .. import state
from ..cpa.client import CPAError, get_client
from ..cpa.format import (
    format_ambiguous,
    match_auth,
)
from ..cpa.quota import platform_of
from ..help import HelpDoc
from ..model import normalize_channel
from ..render.help_page import render_help_images
from ..render.html import RenderError

#: CPA 平台/渠道关键字（供 alias set 校验渠道名时复用）。
KNOWN_CHANNELS = (
    "claude / codex(gpt, openai) / antigravity(反重力, agy) / kimi / xai / gemini-cli / 火山(volcengine, ark) / workbuddy(wb) / qoder"
)


async def _extra_admin(event: Event) -> bool:
    try:
        user_id = event.get_user_id()
    except Exception:
        return False
    return user_id in state.get_snapshot().cpa.admins


CPA_ADMIN = SUPERUSER | Permission(_extra_admin)


def _can_refresh_codex(event: Event) -> bool:
    try:
        user_id = event.get_user_id()
    except Exception:
        return False
    admins = state.get_snapshot().cpa.codex_refresh_admin
    return bool(admins) and user_id in admins


def _text(query: Query[Any]) -> str:
    raw = query.result
    if isinstance(raw, (list, tuple)):
        return " ".join(str(item).strip() for item in raw if str(item).strip()).strip()
    return str(raw).strip()


def _without(*paths: str):
    async def _check(_event: Event, _bot: Bot, _state: Any, result: Any) -> bool:
        return all(result.query(path, "\0") == "\0" for path in paths)

    return _check


def instance_names() -> tuple[str, ...]:
    """当前配置里的全部 CPA 实例名。"""
    return state.get_snapshot().cpa.names()


async def send_help(doc: HelpDoc, *, text: bool, render=render_help_images) -> None:
    """发送帮助：默认只发一张帮助图（主题跟随额度图主题），--text 或渲染失败回退纯文字。"""
    if text:
        await UniMessage(doc.to_text()).finish()
        return
    try:
        _theme, images = await render(doc)
    except Exception as exc:  # RenderError 及兜底
        await UniMessage(f"{doc.to_text()}\n\n（帮助图渲染失败，已回退为文字帮助：{exc}）").finish()
        return
    if not images:
        await UniMessage(doc.to_text()).finish()
        return
    for index, png in enumerate(images, start=1):
        msg = UniMessage(Image(raw=png, mimetype="image/png"))
        if index == len(images):
            await msg.finish()
        else:
            await msg.send()


# --------------------------------------------------------------------------- #
# 实例内查找
# --------------------------------------------------------------------------- #


async def _lookup(instance: str, query: str) -> list[dict[str, Any]]:
    client = get_client(instance)
    needle = query.strip()
    attempts: list[dict[str, str]] = [{"auth_index": needle}, {"name": needle}]
    if not needle.endswith(".json"):
        attempts.append({"name": f"{needle}.json"})
    for kwargs in attempts:
        try:
            files = await client.list_auth_files(**kwargs)
        except CPAError as exc:
            if "404" not in str(exc):
                raise
            files = []
        if files:
            return files
    files = await client.list_auth_files()
    return match_auth(files, needle)


async def _require_one(instance: str, query: str) -> dict[str, Any]:
    try:
        files = await _lookup(instance, query)
    except CPAError as exc:
        await UniMessage(str(exc)).finish()
        raise
    if not files:
        await UniMessage(f"没有找到凭证：{query}").finish()
        raise CPAError(f"没有找到凭证：{query}")
    if len(files) > 1:
        await UniMessage(format_ambiguous(query, files, instance=instance)).finish()
        raise CPAError("匹配到多个凭证")
    return files[0]


async def _require_platform_account(instance: str, provider: str, query: str) -> dict[str, Any]:
    platform = normalize_channel(provider)
    if not platform:
        await UniMessage(
            f"未知渠道「{provider}」。用法：/quotanoa alias set <渠道> <邮箱> <别名>\n"
            f"渠道如：{KNOWN_CHANNELS}"
        ).finish()
        raise CPAError(f"未知渠道：{provider}")
    try:
        files = await get_client(instance).list_auth_files()
    except CPAError as exc:
        await UniMessage(str(exc)).finish()
        raise
    scoped = [item for item in files if platform_of(item) == platform]
    matched = match_auth(scoped, query)
    if not matched:
        await UniMessage(f"没有找到 [{platform}] 凭证：{query}").finish()
        raise CPAError(f"没有找到凭证：{query}")
    if len(matched) > 1:
        await UniMessage(format_ambiguous(f"{platform} {query}", matched, instance=instance)).finish()
        raise CPAError("匹配到多个凭证")
    return matched[0]


async def _set_disabled(instance: str, query: str, *, disabled: bool) -> None:
    file = await _require_one(instance, query)
    name = str(file.get("name") or "")
    if not name:
        await UniMessage("该凭证没有文件名，无法改状态（配置型 API-key / 插件虚拟项请用其它方式）。").finish()
    try:
        await get_client(instance).patch_auth_status(
            name,
            disabled,
            auth_index=str(file.get("auth_index") or "") or None,
        )
    except CPAError as exc:
        await UniMessage(str(exc)).finish()
    action = "已禁用" if disabled else "已启用"
    await UniMessage(f"{action} {name}。").finish()


# --------------------------------------------------------------------------- #
# 跨实例查找（别名 / reset 等不指定实例的场景）
# --------------------------------------------------------------------------- #


async def _collect_across() -> list[tuple[str, dict[str, Any]]]:
    """返回全部 CPA 实例的 (实例名, 凭证) 列表；实例不可用时跳过。"""
    collected: list[tuple[str, dict[str, Any]]] = []
    for name in instance_names():
        try:
            files = await get_client(name).list_auth_files()
        except CPAError:
            continue
        collected.extend((name, item) for item in files)
    return collected


# --------------------------------------------------------------------------- #
# 别名候选：CPA 凭证 + WorkBuddy 网关账号
# --------------------------------------------------------------------------- #


async def _workbuddy_candidates() -> list[tuple[str, dict[str, Any]]]:
    """把 WorkBuddy 网关账号伪装成凭证字典，供别名绑定复用同一套匹配逻辑。

    绑定别名需要一次 ``/v1/quota`` 查询；查询失败（网关不可达等）时跳过，
    不阻断别名操作。
    """
    from ..wb.client import WorkbuddyError, fetch_quota

    servers = state.get_snapshot().workbuddy.servers
    collected: list[tuple[str, dict[str, Any]]] = []
    for server in servers:
        try:
            payload = await fetch_quota(server)
        except WorkbuddyError:
            continue
        rows = payload.get("accounts") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            uid = str(row.get("uid") or "").strip()
            if not uid:
                continue
            nickname = str(row.get("nickname") or "").strip()
            collected.append(
                (
                    server.name,
                    {
                        "provider": "workbuddy",
                        "auth_index": uid,
                        "name": nickname or uid,
                        "label": nickname,
                        "disabled": bool(row.get("disabled")),
                    },
                )
            )
    return collected


async def _qoder_candidates() -> list[tuple[str, dict[str, Any]]]:
    """把 Qoder 代理账号伪装成凭证字典，供别名绑定复用同一套匹配逻辑。"""
    from ..qoder.client import QoderError, fetch_credits

    servers = state.get_snapshot().qoder.servers
    collected: list[tuple[str, dict[str, Any]]] = []
    for server in servers:
        try:
            payload = await fetch_credits(server)
        except QoderError:
            continue
        rows = payload.get("accounts") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            uid = str(row.get("id") or "").strip()
            if not uid:
                continue
            name = str(row.get("name") or row.get("email") or row.get("user_id") or "").strip()
            flags = row.get("flags")
            disabled = bool(flags.get("skip_auth")) if isinstance(flags, dict) else False
            collected.append(
                (
                    server.name,
                    {
                        "provider": "qoder",
                        "auth_index": uid,
                        "name": name or uid,
                        "label": name,
                        "disabled": disabled,
                    },
                )
            )
    return collected


async def _collect_alias_across() -> list[tuple[str, dict[str, Any]]]:
    """别名绑定用的候选：CPA 凭证 + WorkBuddy 网关账号 + Qoder 代理账号。"""
    collected = await _collect_across()
    collected.extend(await _workbuddy_candidates())
    collected.extend(await _qoder_candidates())
    return collected


async def _require_one_across(query: str, *, include_workbuddy: bool = False) -> tuple[str, dict[str, Any]]:
    candidates = await _collect_alias_across() if include_workbuddy else await _collect_across()
    needle = query.strip()
    matched: list[tuple[str, dict[str, Any]]] = []
    for instance, file in candidates:
        hits = match_auth([file], needle)
        if hits:
            matched.append((instance, file))
    if not matched:
        await UniMessage(f"没有找到凭证：{query}").finish()
        raise CPAError(f"没有找到凭证：{query}")
    if len(matched) > 1:
        if _same_physical_credential(matched):
            # 多实例镜像部署的同一凭据：别名是全局的，取首个即可。
            return matched[0]
        shown = "、".join(f"{instance}:{file.get('name') or file.get('auth_index')}" for instance, file in matched)
        await UniMessage(
            f"「{query}」匹配到多个实例的不同凭证：{shown}\n请输入更精确的完整邮箱或文件名。"
        ).finish()
        raise CPAError("匹配到多个凭证")
    return matched[0]


def _same_physical_credential(matched: list[tuple[str, dict[str, Any]]]) -> bool:
    """多个实例命中的是否为同一物理凭据（身份键一致）。"""
    from ..aliases import unique_identity_keys

    first = {key.lower() for key in unique_identity_keys(matched[0][1])}
    if not first:
        return False
    return all({key.lower() for key in unique_identity_keys(item[1])} == first for item in matched[1:])


async def _require_platform_account_across(provider: str, query: str) -> tuple[str, dict[str, Any]]:
    platform = normalize_channel(provider)
    if not platform:
        await UniMessage(
            f"未知渠道「{provider}」。用法：/quotanoa alias set <渠道> <邮箱> <别名>\n"
            f"渠道如：{KNOWN_CHANNELS}"
        ).finish()
        raise CPAError(f"未知渠道：{provider}")
    # WorkBuddy / Qoder 是本地渠道（网关/代理账号），不在 CPA 凭证里；纳入候选才能绑别名。
    candidates = await _collect_alias_across() if platform in {"workbuddy", "qoder"} else await _collect_across()
    matched: list[tuple[str, dict[str, Any]]] = []
    for instance, file in candidates:
        if platform_of(file) != platform:
            continue
        if match_auth([file], query):
            matched.append((instance, file))
    if not matched:
        await UniMessage(f"没有找到 [{platform}] 凭证：{query}").finish()
        raise CPAError(f"没有找到凭证：{query}")
    if len(matched) > 1:
        if _same_physical_credential(matched):
            return matched[0]
        shown = "、".join(f"{instance}:{file.get('name') or file.get('auth_index')}" for instance, file in matched)
        await UniMessage(
            f"[{platform}]「{query}」匹配到多个实例的不同凭证：{shown}\n请输入更精确的完整邮箱或文件名。"
        ).finish()
        raise CPAError("匹配到多个凭证")
    return matched[0]
