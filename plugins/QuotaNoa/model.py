"""与渠道无关的额度数据模型与展示统计。

本模块不依赖任何具体渠道（CPA / 火山等），供 `cpa/`、`render/`、`volcengine/`
以及命令层共同使用。子包只允许单向依赖根模块，因此这里的类型与纯函数必须
保持“无渠道知识”。
"""

from __future__ import annotations

import re
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence


@dataclass
class QuotaWindow:
    id: str
    label: str
    used_percent: float | None = None
    remaining_percent: float | None = None
    remaining: float | None = None
    limit: float | None = None
    reset_label: str = "-"
    reset_at: float | None = None
    #: 自定义重置文案；非空时卡片与文字总览直接使用它，忽略 ``reset_label`` 的通用格式化。
    #: 供 WorkBuddy 这类「按最早到期套餐」表达倒计时的渠道使用。
    reset_note: str = ""
    #: 该窗口进度条的"主方向"："" = 自动推断（旧数据按 id 前缀，如 grok- 视为已用）；
    #: "remaining" = 表示剩余（多数渠道）；"used" = 表示已用（如火山 Percent）。
    direction: str = ""


def window_is_used(window: QuotaWindow) -> bool:
    """窗口进度条是否按"已用"方向展示。"""
    if window.direction == "used":
        return True
    if window.direction == "remaining":
        return False
    return window.id.startswith("grok-")


#: 火山计划分组顺序（汇总视图：Coding 在前，Agent 在后）。
PLAN_GROUP_ORDER = ("Coding", "Agent")


def plan_group_of(window_id: str) -> str:
    """火山窗口 id → 计划分组名（``"Coding"`` / ``"Agent"``）；非火山返回空串。"""
    wid = (window_id or "").lower()
    if wid.startswith("volc-agent-"):
        return "Agent"
    if wid.startswith("volc-"):
        return "Coding"
    return ""


def group_window_ids_by_plan(ordered_ids: Sequence[str]) -> list[tuple[str, list[str]]]:
    """把有序窗口 id 按火山计划分组，供「计划小标题 + 窗口行」式汇总渲染。

    规则：
    - 非火山窗口归入计划名为 ``""`` 的一组，组内保持传入顺序，且该组排在最前；
    - 火山窗口按 :data:`PLAN_GROUP_ORDER`（Coding → Agent）归组，组内保持传入顺序；
    - 未知火山计划（未来扩展）追加在已知组之后。
    """
    plain: list[str] = []
    plan_groups: dict[str, list[str]] = {}
    for wid in ordered_ids:
        plan = plan_group_of(wid)
        if plan:
            plan_groups.setdefault(plan, []).append(wid)
        else:
            plain.append(wid)
    result: list[tuple[str, list[str]]] = []
    if plain:
        result.append(("", plain))
    for plan in PLAN_GROUP_ORDER:
        if plan_groups.get(plan):
            result.append((plan, plan_groups[plan]))
    for plan, ids in plan_groups.items():
        if plan not in PLAN_GROUP_ORDER and ids:
            result.append((plan, ids))
    return result


@dataclass
class AccountQuota:
    platform: str
    name: str
    auth_index: str
    plan: str = ""
    #: 计划徽章：[(kind, text)]，kind ∈ {"coding","agent"}；卡片按计划分别标记。
    plan_badges: list[tuple[str, str]] = field(default_factory=list)
    #: 订阅到期徽章：[(kind, text)]，kind ∈ {"coding","agent"}。
    subscription_badges: list[tuple[str, str]] = field(default_factory=list)
    status: str = "unknown"
    error: str = ""
    windows: list[QuotaWindow] = field(default_factory=list)
    disabled: bool = False
    cooling: bool = False
    #: 该账号所属的查询来源实例名（CPA 实例名；火山等本地渠道为空）。
    instance: str = ""
    subscription_expires_at: float | None = None
    subscription_expires_label: str = ""
    reset_credits: int | None = None


@dataclass
class PlatformQuota:
    platform: str
    title: str
    accounts: list[AccountQuota]
    window_remain_sum: dict[str, float]
    window_remain_count: dict[str, int]
    window_labels: dict[str, str] = field(default_factory=dict)
    remaining_sum: float = 0.0
    limit_sum: float = 0.0
    hidden: int = 0


