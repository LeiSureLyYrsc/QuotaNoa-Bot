"""额度图渲染设置（主题 / 每行卡片数）。

设置现在存放在 ``data/quotanoa_config.json`` 的 ``render`` 段，由 ``state`` 统一
做热重载与原子写入。本模块只负责校验与读写这一小段配置。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from .themes import get_theme_registry

DEFAULT_THEME = "default"
DEFAULT_CARDS_PER_ROW = 3
MIN_CARDS_PER_ROW = 1
MAX_CARDS_PER_ROW = 6

DEFAULT_MAX_CARDS_PER_CHANNEL = 40
MIN_MAX_CARDS_PER_CHANNEL = 1
MAX_MAX_CARDS_PER_CHANNEL = 200


@dataclass
class RenderSettings:
    theme: str = DEFAULT_THEME
    cards_per_row: int = DEFAULT_CARDS_PER_ROW
    max_cards_per_channel: int = DEFAULT_MAX_CARDS_PER_CHANNEL
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = dict(self.extra)
        data["theme"] = self.theme
        data["cards_per_row"] = self.cards_per_row
        data["max_cards_per_channel"] = self.max_cards_per_channel
        return data


def normalize_theme(value: str) -> str:
    text = (value or "").strip().lower()
    registry = get_theme_registry()
    if registry.is_valid_theme(text):
        return registry.resolve_theme_name(text)
    allowed = ", ".join(registry.list_canonical_names())
    raise ValueError(f"未知主题「{value}」。可选主题：{allowed}")


def normalize_theme_or_default(value: Any) -> str:
    if not isinstance(value, str):
        return DEFAULT_THEME
    text = value.strip().lower()
    registry = get_theme_registry()
    return registry.resolve_theme_name(text)


def normalize_cards_per_row(value: Any) -> int:
    try:
        num = int(value)
    except (ValueError, TypeError):
        raise ValueError(f"每行卡片数必须为 {MIN_CARDS_PER_ROW} 到 {MAX_CARDS_PER_ROW} 的整数。")
    if not (MIN_CARDS_PER_ROW <= num <= MAX_CARDS_PER_ROW):
        raise ValueError(f"每行卡片数必须为 {MIN_CARDS_PER_ROW} 到 {MAX_CARDS_PER_ROW} 的整数。")
    return num


def normalize_cards_per_row_or_default(value: Any) -> int:
    try:
        num = int(value)
        if MIN_CARDS_PER_ROW <= num <= MAX_CARDS_PER_ROW:
            return num
    except (ValueError, TypeError):
        pass
    return DEFAULT_CARDS_PER_ROW


def normalize_max_cards_per_channel(value: Any) -> int:
    try:
        num = int(value)
    except (ValueError, TypeError):
        raise ValueError(f"每渠道最多账号卡片数必须为 {MIN_MAX_CARDS_PER_CHANNEL} 到 {MAX_MAX_CARDS_PER_CHANNEL} 的整数。")
    if not (MIN_MAX_CARDS_PER_CHANNEL <= num <= MAX_MAX_CARDS_PER_CHANNEL):
        raise ValueError(f"每渠道最多账号卡片数必须为 {MIN_MAX_CARDS_PER_CHANNEL} 到 {MAX_MAX_CARDS_PER_CHANNEL} 的整数。")
    return num


def normalize_max_cards_per_channel_or_default(value: Any) -> int:
    try:
        num = int(value)
        if MIN_MAX_CARDS_PER_CHANNEL <= num <= MAX_MAX_CARDS_PER_CHANNEL:
            return num
    except (ValueError, TypeError):
        pass
    return DEFAULT_MAX_CARDS_PER_CHANNEL


def _render_section() -> Mapping[str, Any]:
    from .. import state

    render = state.get_snapshot().render
    return {
        "theme": render.theme,
        "cards_per_row": render.cards_per_row,
        "max_cards_per_channel": render.max_cards_per_channel,
    }


def get_render_settings() -> RenderSettings:
    section = _render_section()
    return RenderSettings(
        theme=normalize_theme_or_default(section.get("theme")),
        cards_per_row=normalize_cards_per_row_or_default(section.get("cards_per_row")),
        max_cards_per_channel=normalize_max_cards_per_channel_or_default(section.get("max_cards_per_channel")),
    )


def _persist(theme: str, cards_per_row: int, max_cards_per_channel: int) -> RenderSettings:
    from .. import state

    state.update_config(
        {
            "render": {
                "theme": theme,
                "cards_per_row": cards_per_row,
                "max_cards_per_channel": max_cards_per_channel,
            }
        }
    )
    return get_render_settings()


def set_theme(theme_name: str) -> RenderSettings:
    target = normalize_theme(theme_name)
    current = get_render_settings()
    return _persist(target, current.cards_per_row, current.max_cards_per_channel)


def set_cards_per_row(count: int | str) -> RenderSettings:
    target = normalize_cards_per_row(count)
    current = get_render_settings()
    return _persist(current.theme, target, current.max_cards_per_channel)


def set_max_cards_per_channel(count: int | str) -> RenderSettings:
    target = normalize_max_cards_per_channel(count)
    current = get_render_settings()
    return _persist(current.theme, current.cards_per_row, target)
