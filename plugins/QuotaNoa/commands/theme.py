"""/quotanoa theme / /quotanoa card：额度图主题与卡片排版。"""

from __future__ import annotations

from nonebot_plugin_alconna import Query, UniMessage

from ..render.settings import get_render_settings, set_cards_per_row, set_max_cards_per_channel, set_theme
from ..render.themes import get_theme_registry

from .common import _text, _without
from .quota import quota


@quota.assign("theme.set")
async def quota_theme_set(name: Query[str] = Query("theme.set.name")) -> None:
    theme_name = _text(name)
    try:
        settings = set_theme(theme_name)
    except ValueError as exc:
        allowed = " / ".join(get_theme_registry().list_canonical_names())
        await UniMessage(f"{exc}\n可用主题：{allowed}").finish()
        return
    await UniMessage(f"已将额度图主题设置为「{settings.theme}」。").finish()


@quota.assign("theme", additional=_without("theme.set"))
async def quota_theme_get() -> None:
    settings = get_render_settings()
    registry = get_theme_registry()
    allowed = " / ".join(registry.list_canonical_names())
    aliases = registry.get_alias_map()
    alias_notes = [
        f"{alias} → {canonical}"
        for alias, canonical in sorted(aliases.items())
        if alias != canonical
    ]
    alias_text = f"\n别名：{' / '.join(alias_notes)}" if alias_notes else ""
    await UniMessage(
        f"当前额度图主题：{settings.theme}\n"
        f"可选主题：{allowed}{alias_text}\n"
        f"修改主题：/quotanoa theme set <主题>"
    ).finish()


@quota.assign("card.row")
async def quota_card_row(count: Query[str] = Query("card.row.count")) -> None:
    val = _text(count)
    try:
        settings = set_cards_per_row(val)
    except ValueError as exc:
        await UniMessage(str(exc)).finish()
        return
    await UniMessage(f"已设置每行展示 {settings.cards_per_row} 张卡片。").finish()


@quota.assign("card.max")
async def quota_card_max(count: Query[str] = Query("card.max.count")) -> None:
    val = _text(count)
    try:
        settings = set_max_cards_per_channel(val)
    except ValueError as exc:
        await UniMessage(str(exc)).finish()
        return
    await UniMessage(f"已设置每渠道最多展示 {settings.max_cards_per_channel} 张账号卡片。").finish()


@quota.assign("card", additional=_without("card.row", "card.max"))
async def quota_card_get() -> None:
    settings = get_render_settings()
    await UniMessage(
        f"当前每行卡片数：{settings.cards_per_row} (1..6)\n"
        f"每渠道最多账号卡片数：{settings.max_cards_per_channel}\n"
        f"修改排版：/quotanoa card row <数量>；/quotanoa card max <数量>"
    ).finish()