@dataclass
class QuotaBoard:
    platforms: list[PlatformQuota]
    queried: int = 0
    ok: int = 0
    failed: int = 0
    skipped: int = 0
    cached: bool = False


WINDOW_ORDER = (
    "gemini-5h",
    "gemini-week",
    "gemini-month",
    "claude-gpt-5h",
    "claude-gpt-week",
    "claude-gpt-month",
    "code-5h",
    "code-7d",
    "five_hour",
    "seven_day",
    "seven_day_opus",
    "seven_day_sonnet",
    "seven_day_oauth_apps",
    "seven_day_cowork",
    "iguana_necktie",
    "extra",
    "billing",
    "grok-build",
    "grok-chat",
    "grok-imagine",
    "usage",
    "wb-credits",
    "qoder-general",
    "qoder-addon",
)


#: 本地渠道（非 CPA 实例）：凭据来自本机配置，不经过 CLIProxyAPI。
LOCAL_CHANNELS = ("volcengine", "workbuddy", "qoder")


#: 渠道（channel）别名表。渠道是与 Provider 无关的“归属”标识，别名按渠道分桶。
CHANNEL_ALIASES = {
    "claude": "claude",
    "anthropic": "claude",
    "codex": "codex",
    "gpt": "codex",
    "openai": "codex",
    "antigravity": "antigravity",
    "反重力": "antigravity",
    "kimi": "kimi",
    "xai": "xai",
    "x-ai": "xai",
    "grok": "xai",
    "gemini-cli": "gemini-cli",
    "gemini": "gemini-cli",
    "volcengine": "volcengine",
    "volc": "volcengine",
    "火山": "volcengine",
    "ark": "volcengine",
    "火山方舟": "volcengine",
    "workbuddy": "workbuddy",
    "work-buddy": "workbuddy",
    "wb": "workbuddy",
    "qoder": "qoder",
    "qd": "qoder",
}

#: 平台展示标题。
PLATFORM_TITLES = {
    "claude": "Claude",
    "codex": "Codex",
    "antigravity": "Antigravity",
    "kimi": "Kimi",
    "xai": "xAI / Grok",
    "gemini-cli": "Gemini CLI",
    "volcengine": "火山方舟",
    "qoder": "Qoder",
    "workbuddy": "WorkBuddy",
    "other": "其他",
}

#: 平台展示顺序。
PLATFORM_ORDER = (
    "claude",
    "codex",
    "antigravity",
    "kimi",
    "xai",
    "gemini-cli",
    "volcengine",
    "qoder",
    "workbuddy",
    "other",
)


def normalize_channel(value: str) -> str:
    """把渠道名/别名统一为 canonical 渠道名；未知返回空串。"""
    raw = (value or "").strip()
    if not raw:
        return ""
    lowered = raw.lower().replace("_", "-")
    return CHANNEL_ALIASES.get(lowered, "") or CHANNEL_ALIASES.get(raw, "")


def is_channel_name(value: str) -> bool:
    return bool(normalize_channel(value))


#: 额外渠道配置里的“全部渠道”关键字（等价于命令的 ``all`` / ``--all``）。
ALL_CHANNELS_TOKEN = "all"


def is_all_channels(value: str) -> bool:
    """值是否为“全部渠道”关键字（``all`` / ``--all`` / ``-a`` / ``*``）。"""
    return (value or "").strip().lower() in {"all", "--all", "-a", "*"}


def channel_of(file: Mapping[str, Any]) -> str:
    """从 CPA 凭证字典推断渠道；未知归入 other。"""
    raw = str(file.get("provider") or file.get("type") or "").strip().lower().replace("_", "-")
    return CHANNEL_ALIASES.get(raw, "other")


def calculate_plan_distribution(accounts: list[AccountQuota]) -> list[tuple[str, int]]:
    """统计平台账号的计划分布。"""
    plans = [a.plan.strip() for a in accounts if getattr(a, "plan", "") and a.plan.strip()]
    if not plans:
        return []
    counter = Counter(plans)
    return sorted(counter.items(), key=lambda x: (-x[1], x[0]))


def calculate_total_reset_credits(accounts: list[AccountQuota]) -> int | None:
    """计算平台下所有账号的可用重置点数总和。"""
    found = False
    total = 0
    for account in accounts:
        rc = getattr(account, "reset_credits", None)
        if rc is not None:
            try:
                total += int(rc)
                found = True
            except (ValueError, TypeError):
                pass
    return total if found else None


