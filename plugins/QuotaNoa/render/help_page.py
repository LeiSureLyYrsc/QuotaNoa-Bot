"""帮助页面分页与图片渲染模块。

本模块负责：
1. 根据行预算将 HelpDoc 分页为 HelpPage (paginate_help)；
2. 结合所选主题、全局 base.css 与 help.css 生成自包含 HTML 页面 (build_help_html)；
3. 将帮助卡片渲染为单张完整图片并对产物进行 LRU 缓存 (render_help_images)。
"""

from __future__ import annotations

import asyncio
import hashlib
import html
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

from ..help import HelpDoc, HelpEntry, HelpNote, HelpSection
from .highlight import highlight_html
from .html import RenderError, compose_document, screenshot_html
from .settings import get_render_settings
from .themes import get_theme_registry, validate_css_security

__all__ = [
    "HELP_WIDTH",
    "HELP_ROWS_PER_IMAGE",
    "MAX_CACHE_ENTRIES",
    "HelpPageSection",
    "HelpPage",
    "paginate_help",
    "build_help_html",
    "render_help_images",
    "clear_help_cache",
]

HELP_WIDTH: int = 960
HELP_ROWS_PER_IMAGE: int = 42
MAX_CACHE_ENTRIES: int = 16

_ASSETS_DIR = Path(__file__).resolve().parent / "assets"
_HELP_CSS_PATH = _ASSETS_DIR / "help.css"
_HELP_CSS_CACHE: str | None = None

_IMAGE_CACHE: OrderedDict[tuple[str, int, int, str], bytes] = OrderedDict()
_CACHE_LOCK = asyncio.Lock()


@dataclass(frozen=True)
class HelpPageSection:
    section: HelpSection
    items: tuple[HelpEntry | HelpNote, ...]
    continued: bool = False


HelpPage = tuple[HelpPageSection, ...]


def _item_rows(item: HelpEntry | HelpNote) -> int:
    if isinstance(item, HelpEntry):
        return 1 + len(item.details)
    if isinstance(item, HelpNote):
        return max(1, len(item.lines))
    return 1


def paginate_help(
    doc: HelpDoc,
    *,
    rows_per_image: int = HELP_ROWS_PER_IMAGE,
) -> list[HelpPage]:
    """将 HelpDoc 按照行数预算分割为若干 HelpPage。

    - header rows (仅第 1 页预留): 0 (若无 title 且无 subtitle) 否则 1 + len(subtitle)
    - section head rows: 1 + (1 if intro else 0)，续段只占 1 行标题行
    - item rows: HelpEntry 为 1 + len(details)，HelpNote 为 max(1, len(lines))
    - 仅在 item 边界处切分 section
    - 每个 item 恰好出现一次
    - 至少返回一页 (空文档返回 [()])
    """
    if not doc.sections:
        return [()]

    header_rows = 0 if (not doc.title and not doc.subtitle) else 1 + len(doc.subtitle)

    pages: list[HelpPage] = []
    current_page_sections: list[HelpPageSection] = []
    current_page_rows = header_rows

    def start_new_page() -> None:
        nonlocal current_page_rows, current_page_sections
        pages.append(tuple(current_page_sections))
        current_page_sections = []
        current_page_rows = 0

    for section in doc.sections:
        remaining_items = list(section.items)
        is_first_fragment = True

        if not remaining_items:
            # 节没有条目，仅有标题/导语
            head_cost = 1 + (1 if section.intro else 0)
            if current_page_sections and (current_page_rows + head_cost > rows_per_image):
                start_new_page()
            current_page_sections.append(
                HelpPageSection(section=section, items=(), continued=False)
            )
            current_page_rows += head_cost
            continue

        while remaining_items:
            head_cost = (1 + (1 if section.intro else 0)) if is_first_fragment else 1
            first_item_cost = _item_rows(remaining_items[0])

            # 如果当前页已有内容，且即便只放头部 + 1 个条目都会超标，则换新页
            if current_page_sections and (current_page_rows + head_cost + first_item_cost > rows_per_image):
                start_new_page()
                head_cost = (1 + (1 if section.intro else 0)) if is_first_fragment else 1

            fragment_items: list[HelpEntry | HelpNote] = []
            fragment_rows = head_cost

            while remaining_items:
                next_item = remaining_items[0]
                item_cost = _item_rows(next_item)

                # 如果当前页（以及当前 fragment）为空，必须至少容纳 1 个 item（即使超标，避免死循环）
                if not fragment_items and not current_page_sections:
                    fragment_items.append(remaining_items.pop(0))
                    fragment_rows += item_cost
                elif current_page_rows + fragment_rows + item_cost <= rows_per_image:
                    fragment_items.append(remaining_items.pop(0))
                    fragment_rows += item_cost
                else:
                    break

            current_page_sections.append(
                HelpPageSection(
                    section=section,
                    items=tuple(fragment_items),
                    continued=not is_first_fragment,
                )
            )
            current_page_rows += fragment_rows
            is_first_fragment = False

            # 若本节还有剩余条目，说明当前页已满，需要开启新页
            if remaining_items:
                start_new_page()

    if current_page_sections:
        pages.append(tuple(current_page_sections))

    return pages if pages else [()]


