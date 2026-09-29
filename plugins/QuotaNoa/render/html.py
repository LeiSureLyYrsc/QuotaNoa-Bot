from __future__ import annotations

import asyncio
import base64
import html
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..model import (
    AccountQuota,
    MAX_ACCOUNT_NAME,
    PlatformQuota,
    QuotaBoard,
    QuotaWindow,
    calculate_aggregate_windows,
    calculate_grouped_earliest_resets,
    calculate_plan_distribution,
    calculate_total_reset_credits,
    extract_earliest_reset_seconds,
    format_reset_zh,
    group_window_ids_by_plan,
    instance_tag,
    sort_windows,
    truncate_text,
    window_is_used,
)

from .highlight import highlight_html
from .themes import get_theme_registry

DEFAULT_CARDS_PER_ROW = 3
GRID_ROWS_PER_IMAGE = 3
CARDS_PER_IMAGE = 8


def _chunks(items: list[Any], size: int) -> list[list[Any]]:
    """向后兼容辅助函数。"""
    return [items[index : index + size] for index in range(0, len(items), size)] or [[]]

_ASSETS = Path(__file__).resolve().parent / "assets"
_TEMPLATE = (_ASSETS / "quota.html").read_text(encoding="utf-8")
_BRANDS_DIR = _ASSETS / "brands"

_BADGES = {
    "claude": "CL",
    "codex": "CX",
    "antigravity": "AG",
    "kimi": "KM",
    "xai": "xAI",
    "gemini-cli": "GM",
    "volcengine": "VOLC",
    "qoder": "QD",
    "workbuddy": "WB",
    "other": "?",
}

#: 计划徽章 kind -> 主题内已有的徽章样式类（跨全部主题可用）。
_PLAN_BADGE_CLASS = {"coding": "badge-plan", "agent": "badge-credits"}

_GROUP_TITLES = {
    "gemini": "Gemini Models",
    "claude-gpt": "Claude and GPT Models",
    "code": "Codex",
    "claude": "Claude",
    "xai": "xAI",
    "kimi": "Usage",
    "volc": "Coding Plan",
    "volc-agent": "Agent Plan",
    "wb": "积分",
    "qoder": "积分",
    "other": "Quota",
}

_SUMMARY_PLAN_HEADINGS = {"Coding": "Coding Plan", "Agent": "Agent Plan"}

_BRAND_FILES = {
    "claude": "claude.svg",
    "codex": "codex.svg",
    "antigravity": "antigravity.png",
    "kimi": "kimi.svg",
    "xai": "grok.svg",
    "gemini-cli": "gemini.svg",
}

_lock = asyncio.Lock()
_playwright: Any = None
_browser: Any = None


class RenderError(Exception):
    """出图失败，消息可直接发给管理员。"""


def get_platform_icon_uri(platform: str) -> str:
    """返回本地 vendored 图标的 data URI，如果未找到则返回空字符串。"""
    filename = _BRAND_FILES.get(platform)
    if not filename:
        return ""
    icon_path = _BRANDS_DIR / filename
    if not icon_path.exists():
        return ""
    try:
        raw = icon_path.read_bytes()
        mime = "image/svg+xml" if filename.endswith(".svg") else "image/png"
        b64 = base64.b64encode(raw).decode("ascii")
        return f"data:{mime};base64,{b64}"
    except Exception:
        return ""


def get_render_settings_adapter() -> dict[str, Any]:
    """读取渲染设置，返回 canonical theme 与 cards_per_row (1..6)。

    直接依赖 ``render.settings``（不再静默回退），避免配置错误被掩盖。
    """
    from . import settings

    res = settings.get_render_settings()
    return _normalize_settings(res.to_dict())


def _normalize_settings(raw: dict[str, Any]) -> dict[str, Any]:
    registry = get_theme_registry()
    raw_theme = str(raw.get("theme") or "default").strip().lower()
    theme = registry.resolve_theme_name(raw_theme)

    try:
        cols = int(raw.get("cards_per_row", DEFAULT_CARDS_PER_ROW))
    except (ValueError, TypeError):
        cols = DEFAULT_CARDS_PER_ROW
    cols = max(1, min(6, cols))

    return {"theme": theme, "cards_per_row": cols}