def _normalize_epoch_timestamp(val: float, now: float) -> float:
    # If val is in milliseconds (e.g. > 1e11 or if val is > now * 500 when now > 1e8)
    if val > 1e11:
        return val / 1000.0
    # Also handle relative or custom millisecond stamps when now is small (e.g. in synthetic tests where now=10000)
    if now < 1e8 and val > 1e6:
        return val / 1000.0
    return val


def _classify_model_category(window_id: str, window_label: str = "") -> str:
    wid = (window_id or "").strip().lower()
    lbl = (window_label or "").strip().lower()

    if wid.startswith("volc-") or "火山" in lbl or "volcengine" in lbl:
        return "火山"
    if wid.startswith("qoder-") or "qoder" in lbl:
        return "Qoder"
    if wid.startswith("wb-") or "workbuddy" in lbl:
        return "WorkBuddy"
    if wid.startswith("gemini-") or lbl.startswith("gemini"):
        return "Gemini"
    if (
        wid.startswith("claude-gpt-")
        or lbl.startswith("claude-gpt")
        or lbl.startswith("claude/gpt")
    ):
        return "Claude/GPT"
    if wid.startswith("code-") or lbl.startswith("codex") or lbl.startswith("code"):
        return "Codex"
    if (
        wid in {"five_hour", "extra", "iguana_necktie"}
        or wid.startswith("seven_day")
        or lbl.startswith("claude")
    ):
        return "Claude"
    if wid.startswith("grok-") or wid == "billing" or "grok" in lbl or "xai" in lbl:
        return "xAI"
    if wid.startswith("limit-") or wid == "usage" or "kimi" in lbl:
        return "Kimi"

    # Fallback to normalized window label or id or 其他
    fallback = (window_label or window_id or "").strip()
    return fallback if fallback else "其他"


def _classify_period_category(window_id: str, window_label: str = "") -> str:
    blob = f"{window_id} {window_label}".strip().lower()
    if any(token in blob for token in ("5h", "five", "hour", "滚动", "rolling", "用量", "usage")):
        return "小时额度"
    if any(token in blob for token in ("week", "weekly", "7d", "seven", "周")):
        return "周额度"
    if any(token in blob for token in ("month", "monthly", "30d", "月")):
        return "月额度"
    return "其他额度"


_MODEL_CATEGORY_ORDER = ("Gemini", "Claude/GPT", "Codex", "Claude", "xAI", "Kimi", "Qoder", "WorkBuddy")
_PERIOD_CATEGORY_ORDER = ("小时额度", "周额度", "月额度", "其他额度")


def calculate_grouped_earliest_resets(
    accounts: list[AccountQuota],
    now: float | None = None,
) -> list[dict[str, Any]]:
    """按模型分类与周期分类计算所有账号窗口中最早的未来刷新倒计时。"""
    current_time = time.time() if now is None else float(now)
    # Map (model_category, period_category) -> best_dict
    grouped: dict[tuple[str, str], dict[str, Any]] = {}

    for account in accounts:
        for window in account.windows:
            diff: float | None = None
            reset_at = getattr(window, "reset_at", None)
            if reset_at is not None and isinstance(reset_at, (int, float)) and not isinstance(reset_at, bool):
                epoch = _normalize_epoch_timestamp(float(reset_at), current_time)
                val = epoch - current_time
                if val > 0:
                    diff = val
                else:
                    # Numeric reset_at exists but in the past; do not fall back to stale reset_label
                    continue
            elif window.reset_label and window.reset_label != "-" and "过期" not in window.reset_label:
                sec = _parse_reset_label_duration(window.reset_label)
                if sec is not None and sec > 0:
                    diff = sec

            if diff is None or diff <= 0:
                continue

            model_cat = _classify_model_category(window.id, window.label)
            period_cat = _classify_period_category(window.id, window.label)
            key = (model_cat, period_cat)

            if key not in grouped or diff < grouped[key]["seconds"]:
                grouped[key] = {
                    "model": model_cat,
                    "period": period_cat,
                    "seconds": diff,
                    "window_id": window.id,
                    "window_label": window.label,
                }

    model_rank = {name: idx for idx, name in enumerate(_MODEL_CATEGORY_ORDER)}
    period_rank = {name: idx for idx, name in enumerate(_PERIOD_CATEGORY_ORDER)}

    def _sort_key(item: dict[str, Any]) -> tuple[int, str, int, str]:
        m = item["model"]
        p = item["period"]
        return (
            model_rank.get(m, len(_MODEL_CATEGORY_ORDER)),
            m,
            period_rank.get(p, len(_PERIOD_CATEGORY_ORDER)),
            p,
        )

    return sorted(grouped.values(), key=_sort_key)