def _page_html(doc: HelpDoc, page: HelpPage, *, page_number: int) -> str:
    """生成单页内容卡片 HTML。"""
    parts: list[str] = ['<article class="card help-card">']

    # 头部仅在第一页且存在标题/副标题时渲染
    if page_number == 1 and (doc.title or doc.subtitle):
        parts.append('<header class="help-head">')
        if doc.title:
            parts.append(f'<h1 class="help-title">{html.escape(doc.title)}</h1>')
        for sub in doc.subtitle:
            parts.append(f'<p class="help-subtitle">{html.escape(sub)}</p>')
        parts.append('</header>')

    # 各分段内容
    for frag in page:
        parts.append('<section class="help-section">')
        title_text = f"{frag.section.title}（续）" if frag.continued else frag.section.title
        parts.append(f'<h2 class="help-section-title">{html.escape(title_text)}</h2>')
        if not frag.continued and frag.section.intro:
            parts.append(f'<p class="help-section-intro">{html.escape(frag.section.intro)}</p>')

        for item in frag.items:
            if isinstance(item, HelpEntry):
                parts.append('<div class="help-entry">')
                parts.append(f'<span class="help-usage">{highlight_html(item.usage)}</span>')
                parts.append(f'<span class="help-entry-desc">{highlight_html(item.desc)}</span>')
                for line in item.details:
                    parts.append(f'<div class="help-entry-detail">{highlight_html(line)}</div>')
                parts.append('</div>')
            elif isinstance(item, HelpNote):
                for idx, line in enumerate(item.lines):
                    cls = "help-note help-note-sub" if (idx > 0 or getattr(item, "sub", False)) else "help-note"
                    parts.append(f'<div class="{cls}">{highlight_html(line)}</div>')

        parts.append('</section>')

    parts.append('</article>')
    return "".join(parts)


def _help_css() -> str:
    """读取并安全校验 help.css。若不存在或校验失败则抛出 RenderError。"""
    global _HELP_CSS_CACHE
    if _HELP_CSS_CACHE is not None:
        return _HELP_CSS_CACHE

    if not _HELP_CSS_PATH.is_file():
        raise RenderError(f"帮助图样式缺失：未找到 {_HELP_CSS_PATH}")

    try:
        content = _HELP_CSS_PATH.read_text(encoding="utf-8")
        validate_css_security(content)
    except Exception as exc:
        raise RenderError(f"帮助图样式不合法：{exc}") from exc

    _HELP_CSS_CACHE = content
    return _HELP_CSS_CACHE


def build_help_html(
    doc: HelpDoc,
    page: HelpPage,
    *,
    page_number: int,
    page_count: int,
    theme: str | None = None,
    width: int = HELP_WIDTH,
) -> str:
    """构建单页帮助 HTML 文档。"""
    registry = get_theme_registry()
    target_theme = theme or get_render_settings().theme
    resolved_name = registry.resolve_theme_name(target_theme)
    theme_obj = registry.get_theme(resolved_name)

    page_note = f'<div class="page-note">第 {page_number} / {page_count} 页</div>' if page_count > 1 else ""
    escaped_doc_title = html.escape(doc.title)
    grid_html = _page_html(doc, page, page_number=page_number)

    sheet_inner = theme_obj.render_wrapper(
        title=escaped_doc_title,
        grid=grid_html,
        page_note=page_note,
    )
    body = f'<div class="sheet {theme_obj.css_class} help-sheet">{sheet_inner}</div>'

    help_css = _help_css()
    full_css = (
        "\n".join((registry.base_css, theme_obj.css, help_css))
        .replace("__WIDTH__", str(width))
        .replace("__COLS__", "1")
    )
    return compose_document(body=body, css=full_css)


async def render_help_images(
    doc: HelpDoc,
    *,
    theme: str | None = None,
) -> tuple[str, list[bytes]]:
    """渲染单张完整帮助卡片图片，返回 (解析后的主题名, [PNG字节流])。"""
    registry = get_theme_registry()
    target_theme = theme or get_render_settings().theme
    resolved_theme_name = registry.resolve_theme_name(target_theme)

    page: HelpPage = tuple(
        HelpPageSection(section=s, items=tuple(s.items), continued=False) for s in doc.sections
    )
    pages = [page] if doc.sections else [()]
    page_count = 1
    doc_hash = hashlib.sha1(doc.to_text().encode("utf-8")).hexdigest()

    cache_key = (resolved_theme_name, 1, 1, doc_hash)
    async with _CACHE_LOCK:
        cached = _IMAGE_CACHE.get(cache_key)
        if cached is not None:
            _IMAGE_CACHE.move_to_end(cache_key)
            return resolved_theme_name, [cached]

    html_doc = build_help_html(
        doc,
        pages[0],
        page_number=1,
        page_count=page_count,
        theme=resolved_theme_name,
        width=HELP_WIDTH,
    )
    png = await screenshot_html(html_doc, width=HELP_WIDTH)

    async with _CACHE_LOCK:
        _IMAGE_CACHE[cache_key] = png
        _IMAGE_CACHE.move_to_end(cache_key)
        while len(_IMAGE_CACHE) > MAX_CACHE_ENTRIES:
            _IMAGE_CACHE.popitem(last=False)

    return resolved_theme_name, [png]


def clear_help_cache() -> None:
    """清空帮助图缓存与帮助样式缓存。"""
    global _HELP_CSS_CACHE
    _IMAGE_CACHE.clear()
    _HELP_CSS_CACHE = None