def paginate_accounts(
    accounts: list[AccountQuota],
    cards_per_row: int = DEFAULT_CARDS_PER_ROW,
    rows_per_page: int = GRID_ROWS_PER_IMAGE,
) -> list[list[AccountQuota]]:
    """
    纯分页辅助函数：
    - cards_per_row in 1..6
    - 第 1 页容量为 (rows * cols - 1)，因为 Summary 卡片占据第 1 个网格单元
    - 第 2 页及后续页容量为 (rows * cols)
    """
    cols = max(1, min(6, cards_per_row))
    rows = max(1, rows_per_page)
    full_cap = rows * cols
    page1_cap = max(1, full_cap - 1)

    if not accounts:
        return [[]]

    pages: list[list[AccountQuota]] = []
    # 第 1 页
    pages.append(accounts[:page1_cap])
    remaining = accounts[page1_cap:]

    # 后续页
    while remaining:
        pages.append(remaining[:full_cap])
        remaining = remaining[full_cap:]

    return pages


def calculate_canvas_width(cards_per_row: int) -> int:
    """根据列数动态计算合适的画布宽度。"""
    cols = max(1, min(6, cards_per_row))
    # 单卡宽度约为 240~270px 左右，外加 padding/gap
    card_widths = {
        1: 420,
        2: 640,
        3: 880,
        4: 1120,
        5: 1360,
        6: 1600,
    }
    return card_widths.get(cols, 1120)