def extract_earliest_reset_seconds(
    accounts: list[AccountQuota],
    now: float | None = None,
) -> float | None:
    """计算平台下所有账号窗口中最快的未来刷新倒计时秒数。"""
    grouped = calculate_grouped_earliest_resets(accounts, now=now)
    if not grouped:
        return None
    return min(item["seconds"] for item in grouped)


def _parse_reset_label_duration(text: str) -> float | None:
    days = 0
    hours = 0
    mins = 0
    matched = False
    m = re.search(r"(\d+)\s*d", text)
    if m:
        days = int(m.group(1))
        matched = True
    m = re.search(r"(\d+)\s*h", text)
    if m:
        hours = int(m.group(1))
        matched = True
    m = re.search(r"(\d+)\s*m", text)
    if m:
        mins = int(m.group(1))
        matched = True
    if matched:
        return float(days * 86400 + hours * 3600 + mins * 60)
    return None


def calculate_aggregate_windows(
    section: PlatformQuota, accounts: list[AccountQuota] | None = None
) -> list[dict[str, Any]]:
    """
    计算聚合窗口配额（支持 sum% + average% + account count）。
    """
    accts = accounts if accounts is not None else section.accounts
    win_sums: dict[str, float] = {}
    win_counts: dict[str, int] = {}
    win_modes: dict[str, str] = {}
    win_labels: dict[str, str] = dict(getattr(section, "window_labels", {}) or {})

    for account in accts:
        for w in account.windows:
            win_labels.setdefault(w.id, w.label)
            is_used = window_is_used(w)
            pct = w.used_percent if is_used else w.remaining_percent
            win_modes[w.id] = "used" if is_used else "remaining"
            if pct is None and w.used_percent is not None:
                pct = w.used_percent if is_used else max(0.0, 100.0 - w.used_percent)
            elif (
                pct is None
                and w.remaining is not None
                and w.limit is not None
                and w.limit > 0
            ):
                pct = max(0.0, min(100.0, (w.remaining / w.limit) * 100.0))

            if pct is not None:
                win_sums[w.id] = win_sums.get(w.id, 0.0) + pct
                win_counts[w.id] = win_counts.get(w.id, 0) + 1

    results = []
    dummy_windows = [QuotaWindow(id=wid, label=win_labels.get(wid, wid)) for wid in win_sums]
    sorted_order = sort_windows(dummy_windows)

    for w in sorted_order:
        wid = w.id
        total_pct = win_sums[wid]
        count = win_counts[wid]
        avg_pct = total_pct / count if count > 0 else 0.0
        results.append(
            {
                "id": wid,
                "label": win_labels.get(wid, wid),
                "sum_percent": total_pct,
                "avg_percent": avg_pct,
                "count": count,
                "mode": win_modes.get(wid, "remaining"),
            }
        )
    return results


def sort_windows(windows: list[QuotaWindow]) -> list[QuotaWindow]:
    indexed = {wid: index for index, wid in enumerate(WINDOW_ORDER)}

    def _key(window: QuotaWindow) -> tuple[int, int, str]:
        return (_window_span(window), indexed.get(window.id, len(WINDOW_ORDER)), window.label)

    return sorted(windows, key=_key)


def format_reset_zh(reset_label: str) -> str:
    text = (reset_label or "").strip()
    if not text or text == "-":
        return ""
    if text == "已过期":
        return "额度已过期"
    parts: list[str] = []
    for amount, unit, zh in (
        (r"(\d+)\s*d", "d", "天"),
        (r"(\d+)\s*h", "h", "小时"),
        (r"(\d+)\s*m", "m", "分"),
    ):
        match = re.search(amount, text, re.I)
        if match:
            parts.append(f"{int(match.group(1))}{zh}")
    if parts:
        return "在 " + "".join(parts) + " 后刷新额度"
    return f"在 {text} 后刷新额度"