def _format_exact_earliest_reset(seconds: float | None) -> str:
    """
    格式化最早刷新文本：
    当 seconds 有效时返回：最快于 X小时X分 后刷新额度 (如果>0天则 X天X小时)
    """
    if seconds is None:
        return ""
    if seconds <= 0:
        return "最快于 0分 后刷新额度"

    secs = int(seconds)
    days, rem = divmod(secs, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60

    if days > 0:
        part = f"{days}天{hours}小时" if hours else f"{days}天"
    elif hours > 0:
        part = f"{hours}小时{minutes}分" if minutes else f"{hours}小时"
    else:
        part = f"{max(minutes, 1)}分"

    return f"最快于 {part} 后刷新额度"


def build_summary_card_html(
    section: PlatformQuota,
    all_accounts: list[AccountQuota],
    theme: str,
) -> str:
    """构建首卡 Summary 卡片 HTML（作为 Grid 的第一个单元格）。"""
    icon_uri = get_platform_icon_uri(section.platform)
    if icon_uri:
        icon_html = f'<img class="brand-icon" src="{icon_uri}" alt="{html.escape(section.platform)}" />'
    else:
        badge_text = _BADGES.get(section.platform, "?")
        icon_html = f'<span class="brand-badge-fallback">{html.escape(badge_text)}</span>'

    # 1. 计划分布 (Pro × N)
    plan_dist = calculate_plan_distribution(all_accounts)
    plan_chips_html = ""
    if plan_dist:
        chips = [
            f'<span class="badge badge-plan">{html.escape(plan)} × {count}</span>'
            for plan, count in plan_dist
        ]
        plan_chips_html = f'<div class="card-meta-row">{"".join(chips)}</div>'

    # 2. 聚合配额 (SUM percent + average percent + count)
    agg_windows = calculate_aggregate_windows(section, all_accounts)
    stats_rows = []
    by_id = {item["id"]: item for item in agg_windows}
    for plan, window_ids in group_window_ids_by_plan([item["id"] for item in agg_windows]):
        if plan:
            heading = _SUMMARY_PLAN_HEADINGS.get(plan, f"{plan} Plan")
            stats_rows.append(
                f'<div class="summary-plan-heading">{html.escape(heading)}</div>'
            )
        for wid in window_ids:
            agg = by_id.get(wid)
            if not agg:
                continue
            # 显示格式：Gemini 5h 344% (均 86% · 4号)
            is_used = agg.get("mode") == "used"
            sum_text = f'已使用 {agg["sum_percent"]:.0f}%' if is_used else f'{agg["sum_percent"]:.0f}%'
            avg_text = f'均已使用 {agg["avg_percent"]:.0f}%' if is_used else f'均 {agg["avg_percent"]:.0f}%'
            stats_rows.append(
                f'<div class="summary-stat-row">'
                f'<span class="summary-stat-label">{html.escape(agg["label"])}</span>'
                f'<span class="summary-stat-val">'
                f'<span class="sum-pct">{sum_text}</span> '
                f'<span class="avg-cnt">({avg_text} · {agg["count"]}号)</span>'
                f'</span>'
                f'</div>'
            )
    stats_html = (
        f'<div class="summary-stats-grid">{"".join(stats_rows)}</div>' if stats_rows else ""
    )

    # 3. 最早刷新多分组文本列表
    grouped_resets = calculate_grouped_earliest_resets(all_accounts)
    reset_list_html = ""
    if grouped_resets:
        rows_html = []
        for grp in grouped_resets:
            text = _format_exact_earliest_reset(grp["seconds"])
            if not text:
                continue
            kind_label = f'{grp["model"]} · {grp["period"]}：'
            rows_html.append(
                f'<div class="summary-reset-row">'
                f'<span class="summary-reset-kind">{html.escape(kind_label)}</span>'
                f'<span class="summary-reset-time">{html.escape(text)}</span>'
                f'</div>'
            )
        if rows_html:
            reset_list_html = f'<div class="summary-reset-list">{"".join(rows_html)}</div>'

    # 4. Total reset credits
    total_rc = calculate_total_reset_credits(all_accounts)
    rc_html = ""
    if total_rc is not None:
        rc_html = f'<span class="badge badge-credits">主动刷新次数: {total_rc}</span>'

    header_meta = f'<div class="card-meta-row">{rc_html}</div>' if rc_html else ""

    hidden = int(getattr(section, "hidden", 0) or 0)
    shown = len(all_accounts)
    count_text = f"{shown + hidden} 个账号 · 仅显示 {shown}" if hidden > 0 else f"{shown} 个账号"

    return (
        f'<article class="card summary-card">'
        f'<div class="card-header">'
        f'<div class="title-row">'
        f'<div class="summary-brand-row">{icon_html}<h2 class="card-title summary-title">{html.escape(section.title)}</h2></div>'
        f'<span class="summary-account-count">{count_text}</span>'
        f'</div>'
        f'{plan_chips_html}'
        f'{header_meta}'
        f'</div>'
        f'<div class="card-content">'
        f'{stats_html}'
        f'{reset_list_html}'
        f'</div>'
        f'</article>'
    )


def _card_html(account: AccountQuota) -> str:
    """构建账号卡片 HTML。"""
    badges = []

    # 冷却中 (中文)
    if account.cooling:
        badges.append('<span class="badge badge-cooling">冷却中</span>')
    if account.disabled:
        badges.append('<span class="badge badge-disabled">已停用</span>')

    # 计划徽章：火山按 Coding/Agent 分开标记；其它渠道单枚。
    plan_badges = getattr(account, "plan_badges", None) or []
    if plan_badges:
        for kind, text in plan_badges:
            cls = _PLAN_BADGE_CLASS.get(str(kind), "badge-plan")
            badges.append(f'<span class="badge {cls}">{html.escape(str(text))}</span>')
    elif account.plan:
        badges.append(f'<span class="badge badge-plan">{html.escape(account.plan)}</span>')

    # 订阅到期：火山 Coding/Agent 各一枚；其它渠道沿用单枚。
    sub_badges = getattr(account, "subscription_badges", None) or []
    if sub_badges:
        for _kind, text in sub_badges:
            badges.append(f'<span class="badge badge-warn">{html.escape(str(text))}</span>')
    else:
        sub_label = getattr(account, "subscription_expires_label", None)
        if sub_label:
            badges.append(f'<span class="badge badge-warn">到期: {html.escape(str(sub_label))}</span>')

    # Codex 刷新次数
    reset_credits = getattr(account, "reset_credits", None)
    if reset_credits is not None:
        badges.append(f'<span class="badge badge-credits">主动刷新 {reset_credits} 次</span>')

    title_badges_html = (
        f'<div class="card-badges">{"".join(badges)}</div>' if badges else ""
    )

    # 标题 = [实例] 账号名（实例前缀单独包 span，便于独立限宽/省略）。
    tag = instance_tag(account.instance)
    shown_name = truncate_text(account.name, MAX_ACCOUNT_NAME)
    tag_html = f'<span class="card-title-tag">[{html.escape(tag)}]</span>' if tag else ""
    head = (
        f'<div class="card-header">'
        f'<div class="title-row">'
        f'<h3 class="card-title" title="{html.escape(account.name)}">{tag_html}{html.escape(shown_name)}</h3>'
        f'{title_badges_html}'
        f'</div>'
        f'</div>'
    )

    if account.error:
        return (
            f'<article class="card">'
            f'{head}'
            f'<div class="card-content"><div class="card-err-box">{highlight_html(account.error)}</div></div>'
            f'</article>'
        )

    if not account.windows:
        status = account.status or "unknown"
        status_text = "冷却中" if status == "cooling" else status
        return (
            f'<article class="card">'
            f'{head}'
            f'<div class="card-content"><div class="bar-reset-hint">{html.escape(status_text)}（无上游配额）</div></div>'
            f'</article>'
        )

    groups = "".join(
        _group_html(title, windows)
        for title, windows in _grouped_windows(account.windows)
    )
    return f'<article class="card">{head}<div class="card-content">{groups}</div></article>'


def _group_html(title: str, windows: list[QuotaWindow]) -> str:
    rows = "".join(_bar_html(window) for window in windows)
    is_plan = title in ("Coding Plan", "Agent Plan")
    title_cls = "group-title group-title-plan" if is_plan else "group-title"
    return f'<section class="quota-group"><h4 class="{title_cls}">{html.escape(title)}</h4>{rows}</section>'


def _bar_html(window: QuotaWindow) -> str:
    remain = window.remaining_percent
    used = window.used_percent
    is_grok_product = window_is_used(window)
    if remain is None and used is not None:
        remain = max(0.0, 100.0 - used)
    elif (
        remain is None
        and window.remaining is not None
        and window.limit is not None
        and window.limit > 0
    ):
        remain = max(0.0, min(100.0, (window.remaining / window.limit) * 100.0))

    display_percent = used if is_grok_product and used is not None else remain
    width = 0.0 if display_percent is None else max(0.0, min(100.0, display_percent))

    if is_grok_product and used is not None:
        value_text = f"已使用 {used:.0f}%"
    elif remain is not None:
        value_text = f"剩 {remain:.0f}%"
    elif used is not None:
        value_text = f"用 {used:.0f}%"
    elif window.remaining is not None and window.limit is not None:
        value_text = f"{window.remaining:.0f}/{window.limit:.0f}"
    else:
        value_text = "可用"

    zh_reset = window.reset_note or format_reset_zh(window.reset_label)
    reset_html = (
        f'<div class="bar-reset-hint">{html.escape(zh_reset)}</div>' if zh_reset else ""
    )

    bar_level_class = ""
    if is_grok_product and used is not None:
        if used >= 85:
            bar_level_class = "bar-low"
        elif used >= 65:
            bar_level_class = "bar-med"
    elif remain is not None:
        if remain <= 15:
            bar_level_class = "bar-low"
        elif remain <= 35:
            bar_level_class = "bar-med"

    return (
        f'<div class="bar-item {bar_level_class}">'
        f'<div class="bar-meta">'
        f'<span class="bar-label" title="{html.escape(window.label)}">{html.escape(window.label)}</span>'
        f'<span class="bar-val">{html.escape(value_text)}</span>'
        f'</div>'
        f'<div class="progress-track">'
        f'<div class="progress-fill" style="width: {width:.1f}%"></div>'
        f'</div>'
        f'{reset_html}'
        f'</div>'
    )


def _grouped_windows(
    windows: list[QuotaWindow],
) -> list[tuple[str, list[QuotaWindow]]]:
    grouped: dict[str, list[QuotaWindow]] = {}
    order: list[str] = []
    for window in windows:
        key = _group_key(window.id)
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(window)
    return [(_GROUP_TITLES.get(key, key), sort_windows(grouped[key])) for key in order]


def _group_key(window_id: str) -> str:
    if window_id.startswith("volc-agent-"):
        return "volc-agent"
    if window_id.startswith("volc-"):
        return "volc"
    if window_id.startswith("wb-"):
        return "wb"
    if window_id.startswith("qoder-"):
        return "qoder"
    if window_id.startswith("gemini-"):
        return "gemini"
    if window_id.startswith("claude-gpt-"):
        return "claude-gpt"
    if window_id.startswith("code-"):
        return "code"
    if window_id.startswith("grok-") or window_id == "billing":
        return "xai"
    if window_id.startswith("limit-") or window_id == "usage":
        return "kimi"
    if window_id in {
        "five_hour",
        "seven_day",
        "seven_day_opus",
        "seven_day_sonnet",
        "seven_day_oauth_apps",
        "seven_day_cowork",
        "iguana_necktie",
        "extra",
    }:
        return "claude"
    return "other"


def build_platform_html(
    section: PlatformQuota,
    accounts: list[AccountQuota] | None = None,
    *,
    page: int = 1,
    pages: int = 1,
    width: int | None = None,
    theme: str | None = None,
    cards_per_row: int | None = None,
) -> str:
    """
    构建平台配额 HTML。
    - theme: 主题名或别名（如 'default', 'shadcn', 'mac', 'md3', 'winxp', 'win7'）
    - cards_per_row: 1..6
    - page: 当前页码
    - pages: 总页码
    - Summary 卡片仅在 page == 1 时作为第 1 个网格单元插入。
    """
    settings = get_render_settings_adapter()
    registry = get_theme_registry()
    target_theme_name = theme or settings.get("theme") or "default"
    theme_obj = registry.get_theme(target_theme_name)

    cols = cards_per_row or settings.get("cards_per_row", DEFAULT_CARDS_PER_ROW)
    cols = max(1, min(6, int(cols)))

    canvas_w = width if width is not None else calculate_canvas_width(cols)
    cur_accounts = accounts if accounts is not None else section.accounts

    # 生成网格单元列表
    grid_cells = []
    if int(page) == 1:
        # Summary 卡片是第 1 个网格单元
        summary_cell = build_summary_card_html(section, section.accounts, theme_obj.name)
        grid_cells.append(summary_cell)

    # 账号卡片
    for acc in cur_accounts:
        grid_cells.append(_card_html(acc))

    grid_content = "".join(grid_cells)
    page_note = f'<div class="page-note">第 {page} / {pages} 页</div>' if pages > 1 else ""

    sheet_inner = theme_obj.render_wrapper(
        title=html.escape(section.title),
        grid=grid_content,
        page_note=page_note,
    )

    theme_class = theme_obj.css_class
    body = (
        f'<div class="sheet {theme_class} platform-{html.escape(section.platform)}">'
        f"{sheet_inner}"
        f"</div>"
    )

    full_css = (registry.base_css + "\n" + theme_obj.css).replace("__WIDTH__", str(canvas_w)).replace("__COLS__", str(cols))
    return compose_document(body=body, css=full_css)


def compose_document(*, body: str, css: str) -> str:
    """返回填充了 CSS 与 body 的完整 HTML 文档。"""
    return _TEMPLATE.replace("__CSS__", css).replace("__BODY__", body)


async def render_platform_images(section: PlatformQuota) -> list[bytes]:
    if not section.accounts:
        raise RenderError(f"{section.title} 没有可出图的账号。")

    settings = get_render_settings_adapter()
    theme = settings.get("theme", "default")
    cols = settings.get("cards_per_row", DEFAULT_CARDS_PER_ROW)

    pages_accounts = paginate_accounts(section.accounts, cards_per_row=cols, rows_per_page=GRID_ROWS_PER_IMAGE)
    total_pages = len(pages_accounts)
    canvas_w = calculate_canvas_width(cols)

    images: list[bytes] = []
    for index, accounts in enumerate(pages_accounts, start=1):
        html_doc = build_platform_html(
            section,
            accounts,
            page=index,
            pages=total_pages,
            width=canvas_w,
            theme=theme,
            cards_per_row=cols,
        )
        images.append(await screenshot_html(html_doc, width=canvas_w))
    return images


async def render_board_images(board: QuotaBoard) -> list[tuple[str, list[bytes]]]:
    results: list[tuple[str, list[bytes]]] = []
    for section in board.platforms:
        results.append((section.platform, await render_platform_images(section)))
    return results


async def close_renderer() -> None:
    global _playwright, _browser
    async with _lock:
        if _browser is not None:
            try:
                await _browser.close()
            except Exception:
                pass
            _browser = None
        if _playwright is not None:
            try:
                await _playwright.stop()
            except Exception:
                pass
            _playwright = None


async def screenshot_html(html_doc: str, width: int = 1080) -> bytes:
    async with _lock:
        browser = await _ensure_browser()
        context = await browser.new_context(
            viewport={"width": width + 32, "height": 800},
            device_scale_factor=2,
        )
        page = await context.new_page()
        try:
            await page.set_content(html_doc, wait_until="load")
            png = await page.locator("#root").screenshot(type="png")
        except Exception as exc:
            raise RenderError(f"截图失败：{exc}") from exc
        finally:
            await context.close()
    return bytes(png)


async def _screenshot(html_doc: str, width: int = 1080) -> bytes:
    """向后兼容别名。"""
    return await screenshot_html(html_doc, width=width)


def _dir_is_writable(path: Path) -> bool:
    """探测目录是否可写（创建后立即删除探针文件）。"""
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / f".quotanoa-write-probe-{os.getpid()}-{time.time_ns()}"
        probe.write_bytes(b"")
        probe.unlink()
        return True
    except OSError:
        return False


def _playwright_temp_dir() -> Path:
    """返回 Playwright 驱动可写的临时目录。

    某些 Windows 环境（安全软件 / 组策略 / %TEMP% 的 ACL 损坏）下，当前进程对
    ``%TEMP%`` 没有写权限，Playwright 的 Node 驱动会在 ``mkdtemp`` 时抛出
    ``EPERM: operation not permitted, mkdtemp '...\\playwright-artifacts-XXXXXX'``，
    导致无法出图。这里回退到项目内可写的目录。
    """
    candidates: list[Path] = []
    try:
        from ..state import config_file_path

        candidates.append(config_file_path().parent / "tmp")
    except Exception:
        pass
    candidates.append(Path.cwd() / "data" / "tmp")
    candidates.append(Path(__file__).resolve().parents[3] / ".tmp")
    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        candidates.append(Path(local_appdata) / "QuotaNoa" / "tmp")

    for candidate in candidates:
        if _dir_is_writable(candidate):
            return candidate
    raise RenderError(
        "无法找到可写的临时目录（%TEMP% 不可写）。请检查安全软件限制或手动设置 TEMP/TMP 环境变量。"
    )


def _ensure_playwright_temp() -> None:
    """在启动 Playwright 前保证 TEMP/TMP 指向可写目录。

    Playwright 通过 ``os.environ`` 复制环境启动 Node 驱动，因此必须在
    ``async_playwright().start()`` 之前设置。
    """
    current = os.environ.get("TEMP") or os.environ.get("TMP")
    if current and _dir_is_writable(Path(current)):
        return
    writable = _playwright_temp_dir()
    os.environ["TEMP"] = str(writable)
    os.environ["TMP"] = str(writable)


async def _ensure_browser() -> Any:
    global _playwright, _browser
    if _browser is not None:
        try:
            if _browser.is_connected():
                return _browser
        except Exception:
            _browser = None
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise RenderError("未安装 playwright。请 uv sync 后执行 playwright install chromium。") from exc
    try:
        if _playwright is None:
            _ensure_playwright_temp()
            _playwright = await async_playwright().start()
        _browser = await _playwright.chromium.launch(headless=True)
    except Exception as exc:
        message = str(exc)
        if "Executable doesn't exist" in message or "chromium" in message.lower():
            raise RenderError("未安装 Chromium。请执行：playwright install chromium") from exc
        raise RenderError(f"无法启动 Chromium：{exc}") from exc
    return _browser