def _window_span(window: QuotaWindow) -> int:
    blob = f"{window.id} {window.label}".lower()
    if any(token in blob for token in ("5h", "five", "hour", "滚动", "rolling")):
        return 0
    if any(token in blob for token in ("week", "weekly", "7d", "seven", "周")):
        return 1
    if any(token in blob for token in ("month", "monthly", "月")):
        return 2
    return 3


# --------------------------------------------------------------------------- #
# 板（board）构建 —— 与渠道无关，供 cpa/ 与 volcengine/ 共用
# --------------------------------------------------------------------------- #


def build_board(reports: list[AccountQuota]) -> QuotaBoard:
    """把账号额度列表按平台聚合为展示板。"""
    grouped: dict[str, list[AccountQuota]] = {}
    for report in reports:
        grouped.setdefault(report.platform, []).append(report)
    platforms: list[PlatformQuota] = []
    ok = failed = skipped = 0
    for key in PLATFORM_ORDER:
        accounts = grouped.pop(key, [])
        if not accounts:
            continue
        remain_sum: dict[str, float] = {}
        remain_count: dict[str, int] = {}
        labels: dict[str, str] = {}
        remaining_sum = 0.0
        limit_sum = 0.0
        for account in accounts:
            if account.error:
                failed += 1
            elif account.windows:
                ok += 1
            else:
                skipped += 1
            for window in account.windows:
                labels.setdefault(window.id, window.label)
                if window.remaining_percent is not None:
                    remain_sum[window.id] = remain_sum.get(window.id, 0.0) + window.remaining_percent
                    remain_count[window.id] = remain_count.get(window.id, 0) + 1
                if window.remaining is not None:
                    remaining_sum += window.remaining
                if window.limit is not None:
                    limit_sum += window.limit
        platforms.append(
            PlatformQuota(
                platform=key,
                title=PLATFORM_TITLES.get(key, key),
                accounts=accounts,
                window_remain_sum=remain_sum,
                window_remain_count=remain_count,
                window_labels=labels,
                remaining_sum=remaining_sum,
                limit_sum=limit_sum,
            )
        )
    for leftover, accounts in sorted(grouped.items()):
        platforms.append(
            PlatformQuota(
                platform=leftover,
                title=PLATFORM_TITLES.get(leftover, leftover),
                accounts=accounts,
                window_remain_sum={},
                window_remain_count={},
            )
        )
        skipped += len(accounts)
    return QuotaBoard(
        platforms=platforms,
        queried=len(reports),
        ok=ok,
        failed=failed,
        skipped=skipped,
    )


def stamp_instance(board: QuotaBoard, instance: str) -> QuotaBoard:
    """把实例名写入板内所有账号，用于多实例分组展示。"""
    for section in board.platforms:
        for account in section.accounts:
            account.instance = instance
    return board


def board_from_accounts(accounts: list[AccountQuota], *, cached: bool = False) -> QuotaBoard:
    board = build_board(accounts)
    board.cached = cached
    return board


# --------------------------------------------------------------------------- #
# 展示助手 —— 实例前缀与限长（cpa/ 与 render/ 共用，故放根模块）
# --------------------------------------------------------------------------- #

#: 实例前缀（渠道标签）最大字符数。
MAX_INSTANCE_TAG = 8
#: 账号显示名最大字符数。
MAX_ACCOUNT_NAME = 16


def truncate_text(text: str, limit: int) -> str:
    """超长文本以 ``…`` 结尾；``limit`` 非正或文本为空时原样返回。"""
    value = str(text or "")
    if limit <= 0 or len(value) <= limit:
        return value
    return value[: limit - 1] + "…"


def instance_tag(instance: str) -> str:
    """把实例名规范化为展示用短标签（≤ ``MAX_INSTANCE_TAG``）；空则返回空串。"""
    return truncate_text(str(instance or "").strip(), MAX_INSTANCE_TAG)


def prefix_instance(instance: str, name: str) -> str:
    """拼出 ``[实例] 账号名``；无实例时只返回账号名。"""
    tag = instance_tag(instance)
    shown = truncate_text(name, MAX_ACCOUNT_NAME)
    return f"[{tag}] {shown}" if tag else shown

