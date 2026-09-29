from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from .client import CPAError, ManagementClient, get_client
from .. import state
from ..config import CpaInstance
from ..model import (
    CHANNEL_ALIASES,
    PLATFORM_ORDER as _MODEL_PLATFORM_ORDER,
    PLATFORM_TITLES as _MODEL_PLATFORM_TITLES,
    WINDOW_ORDER,
    AccountQuota,
    PlatformQuota,
    QuotaBoard,
    QuotaWindow,
    board_from_accounts,
    build_board,
    calculate_aggregate_windows,
    calculate_grouped_earliest_resets,
    calculate_plan_distribution,
    calculate_total_reset_credits,
    extract_earliest_reset_seconds,
    format_reset_zh,
    group_window_ids_by_plan,
    prefix_instance,
    sort_windows,
    stamp_instance,
    window_is_used,
)
from .format import display_name, is_cooling, is_unhealthy

#: 平台别名/标题/顺序统一来自根模型（含 volcengine），避免多份来源。
PLATFORM_ALIASES = CHANNEL_ALIASES
PLATFORM_TITLES = _MODEL_PLATFORM_TITLES
PLATFORM_ORDER = _MODEL_PLATFORM_ORDER

#: 板构建逻辑已上移到根模型，供各渠道共用；保留旧名以兼容既有调用与测试。
_build_board = build_board

CLAUDE_WINDOWS = (
    ("five_hour", "5h"),
    ("seven_day", "7d"),
    ("seven_day_opus", "7d Opus"),
    ("seven_day_sonnet", "7d Sonnet"),
    ("seven_day_oauth_apps", "7d OAuth"),
    ("seven_day_cowork", "7d Cowork"),
    ("iguana_necktie", "7d Fable"),
)

ANTIGRAVITY_URLS = (
    "https://daily-cloudcode-pa.googleapis.com/v1internal:retrieveUserQuotaSummary",
    "https://daily-cloudcode-pa.sandbox.googleapis.com/v1internal:retrieveUserQuotaSummary",
    "https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuotaSummary",
)

ANTIGRAVITY_PLAN_URLS = (
    "https://daily-cloudcode-pa.googleapis.com/v1internal:loadCodeAssist",
    "https://cloudcode-pa.googleapis.com/v1internal:loadCodeAssist",
    "https://daily-cloudcode-pa.sandbox.googleapis.com/v1internal:loadCodeAssist",
)

ANTIGRAVITY_PLAN_BODY = '{"metadata":{"ideType":"ANTIGRAVITY"}}'

CODEX_RESET_CREDITS_URL = "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits"
CODEX_RESET_CONSUME_URL = "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits/consume"

_AUTH_MECHANISMS = {"oauth", "api_key", "api-key", "apikey", "session", "token"}

_PLAN_BY_TIER_ID = {
    "free-tier": "Free",
    "g1-pro-tier": "Pro",
    "g1-plus-tier": "Plus",
    "g1-ultra-tier": "Ultra",
    "g1-ultra-lite-tier": "Ultra Lite",
    "legacy-tier": "Legacy",
}

_WINDOW_5H = 5 * 60 * 60
_WINDOW_7D = 7 * 24 * 60 * 60


#: 额度缓存：cache_id → (过期时间, 板)。按实例 + 平台分桶，多实例互不干扰。
_quota_cache: dict[str, tuple[float, QuotaBoard]] = {}


def normalize_platform(value: str) -> str:
    raw = value.strip().lower().replace("_", "-")
    return PLATFORM_ALIASES.get(raw, "") or PLATFORM_ALIASES.get(value.strip(), "")


def platform_of(file: dict[str, Any]) -> str:
    raw = str(file.get("provider") or file.get("type") or "").strip().lower().replace("_", "-")
    return PLATFORM_ALIASES.get(raw, "other")


def is_platform_query(value: str) -> bool:
    return bool(normalize_platform(value))


def peek_quota_cache(
    files: list[dict[str, Any]],
    *,
    instance: str,
    platform: str | None = None,
    skip_disabled: bool = True,
) -> QuotaBoard | None:
    wanted = _wanted_files(files, platform=platform, skip_disabled=skip_disabled)
    cache_id = _cache_id(wanted, platform, instance)
    now = time.monotonic()
    entry = _quota_cache.get(cache_id)
    if entry and now < entry[0]:
        board = entry[1]
        board.cached = True
        return board
    if platform:
        full = _wanted_files(files, platform=None, skip_disabled=skip_disabled)
        entry = _quota_cache.get(_cache_id(full, None, instance))
        if entry and now < entry[0]:
            reports = [
                account
                for section in entry[1].platforms
                if section.platform == platform
                for account in section.accounts
            ]
            if reports:
                board = _build_board(reports)
                board.cached = True
                return board
    return None


async def collect_quotas(
    files: list[dict[str, Any]],
    *,
    instance: str,
    platform: str | None = None,
    force: bool = False,
    skip_disabled: bool = True,
) -> QuotaBoard:
    wanted = _wanted_files(files, platform=platform, skip_disabled=skip_disabled)
    cache_id = _cache_id(wanted, platform, instance)
    now = time.monotonic()
    if not force:
        entry = _quota_cache.get(cache_id)
        if entry and now < entry[0]:
            board = entry[1]
            board.cached = True
            return board

    snapshot = state.get_snapshot()
    cfg = snapshot.cpa.get(instance)
    if cfg is None:
        raise CPAError(f"没有名为「{instance}」的 CPA 实例。")
    client = get_client(instance)
    sem = asyncio.Semaphore(max(1, cfg.quota_concurrency))
    reports = await asyncio.gather(
        *(_one_account(client, cfg, item, sem) for item in wanted)
    )
    board = stamp_instance(_build_board(list(reports)), instance)
    ttl = snapshot.cache_ttl(platform or "", fallback=cfg.quota_cache_ttl)
    _quota_cache[cache_id] = (now + max(0.0, ttl), board)
    return board


def clear_quota_cache(instance: str | None = None) -> None:
    """清空额度缓存。传 ``instance`` 只清该实例，传 None 清全部。"""
    if instance is None:
        _quota_cache.clear()
        return
    prefix = f"{instance}:"
    for key in [key for key in _quota_cache if key.startswith(prefix)]:
        _quota_cache.pop(key, None)


def accounts_from_result(data: dict[str, Any] | Any) -> list[AccountQuota]:
    """兼容旧远程协议的结果转换（远程客户端已移除，仅保留给历史调用）。

    传入 ``{"accounts": [...]}`` 或带 ``accounts`` 属性的对象，转换为 AccountQuota 列表。
    """
    if isinstance(data, dict):
        raw_accounts = data.get("accounts")
        default_instance = str(data.get("instance") or data.get("client_name") or "")
    else:
        raw_accounts = getattr(data, "accounts", None)
        default_instance = str(getattr(data, "instance", "") or "")
    accounts: list[AccountQuota] = []
    if not isinstance(raw_accounts, list):
        return accounts
    for item in raw_accounts:
        entry = item if isinstance(item, dict) else getattr(item, "__dict__", {})
        windows = entry.get("windows") or []
        accounts.append(
            AccountQuota(
                platform=str(entry.get("platform") or "other"),
                name=str(entry.get("name") or ""),
                auth_index=str(entry.get("auth_index") or ""),
                plan=str(entry.get("plan") or ""),
                status=str(entry.get("status") or "unknown"),
                error=str(entry.get("error") or ""),
                windows=[
                    QuotaWindow(
                        id=str(window.get("id") or ""),
                        label=str(window.get("label") or ""),
                        used_percent=window.get("used_percent"),
                        remaining_percent=window.get("remaining_percent"),
                        remaining=window.get("remaining"),
                        limit=window.get("limit"),
                        reset_label=str(window.get("reset_label") or "-"),
                        reset_at=window.get("reset_at"),
                    )
                    for window in windows
                    if isinstance(window, dict)
                ],
                disabled=bool(entry.get("disabled")),
                cooling=bool(entry.get("cooling")),
                instance=str(entry.get("instance") or entry.get("client_name") or default_instance),
                subscription_expires_at=entry.get("subscription_expires_at"),
                subscription_expires_label=str(entry.get("subscription_expires_label") or ""),
                reset_credits=entry.get("reset_credits"),
            )
        )
    return accounts


async def refresh_codex_quota(file: dict[str, Any], instance: str) -> str:
    if platform_of(file) != "codex":
        raise CPAError("只有 Codex 账号可以手动重置额度。")
    auth_index = str(file.get("auth_index") or "")
    if not auth_index:
        raise CPAError("该凭证没有 auth_index，无法刷新 Codex 额度。")
    cfg = state.get_snapshot().cpa.get(instance)
    if cfg is None:
        raise CPAError(f"没有名为「{instance}」的 CPA 实例。")
    client = get_client(instance)
    headers = _codex_headers(file)
    credits = await _codex_upstream(client, cfg, auth_index, "GET", CODEX_RESET_CREDITS_URL, headers)
    credit = pick_codex_reset_credit(credits)
    if credit is None:
        raise CPAError("没有可用的 Codex 重置次数。")
    credit_id = _first_str(credit.get("id"), credit.get("credit_id"), credit.get("creditId"))
    if not credit_id:
        raise CPAError("重置券缺少 credit_id，无法消费。")
    body = json.dumps({"credit_id": credit_id, "redeem_request_id": str(uuid.uuid4())})
    result = await _codex_upstream(
        client, cfg, auth_index, "POST", CODEX_RESET_CONSUME_URL, headers, data=body
    )
    clear_quota_cache(instance)
    return format_codex_refresh_result(file, credit, result)


def pick_codex_reset_credit(payload: dict[str, Any]) -> dict[str, Any] | None:
    credits = _codex_credit_list(payload)
    available = [item for item in credits if _codex_credit_available(item)]
    if not available:
        return None

    def _expiry(item: dict[str, Any]) -> float:
        stamp = _first_str(
            item.get("expires_at"),
            item.get("expiresAt"),
            item.get("expire_at"),
            item.get("expireAt"),
        )
        parsed = _parse_ts(stamp)
        return parsed if parsed is not None else float("inf")

    return min(available, key=_expiry)


def format_codex_refresh_result(
    file: dict[str, Any],
    credit: dict[str, Any],
    result: dict[str, Any],
) -> str:
    name = display_name(file, public=True)
    code = _first_str(result.get("code"), result.get("status")).lower()
    remaining = _codex_remaining_count(result) or _codex_remaining_count(credit)
    extra = f"剩余重置次数 {remaining}" if remaining else ""
    if code in {"", "ok", "success", "reset"}:
        text = f"已为 {name} 消耗 1 次 Codex 重置次数，额度窗口已刷新。"
        return f"{text}{(' ' + extra) if extra else ''}"
    if code == "no_credit":
        return f"{name} 没有可用的 Codex 重置次数。"
    if code == "nothing_to_reset":
        return f"{name} 当前没有需要重置的额度窗口。"
    if code == "already_redeemed":
        return f"{name} 这张重置券已经用过。"
    error = _first_str(result.get("error"), result.get("message"), code) or "未知错误"
    return f"{name} Codex 重置失败：{error}"


def _codex_headers(file: dict[str, Any]) -> dict[str, str]:
    headers = {
        "Authorization": "Bearer $TOKEN$",
        "Content-Type": "application/json",
        "User-Agent": "codex_cli_rs/0.76.0 (Debian 13.0.0; x86_64) WindowsTerminal",
    }
    account_id = _first_str(
        file.get("chatgpt_account_id"),
        file.get("chatgptAccountId"),
        file.get("account_id"),
        file.get("accountId"),
    )
    if account_id and "@" not in account_id:
        headers["Chatgpt-Account-Id"] = account_id
    return headers


async def _codex_upstream(
    client: ManagementClient,
    cfg: CpaInstance,
    auth_index: str,
    method: str,
    url: str,
    header: dict[str, str],
    data: str | None = None,
) -> dict[str, Any]:
    response = await client.api_call(
        auth_index,
        method,
        url,
        header=header,
        data=data,
        timeout=cfg.quota_timeout,
    )
    status = int(response.get("status_code") or response.get("statusCode") or 0)
    body = _parse_body(response.get("body"))
    if status and not (200 <= status < 300):
        detail = ""
        if isinstance(body, dict):
            detail = _first_str(body.get("error"), body.get("message"), body.get("code"))
        raise CPAError(f"上游 HTTP {status}" + (f"：{detail}" if detail else ""))
    if body is None:
        raise CPAError("上游返回无法解析")
    return body


def count_codex_reset_credits(payload: dict[str, Any]) -> int | None:
    """从 CODEX_RESET_CREDITS_URL 返回的数据中解析可用重置点数。"""
    rem = _codex_remaining_count(payload)
    if rem:
        try:
            return int(rem)
        except (ValueError, TypeError):
            pass
    credits = _codex_credit_list(payload)
    if credits:
        available = [item for item in credits if _codex_credit_available(item)]
        return len(available)
    # 如果 payload 显式包含 credits/items 列表且为空，或者包含 count 字段
    for key in ("count", "total", "available"):
        val = payload.get(key)
        if isinstance(val, int) and not isinstance(val, bool):
            return val
    return None


def format_subscription_expiry_label(ts: float) -> str:
    """格式化订阅到期时间，包含剩余倒计时与本地时区绝对日期。"""
    dt = datetime.fromtimestamp(ts).astimezone()
    date_str = dt.strftime("%Y-%m-%d")
    now = time.time()
    diff = ts - now
    if diff <= 0:
        return f"{date_str} (已过期)"
    secs = int(diff)
    days, rem = divmod(secs, 86400)
    hours, _ = divmod(rem, 3600)
    if days > 0:
        rel = f"{days}天{hours}小时" if hours > 0 else f"{days}天"
    elif hours > 0:
        rel = f"{hours}小时"
    else:
        rel = "不足1小时"
    return f"{date_str} (剩{rel})"


def extract_subscription_expiry(data: dict[str, Any]) -> tuple[float | None, str]:
    """
    从凭证文件或响应字典中通用提取订阅到期时间。
    可靠规则：
    1. 顶级字段仅允许显式的 subscription_expires_at, subscriptionExpiresAt,
       subscription_expiration, subscriptionExpiration,
       chatgpt_subscription_active_until, chatgptSubscriptionActiveUntil,
       subscription_active_until, subscriptionActiveUntil。
       避免误判 auth token 过期字段 (expires_at, valid_until 等)。
    2. 在明确的 subscription/plan/tier/paidTier/currentTier 以及 id_token/idToken 嵌套对象内，
       提取订阅到期字段（包含 chatgpt_subscription_active_until / subscription_active_until / expires_at 等）。
    3. 严禁将 xAI 的 billingPeriodEnd / billing_period_end 或 Codex rate_limit reset_at 视为订阅到期。
    """
    if not isinstance(data, dict):
        return None, ""

    explicit_top_keys = (
        "subscription_expires_at",
        "subscriptionExpiresAt",
        "subscription_expiration",
        "subscriptionExpiration",
        "chatgpt_subscription_active_until",
        "chatgptSubscriptionActiveUntil",
        "subscription_active_until",
        "subscriptionActiveUntil",
    )
    for key in explicit_top_keys:
        if key in data and data[key] is not None and data[key] != "":
            parsed_ts = _parse_any_ts(data[key])
            if parsed_ts is not None and parsed_ts > 0:
                return parsed_ts, format_subscription_expiry_label(parsed_ts)

    # 优先检查 id_token / idToken 嵌套中的显式 subscription_active_until / chatgpt_subscription_active_until
    id_token = data.get("id_token") or data.get("idToken")
    if isinstance(id_token, dict):
        id_token_keys = (
            "chatgpt_subscription_active_until",
            "chatgptSubscriptionActiveUntil",
            "subscription_active_until",
            "subscriptionActiveUntil",
            "subscription_expires_at",
            "subscriptionExpiresAt",
            "subscription_expiration",
            "subscriptionExpiration",
        )
        for key in id_token_keys:
            if key in id_token and id_token[key] is not None and id_token[key] != "":
                parsed_ts = _parse_any_ts(id_token[key])
                if parsed_ts is not None and parsed_ts > 0:
                    return parsed_ts, format_subscription_expiry_label(parsed_ts)

    nested_sub_keys = (
        "subscription",
        "plan",
        "tier",
        "paidTier",
        "paid_tier",
        "currentTier",
        "current_tier",
    )
    candidate_nested_keys = (
        "chatgpt_subscription_active_until", "chatgptSubscriptionActiveUntil",
        "subscription_active_until", "subscriptionActiveUntil",
        "expires_at", "expiresAt", "expire_at", "expireAt",
        "subscription_expires_at", "subscriptionExpiresAt",
        "subscription_expiration", "subscriptionExpiration",
        "expires", "expire", "valid_until", "validUntil",
        "end_date", "endDate", "next_billing_date", "nextBillingDate",
    )

    for n_key in nested_sub_keys:
        nested = data.get(n_key)
        if isinstance(nested, dict):
            for key in candidate_nested_keys:
                if key in nested and nested[key] is not None and nested[key] != "":
                    parsed_ts = _parse_any_ts(nested[key])
                    if parsed_ts is not None and parsed_ts > 0:
                        return parsed_ts, format_subscription_expiry_label(parsed_ts)

    return None, ""


def _parse_any_ts(value: Any) -> float | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        ts = float(value)
        return ts / 1000.0 if ts > 1e12 else ts
    text = str(value).strip()
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        pass
    try:
        val = float(text)
        return val / 1000.0 if val > 1e12 else val
    except Exception:
        pass
    return None


def _codex_credit_list(payload: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("credits", "items", "rate_limit_reset_credits", "rateLimitResetCredits"):
        raw = payload.get(key)
        if isinstance(raw, list):
            return [item for item in raw if isinstance(item, dict)]
        if isinstance(raw, dict):
            nested = raw.get("credits") or raw.get("items")
            if isinstance(nested, list):
                return [item for item in nested if isinstance(item, dict)]
    return []


def _codex_credit_available(item: dict[str, Any]) -> bool:
    status = _first_str(item.get("status"), item.get("state")).lower()
    if status and status not in {"available", "ok", "active", "unused"}:
        return False
    if item.get("redeemed") or item.get("redeemed_at") or item.get("redeemedAt"):
        return False
    return True


def _codex_remaining_count(*payloads: dict[str, Any]) -> str:
    for payload in payloads:
        value = payload.get("available_count")
        if value is None:
            value = payload.get("availableCount")
        if value is None:
            value = payload.get("remaining")
        if value is not None and value != "":
            return str(value)
    return ""


def _parse_ts(value: str) -> float | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except ValueError:
        return None


def format_quota_board(board: QuotaBoard, *, account_limit: int = 12) -> list[str]:
    if not board.platforms:
        return ["没有可展示的额度账号。"]
    head = [
        "额度总览（按平台）",
        f"查询 {board.queried} | 成功 {board.ok} | 失败 {board.failed} | 仅健康 {board.skipped}"
        + (" | 缓存" if board.cached else ""),
        "合计格式：窗口 剩余当量/账号数（均剩%）。1.00 = 满额一个号，不要把各账号百分比直接相加。",
    ]
    chunks: list[str] = []
    current = list(head)
    for section in board.platforms:
        block = _format_platform(section, account_limit)
        if sum(len(line) + 1 for line in current) + sum(len(line) + 1 for line in block) > 3400:
            chunks.append("\n".join(current))
            current = block
        else:
            if current is not head and current:
                current.append("")
            current.extend(block)
    if current:
        chunks.append("\n".join(current))
    return chunks or ["没有可展示的额度账号。"]


def platform_total_groups(section: PlatformQuota) -> list[tuple[str, list[str]]]:
    """合计窗口按火山计划分组：返回 ``[(计划名, [chip, ...]), ...]``。

    非火山平台返回单组 ``("", [所有 chip])``；火山平台按 Coding → Agent 分组，
    计划名由调用方渲染为小标题（``""`` 表示不分组、不加标题）。
    """
    account_n = len(section.accounts)
    ordered = sort_windows(
        [
            QuotaWindow(id=window_id, label=section.window_labels.get(window_id, window_id))
            for window_id in section.window_remain_sum
        ]
    )
    chips_by_id: dict[str, str] = {}
    for window in ordered:
        window_id = window.id
        total = section.window_remain_sum[window_id]
        count = section.window_remain_count.get(window_id, 0)
        denom = count or account_n
        equiv = total / 100.0
        avg = (equiv / denom * 100.0) if denom else 0.0
        label = section.window_labels.get(window_id, window_id)
        chips_by_id[window_id] = f"{label} {equiv:.2f}/{denom} ({avg:.0f}%)"
    groups: list[tuple[str, list[str]]] = [
        (plan, [chips_by_id[wid] for wid in ids])
        for plan, ids in group_window_ids_by_plan([window.id for window in ordered])
    ]
    if section.limit_sum:
        tail = f"绝对剩余 {section.remaining_sum:.0f}/{section.limit_sum:.0f}"
        if groups:
            groups[-1][1].append(tail)
        else:
            groups = [("", [tail])]
    return groups


def platform_total_chips(section: PlatformQuota) -> list[str]:
    """扁平合计 chips（保持既有调用与测试兼容）。"""
    return [chip for _plan, chips in platform_total_groups(section) for chip in chips]


def format_account_quota(account: AccountQuota) -> str:
    lines = [f"[{PLATFORM_TITLES.get(account.platform, account.platform)}] {account.name}"]
    if account.plan:
        lines.append(f"套餐：{account.plan}")
    for _kind, text in (getattr(account, "subscription_badges", None) or []):
        lines.append(str(text))
    lines.append(f"状态：{account.status}")
    if account.error:
        lines.append(f"错误：{account.error}")
    if account.windows:
        lines.append("额度：")
        for window in account.windows:
            lines.append(f"  {_window_text(window)}")
    return "\n".join(lines)


async def _one_account(
    client: ManagementClient,
    cfg: CpaInstance,
    file: dict[str, Any],
    sem: asyncio.Semaphore,
) -> AccountQuota:
    platform = platform_of(file)
    exp_ts, exp_label = extract_subscription_expiry(file)
    report = AccountQuota(
        platform=platform,
        name=display_name(file, public=True),
        auth_index=str(file.get("auth_index") or ""),
        plan=_plan_from_auth_file(file),
        status=str(file.get("status") or "unknown"),
        disabled=bool(file.get("disabled")),
        cooling=is_cooling(file),
        subscription_expires_at=exp_ts,
        subscription_expires_label=exp_label,
    )
    if is_unhealthy(file) and report.status in {"ready", "ok", "active", "unknown"}:
        report.status = "cooling" if report.cooling else str(file.get("status") or "error")
    if not report.auth_index or platform == "other":
        return report
    async with sem:
        try:
            if platform == "claude":
                await _fill_claude(client, cfg, report)
            elif platform == "codex":
                await _fill_codex(client, cfg, file, report)
            elif platform == "kimi":
                await _fill_kimi(client, cfg, report)
            elif platform == "xai":
                await _fill_xai(client, cfg, report)
            elif platform == "antigravity":
                await _fill_antigravity(client, cfg, report)
            elif platform == "gemini-cli":
                await _fill_gemini(client, cfg, report)
        except CPAError as exc:
            report.error = str(exc)
            report.status = "error"
    return report


async def _fill_claude(client: ManagementClient, cfg: CpaInstance, report: AccountQuota) -> None:
    payload = await _upstream_json(
        client,
        cfg,
        report,
        "GET",
        "https://api.anthropic.com/api/oauth/usage",
        {
            "Authorization": "Bearer $TOKEN$",
            "Content-Type": "application/json",
            "anthropic-beta": "oauth-2025-04-20",
        },
    )
    if payload is None:
        return
    windows, plan = parse_claude_usage(payload)
    report.windows = sort_windows(windows)
    report.status = _status_from_windows(report.windows, report)
    report.plan = _sanitize_plan(plan) or report.plan
    report.error = ""
    if not report.subscription_expires_label:
        exp_ts, exp_label = extract_subscription_expiry(payload)
        if exp_label:
            report.subscription_expires_at = exp_ts
            report.subscription_expires_label = exp_label


def _codex_account_id(file: dict[str, Any]) -> str:
    """提取 Codex account_id，仅接受非邮箱格式的显式 ID。"""
    raw_id = _first_str(
        file.get("chatgpt_account_id"),
        file.get("chatgptAccountId"),
        file.get("account_id"),
        file.get("accountId"),
    )
    if raw_id and "@" not in raw_id:
        return raw_id
    return ""


async def _fill_codex(
    client: ManagementClient,
    cfg: CpaInstance,
    file: dict[str, Any],
    report: AccountQuota,
) -> None:
    headers = {
        "Authorization": "Bearer $TOKEN$",
        "Content-Type": "application/json",
        "User-Agent": "codex_cli_rs/0.76.0 (Debian 13.0.0; x86_64) WindowsTerminal",
    }
    account_id = _codex_account_id(file)
    if account_id:
        headers["Chatgpt-Account-Id"] = account_id
    payload = await _upstream_json(
        client,
        cfg,
        report,
        "GET",
        "https://chatgpt.com/backend-api/wham/usage",
        headers,
    )
    if payload is not None:
        windows, plan = parse_codex_usage(payload)
        report.windows = sort_windows(windows)
        report.status = _status_from_windows(report.windows, report)
        report.plan = _sanitize_plan(plan) or report.plan
        report.error = ""
        if not report.subscription_expires_label:
            exp_ts, exp_label = extract_subscription_expiry(payload)
            if exp_label:
                report.subscription_expires_at = exp_ts
                report.subscription_expires_label = exp_label

    # Codex reset credits read-only GET check (failure must not fail usage quota)
    saved_status = report.status
    saved_error = report.error
    try:
        credits_payload = await _upstream_json(
            client,
            cfg,
            report,
            "GET",
            CODEX_RESET_CREDITS_URL,
            headers,
        )
        if credits_payload is not None:
            credits_count = count_codex_reset_credits(credits_payload)
            if credits_count is not None:
                report.reset_credits = credits_count
    except Exception:
        pass
    finally:
        report.status = saved_status
        report.error = saved_error


async def _fill_kimi(client: ManagementClient, cfg: CpaInstance, report: AccountQuota) -> None:
    payload = await _upstream_json(
        client,
        cfg,
        report,
        "GET",
        "https://api.kimi.com/coding/v1/usages",
        {"Authorization": "Bearer $TOKEN$"},
    )
    if payload is None:
        return
    report.windows = sort_windows(parse_kimi_usage(payload))
    report.status = _status_from_windows(report.windows, report)
    report.error = ""
    if not report.subscription_expires_label:
        exp_ts, exp_label = extract_subscription_expiry(payload)
        if exp_label:
            report.subscription_expires_at = exp_ts
            report.subscription_expires_label = exp_label


async def _fill_xai(client: ManagementClient, cfg: CpaInstance, report: AccountQuota) -> None:
    payload = await _upstream_json(
        client,
        cfg,
        report,
        "GET",
        "https://cli-chat-proxy.grok.com/v1/billing?format=credits",
        {
            "Authorization": "Bearer $TOKEN$",
            "x-xai-token-auth": "xai-grok-cli",
            "x-grok-client-version": "0.2.91",
            "accept": "*/*",
        },
    )
    if payload is None:
        return
    report.windows = sort_windows(parse_xai_billing(payload))
    report.status = _status_from_windows(report.windows, report)
    report.error = ""
    # Note: Requirement 3: Do NOT treat xAI billingPeriodEnd as subscription expiry.


async def _fill_antigravity(client: ManagementClient, cfg: CpaInstance, report: AccountQuota) -> None:
    headers = {
        "Authorization": "Bearer $TOKEN$",
        "Content-Type": "application/json",
        "User-Agent": "antigravity/cli/1.0.13 (aidev_client; os_type=darwin; arch=arm64)",
    }
    payload = None
    for url in ANTIGRAVITY_URLS:
        payload = await _upstream_json(client, cfg, report, "POST", url, headers, data="{}")
        if payload is not None:
            break
    if payload is None:
        return
    report.windows = sort_windows(parse_antigravity_summary(payload))
    report.status = _status_from_windows(report.windows, report)
    report.error = ""
    plan = await _antigravity_plan(client, cfg, report)
    if plan:
        report.plan = plan
    if not report.subscription_expires_label:
        exp_ts, exp_label = extract_subscription_expiry(payload)
        if exp_label:
            report.subscription_expires_at = exp_ts
            report.subscription_expires_label = exp_label


async def _fill_gemini(client: ManagementClient, cfg: CpaInstance, report: AccountQuota) -> None:
    payload = await _upstream_json(
        client,
        cfg,
        report,
        "POST",
        "https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuota",
        {
            "Authorization": "Bearer $TOKEN$",
            "Content-Type": "application/json",
        },
        data="{}",
    )
    if payload is None:
        return
    windows: list[QuotaWindow] = []
    models = payload.get("models") or payload.get("quotas") or []
    if isinstance(models, list):
        for item in models[:8]:
            if not isinstance(item, dict):
                continue
            label = str(item.get("name") or item.get("model") or item.get("displayName") or "模型")
            remain = _first_number(item.get("remainingFraction"), item.get("remaining"))
            limit = _first_number(item.get("limit"), item.get("quota"))
            used = _first_number(item.get("used"), item.get("usedPercent"))
            window = QuotaWindow(
                id=label,
                label=label,
                reset_label=_iso_reset(item.get("resetTime") or item.get("resetAt")),
                reset_at=_parse_ts(_first_str(item.get("resetTime"), item.get("resetAt"))),
            )
            if remain is not None and remain <= 1:
                window.remaining_percent = _clamp(remain * 100.0)
                window.used_percent = _clamp(100.0 - window.remaining_percent)
            elif used is not None and used <= 100:
                window.used_percent = _clamp(used)
                window.remaining_percent = _clamp(100.0 - window.used_percent)
            elif remain is not None and limit:
                window.remaining = remain
                window.limit = limit
                window.remaining_percent = _clamp(remain / limit * 100.0)
                window.used_percent = _clamp(100.0 - window.remaining_percent)
            windows.append(window)
    report.windows = sort_windows(windows)
    report.status = _status_from_windows(report.windows, report)
    report.error = ""
    if not report.subscription_expires_label:
        exp_ts, exp_label = extract_subscription_expiry(payload)
        if exp_label:
            report.subscription_expires_at = exp_ts
            report.subscription_expires_label = exp_label


def parse_claude_usage(payload: dict[str, Any]) -> tuple[list[QuotaWindow], str]:
    windows: list[QuotaWindow] = []
    for key, label in CLAUDE_WINDOWS:
        item = payload.get(key)
        if not isinstance(item, dict):
            continue
        used = _number(item.get("utilization"))
        reset_str = item.get("resets_at") or item.get("reset_at")
        window = QuotaWindow(
            id=key,
            label=label,
            reset_label=_iso_reset(reset_str),
            reset_at=_parse_ts(reset_str) if reset_str else None,
        )
        if used is not None:
            window.used_percent = _clamp(used)
            window.remaining_percent = _clamp(100.0 - window.used_percent)
        windows.append(window)
    extra = payload.get("extra_usage")
    if isinstance(extra, dict) and extra.get("is_enabled"):
        used = _number(extra.get("utilization"))
        window = QuotaWindow(id="extra", label="Extra", reset_label="-")
        if used is not None:
            window.used_percent = _clamp(used)
            window.remaining_percent = _clamp(100.0 - window.used_percent)
        windows.append(window)
    return sort_windows(windows), _sanitize_plan(payload.get("plan_type") or payload.get("planType"))


def parse_codex_usage(payload: dict[str, Any]) -> tuple[list[QuotaWindow], str]:
    plan = _first_str(payload.get("plan_type"), payload.get("planType"))
    rate = _as_dict(payload.get("rate_limit") or payload.get("rateLimit"))
    five, weekly = _codex_windows(rate)
    windows: list[QuotaWindow] = []
    if five:
        windows.append(_codex_window("code-5h", "5h", five, rate))
    if weekly:
        windows.append(_codex_window("code-7d", "周", weekly, rate))
    return sort_windows([item for item in windows if item]), _sanitize_plan(plan)


def parse_kimi_usage(payload: dict[str, Any]) -> list[QuotaWindow]:
    windows: list[QuotaWindow] = []
    usage = payload.get("usage")
    if isinstance(usage, dict):
        windows.append(_kimi_window("usage", "用量", usage))
    limits = payload.get("limits")
    if isinstance(limits, list):
        for index, item in enumerate(limits):
            if not isinstance(item, dict):
                continue
            detail = item.get("detail") if isinstance(item.get("detail"), dict) else item
            label = str(item.get("title") or item.get("name") or item.get("scope") or f"限额{index + 1}")
            windows.append(_kimi_window(f"limit-{index}", label, detail if isinstance(detail, dict) else item))
    return sort_windows(
        [item for item in windows if item.used_percent is not None or item.remaining is not None]
    )


def _slugify_grok_product(name: str) -> str:
    """
    将产品名规范化为 ID，例如 GrokImagine -> grok-imagine, GrokChat -> grok-chat, GrokBuild -> grok-build。
    处理 CamelCase、空格、下划线、连字符。
    """
    cleaned = re.sub(r"[_\s]+", "-", name.strip())
    # CamelCase 转 kebab-case: 例如 GrokImagine -> Grok-Imagine
    s1 = re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", cleaned)
    s2 = re.sub(r"([A-Z]+)([A-Z][a-z0-9])", r"\1-\2", s1)
    slug = re.sub(r"[^a-zA-Z0-9\-]+", "", s2).strip("-").lower()
    if not slug:
        return ""
    if not slug.startswith("grok-") and slug != "grok":
        slug = f"grok-{slug}"
    return slug


def parse_xai_billing(payload: dict[str, Any]) -> list[QuotaWindow]:
    config = _as_dict(payload.get("config")) or payload
    windows: list[QuotaWindow] = []
    weekly_used = _first_number(
        config.get("creditUsagePercent"),
        config.get("credit_usage_percent"),
        config.get("usedPercent"),
        payload.get("creditUsagePercent"),
    )
    current_period = _as_dict(config.get("currentPeriod")) or _as_dict(config.get("current_period")) or _as_dict(payload.get("currentPeriod")) or _as_dict(payload.get("current_period")) or {}
    period_end = (
        config.get("billingPeriodEnd")
        or config.get("billing_period_end")
        or payload.get("billingPeriodEnd")
        or payload.get("billing_period_end")
        or current_period.get("end")
        or current_period.get("end_time")
        or current_period.get("endTime")
    )
    weekly = QuotaWindow(
        id="billing",
        label="周额度",
        reset_label=_human_reset(period_end),
        reset_at=_parse_ts(str(period_end)) if period_end else None,
    )
    if weekly_used is not None:
        weekly.used_percent = _clamp(weekly_used)
        weekly.remaining_percent = _clamp(100.0 - weekly.used_percent)
        windows.append(weekly)

    seen_ids: set[str] = set()
    dynamic_windows: list[QuotaWindow] = []

    lists_to_check: list[list[Any]] = []
    for container in (payload, config if config is not payload else None):
        if not isinstance(container, dict):
            continue
        for lk in ("productUsage", "product_usage", "products", "usages"):
            val = container.get(lk)
            if isinstance(val, list) and val not in lists_to_check:
                lists_to_check.append(val)

    for prod_list in lists_to_check:
        for item in prod_list:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or item.get("product") or item.get("title") or "").strip()
            used = _first_number(
                item.get("usagePercent"),
                item.get("usedPercent"),
                item.get("creditUsagePercent"),
                item.get("usage_percent"),
                item.get("used_percent"),
                item.get("credit_usage_percent"),
            )
            if not name or used is None:
                continue
            window_id = _slugify_grok_product(name)
            if not window_id or window_id in seen_ids or window_id == "billing":
                continue
            seen_ids.add(window_id)
            w = QuotaWindow(id=window_id, label=name)
            w.used_percent = _clamp(used)
            w.remaining_percent = _clamp(100.0 - w.used_percent)
            dynamic_windows.append(w)

    for window_id, label, keys in (
        ("grok-build", "GrokBuild", ("grokBuildUsagePercent", "grok_build_usage_percent", "grokBuildUsage")),
        ("grok-chat", "GrokChat", ("grokChatUsagePercent", "grok_chat_usage_percent", "grokChatUsage")),
    ):
        if window_id in seen_ids:
            continue
        used = None
        for key in keys:
            used = _number(config.get(key))
            if used is None:
                used = _number(payload.get(key))
            if used is not None:
                break
        if used is None:
            continue
        seen_ids.add(window_id)
        window = QuotaWindow(id=window_id, label=label)
        window.used_percent = _clamp(used)
        window.remaining_percent = _clamp(100.0 - window.used_percent)
        dynamic_windows.append(window)

    indexed = {wid: index for index, wid in enumerate(WINDOW_ORDER)}
    dynamic_windows.sort(key=lambda w: (indexed.get(w.id, len(WINDOW_ORDER)), w.id, w.label))

    windows.extend(dynamic_windows)
    return windows


def parse_antigravity_summary(payload: dict[str, Any]) -> list[QuotaWindow]:
    windows: list[QuotaWindow] = []
    for group in payload.get("groups") or []:
        if not isinstance(group, dict):
            continue
        group_name = str(group.get("displayName") or group.get("display_name") or "配额")
        for bucket in group.get("buckets") or []:
            if not isinstance(bucket, dict):
                continue
            raw_label = str(
                bucket.get("displayName") or bucket.get("display_name") or bucket.get("window") or group_name
            )
            window_id, label = _antigravity_window(group_name, raw_label)
            remain = _first_number(bucket.get("remainingFraction"), bucket.get("remaining_fraction"))
            reset_str = bucket.get("resetTime") or bucket.get("reset_time")
            window = QuotaWindow(
                id=window_id,
                label=label,
                reset_label=_iso_reset(reset_str),
                reset_at=_parse_ts(reset_str) if reset_str else None,
            )
            if remain is not None:
                frac = remain if remain <= 1 else remain / 100.0
                window.remaining_percent = _clamp(frac * 100.0)
                window.used_percent = _clamp(100.0 - window.remaining_percent)
            windows.append(window)
    return sort_windows(windows)


def parse_antigravity_plan(payload: dict[str, Any]) -> str:
    current = payload.get("currentTier") or payload.get("current_tier")
    paid = payload.get("paidTier") or payload.get("paid_tier")
    current_tier = _as_dict(current) if not isinstance(current, str) else {"name": current, "id": current}
    paid_tier = _as_dict(paid) if not isinstance(paid, str) else {"name": paid, "id": paid}
    effective = paid_tier if paid_tier and (paid_tier.get("id") or paid_tier.get("name")) else current_tier
    if not effective:
        return ""
    tier_id = _first_str(effective.get("id"), effective.get("tierId"), effective.get("tier_id"))
    mapped = _PLAN_BY_TIER_ID.get(tier_id.lower()) if tier_id else ""
    if mapped:
        return mapped
    name = _first_str(effective.get("name"), effective.get("displayName"), effective.get("description"))
    return _plan_from_name(name) or _sanitize_plan(name)


def _plan_from_auth_file(file: dict[str, Any]) -> str:
    subscription = file.get("subscription")
    if isinstance(subscription, dict):
        plan = parse_antigravity_plan({"paidTier": subscription, "currentTier": subscription})
        if plan:
            return plan
        plan = _sanitize_plan(subscription.get("plan") or subscription.get("tierName") or subscription.get("name"))
        if plan:
            return plan

    id_token = file.get("id_token") or file.get("idToken")
    if isinstance(id_token, dict):
        for key in ("plan_type", "planType", "plan", "tier", "tier_name", "tierName"):
            plan = _sanitize_plan(id_token.get(key))
            if plan:
                return plan

    for key in ("plan", "plan_type", "planType", "tier", "tier_name", "tierName"):
        plan = _sanitize_plan(file.get(key))
        if plan:
            return plan
    return ""


def _plan_from_name(name: str) -> str:
    blob = name.lower().replace("_", " ").replace("-", " ")
    if "ultra lite" in blob or "ultralite" in blob:
        return "Ultra Lite"
    if "ultra" in blob:
        return "Ultra"
    if "team" in blob:
        return "Team"
    if "enterprise" in blob:
        return "Enterprise"
    if "plus" in blob:
        return "Plus"
    if "pro" in blob:
        return "Pro"
    if "legacy" in blob:
        return "Legacy"
    if "free" in blob:
        return "Free"
    return ""


def _sanitize_plan(value: Any) -> str:
    if isinstance(value, dict):
        # 递归提取嵌套字典中常见字段
        extracted = (
            value.get("name")
            or value.get("tierName")
            or value.get("tier_name")
            or value.get("plan")
            or value.get("tier")
            or value.get("id")
            or value.get("tierId")
            or value.get("tier_id")
        )
        if extracted and isinstance(extracted, str):
            return _sanitize_plan(extracted)
        return ""
    text = str(value or "").strip()
    if not text:
        return ""
    if text.startswith("{") and text.endswith("}"):
        return ""
    if "@" in text:
        return ""
    if text.lower().replace("_", "-") in _AUTH_MECHANISMS:
        return ""
    mapped = _PLAN_BY_TIER_ID.get(text.lower())
    if mapped:
        return mapped
    named = _plan_from_name(text)
    return named or text


async def _antigravity_plan(client: ManagementClient, cfg: CpaInstance, report: AccountQuota) -> str:
    headers = {
        "Authorization": "Bearer $TOKEN$",
        "Content-Type": "application/json",
        "User-Agent": "antigravity/cli/1.0.13 (aidev_client; os_type=darwin; arch=arm64)",
    }
    saved_error = report.error
    saved_status = report.status
    try:
        for url in ANTIGRAVITY_PLAN_URLS:
            payload = await _upstream_json(client, cfg, report, "POST", url, headers, data=ANTIGRAVITY_PLAN_BODY)
            if payload is None:
                continue
            plan = parse_antigravity_plan(payload)
            if plan:
                return plan
        return ""
    finally:
        report.error = saved_error
        report.status = saved_status


def _antigravity_window(group_name: str, bucket_name: str) -> tuple[str, str]:
    group = group_name.lower()
    bucket = bucket_name.lower()
    is_gemini = "gemini" in group
    is_claude = "claude" in group or "gpt" in group
    is_5h = "five" in bucket or "5h" in bucket or "5-hour" in bucket or "hour" in bucket
    is_week = "week" in bucket or "weekly" in bucket or "seven" in bucket
    is_month = "month" in bucket or "monthly" in bucket or "30d" in bucket
    if is_gemini and is_5h:
        return "gemini-5h", "Gemini 5h"
    if is_gemini and is_week:
        return "gemini-week", "Gemini 周"
    if is_gemini and is_month:
        return "gemini-month", "Gemini 月"
    if is_claude and is_5h:
        return "claude-gpt-5h", "Claude/GPT 5h"
    if is_claude and is_week:
        return "claude-gpt-week", "Claude/GPT 周"
    if is_claude and is_month:
        return "claude-gpt-month", "Claude/GPT 月"
    return f"{group_name}-{bucket_name}", f"{group_name} {bucket_name}"


async def _upstream_json(
    client: ManagementClient,
    cfg: CpaInstance,
    report: AccountQuota,
    method: str,
    url: str,
    header: dict[str, str],
    data: str | None = None,
) -> dict[str, Any] | None:
    try:
        response = await client.api_call(
            report.auth_index,
            method,
            url,
            header=header,
            data=data,
            timeout=cfg.quota_timeout,
        )
    except CPAError as exc:
        report.error = str(exc)
        report.status = "error"
        return None
    status = int(response.get("status_code") or response.get("statusCode") or 0)
    body = _parse_body(response.get("body"))
    if status and not (200 <= status < 300):
        report.error = f"上游 HTTP {status}"
        report.status = "error"
        return None
    if body is None:
        report.error = "上游返回无法解析"
        report.status = "error"
        return None
    return body


def _format_platform(section: PlatformQuota, account_limit: int) -> list[str]:
    account_n = len(section.accounts)
    hidden = int(getattr(section, "hidden", 0) or 0)
    header = f"【{section.title}】{account_n} 账号"
    if hidden:
        header += f"（另有 {hidden} 个未显示）"
    lines = [header]
    total_groups = platform_total_groups(section)
    if total_groups:
        if len(total_groups) == 1 and not total_groups[0][0]:
            lines.append("  合计：" + " · ".join(total_groups[0][1]))
        else:
            lines.append("  合计：")
            for plan, chips in total_groups:
                head = f"{plan}：" if plan else ""
                lines.append(f"    {head}" + " · ".join(chips))
    visible = section.accounts[:account_limit]
    for account in visible:
        flags = []
        if account.disabled:
            flags.append("disabled")
        if account.cooling:
            flags.append("冷却中")
        flag = f" [{' '.join(flags)}]" if flags else ""
        plan = f" ({account.plan})" if account.plan else ""
        shown = prefix_instance(account.instance, account.name)
        if account.error:
            lines.append(f"  {shown}{plan}{flag}  失败：{account.error}")
            continue
        if not account.windows:
            raw_st = account.status or "unknown"
            st_text = "冷却中" if raw_st == "cooling" else raw_st
            lines.append(f"  {shown}{plan}{flag}  {st_text}（无上游额度）")
            continue
        windows = " · ".join(_window_text(window, compact=True) for window in account.windows[:4])
        subs = " · ".join(str(t) for _k, t in (getattr(account, "subscription_badges", None) or []))
        if subs:
            shown_subs = f"  {subs}"
        else:
            shown_subs = ""
        lines.append(f"  {shown}{plan}{flag}  {windows}{shown_subs}")
    extra = len(section.accounts) - len(visible)
    if extra > 0:
        lines.append(f"  ... 另有 {extra} 个账号，用 /quotanoa {section.platform} 查看")
    return lines


def _window_text(window: QuotaWindow, *, compact: bool = False) -> str:
    if window_is_used(window) and window.used_percent is not None:
        body = f"已使用 {window.used_percent:.0f}%"
    elif window.remaining_percent is not None:
        body = f"剩 {window.remaining_percent:.0f}%"
    elif window.used_percent is not None:
        body = f"已用 {window.used_percent:.0f}%"
    elif window.remaining is not None and window.limit is not None:
        body = f"{window.remaining:.0f}/{window.limit:.0f}"
    else:
        body = "?"
    if compact:
        if window.reset_note:
            reset = f" {window.reset_note}"
        elif window.reset_label and window.reset_label != "-":
            reset = f" →{window.reset_label}"
        else:
            reset = ""
        return f"{window.label} {body}{reset}"
    if window.reset_note:
        reset = f"  {window.reset_note}"
    elif window.reset_label and window.reset_label != "-":
        reset = f"  重置 {window.reset_label}"
    else:
        reset = ""
    return f"{window.label} {body}{reset}"


def _status_from_windows(windows: list[QuotaWindow], report: AccountQuota) -> str:
    if report.disabled:
        return "disabled"
    percents = [w.remaining_percent for w in windows if w.remaining_percent is not None]
    if not percents:
        return report.status or "unknown"
    lowest = min(percents)
    if lowest <= 0:
        return "exhausted"
    if lowest <= 15:
        return "low"
    if report.cooling:
        return "cooling"
    return "ok"


def _codex_windows(rate: dict[str, Any] | None) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if not rate:
        return None, None
    primary = _as_dict(rate.get("primary_window") or rate.get("primaryWindow"))
    secondary = _as_dict(rate.get("secondary_window") or rate.get("secondaryWindow"))
    five = weekly = None
    for candidate in (primary, secondary):
        if not candidate:
            continue
        duration = _first_number(candidate.get("limit_window_seconds"), candidate.get("limitWindowSeconds")) or 0
        if duration == _WINDOW_5H and five is None:
            five = candidate
        if duration == _WINDOW_7D and weekly is None:
            weekly = candidate
    if five is None:
        five = primary
    if weekly is None:
        weekly = secondary
    return five, weekly


def _codex_window(window_id: str, label: str, window: dict[str, Any], rate: dict[str, Any] | None) -> QuotaWindow:
    used = _first_number(window.get("used_percent"), window.get("usedPercent"))
    reset_ts = _first_number(window.get("reset_at"), window.get("resetAt"))
    if reset_ts:
        reset_epoch = reset_ts / 1000.0 if reset_ts > 1e12 else reset_ts
    else:
        secs = _first_number(window.get("reset_after_seconds"), window.get("resetAfterSeconds"))
        reset_epoch = (time.time() + secs) if secs else None

    item = QuotaWindow(
        id=window_id,
        label=label,
        reset_label=_codex_reset(window),
        reset_at=reset_epoch,
    )
    if used is not None:
        item.used_percent = _clamp(used)
        item.remaining_percent = _clamp(100.0 - item.used_percent)
    elif rate and (rate.get("limit_reached") or rate.get("limitReached")):
        item.used_percent = 100.0
        item.remaining_percent = 0.0
    return item


def _kimi_window(window_id: str, label: str, data: dict[str, Any]) -> QuotaWindow:
    used = _number(data.get("used"))
    limit = _number(data.get("limit"))
    remaining = _number(data.get("remaining"))
    reset_val = data.get("resetAt") or data.get("reset_at") or data.get("resetTime")
    reset_epoch = _parse_ts(str(reset_val)) if reset_val else None
    if reset_epoch is None and isinstance(reset_val, (int, float)) and not isinstance(reset_val, bool):
        reset_epoch = float(reset_val) / 1000.0 if float(reset_val) > 1e12 else float(reset_val)

    window = QuotaWindow(
        id=window_id,
        label=label,
        remaining=remaining,
        limit=limit,
        reset_label=_iso_reset(reset_val),
        reset_at=reset_epoch,
    )
    if remaining is not None and limit:
        window.remaining_percent = _clamp(remaining / limit * 100.0)
        window.used_percent = _clamp(100.0 - window.remaining_percent)
    elif used is not None and limit:
        window.used_percent = _clamp(used / limit * 100.0)
        window.remaining_percent = _clamp(100.0 - window.used_percent)
        window.remaining = (limit - used) if remaining is None else remaining
    return window


def _codex_reset(window: dict[str, Any]) -> str:
    ts = _first_number(window.get("reset_at"), window.get("resetAt"))
    if ts:
        return _relative_from_ts(ts / 1000.0 if ts > 1e12 else ts)
    secs = _first_number(window.get("reset_after_seconds"), window.get("resetAfterSeconds"))
    if secs:
        return _relative_from_ts(time.time() + secs)
    return "-"


def _iso_reset(value: Any) -> str:
    return _human_reset(value)


def _human_reset(value: Any) -> str:
    if value is None or value == "" or value == "-":
        return "-"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        ts = float(value)
        return _relative_from_ts(ts / 1000.0 if ts > 1e12 else ts)
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return _relative_from_ts(parsed.timestamp())
    except ValueError:
        return text[:16]


def _relative_from_ts(ts: float) -> str:
    delta = ts - time.time()
    if delta <= 0:
        return "已过期"
    secs = int(delta)
    days, rem = divmod(secs, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d{hours}h" if hours else f"{days}d"
    if hours:
        return f"{hours}h{minutes}m" if minutes else f"{hours}h"
    return f"{max(minutes, 1)}m"


def _wanted_files(
    files: list[dict[str, Any]],
    *,
    platform: str | None,
    skip_disabled: bool,
) -> list[dict[str, Any]]:
    wanted: list[dict[str, Any]] = []
    for item in files:
        if platform is not None and platform_of(item) != platform:
            continue
        if skip_disabled and item.get("disabled"):
            continue
        wanted.append(item)
    return wanted


def _cache_id(files: list[dict[str, Any]], platform: str | None, instance: str) -> str:
    return f"{instance}:{platform or '*'}:{len(files)}:{_file_stamp(files)}"


def _parse_body(body: Any) -> dict[str, Any] | None:
    if isinstance(body, dict):
        return body
    if isinstance(body, str) and body.strip():
        try:
            data = json.loads(body)
        except ValueError:
            return None
        return data if isinstance(data, dict) else None
    return None


def _as_dict(value: Any) -> dict[str, Any] | None:
    return value if isinstance(value, dict) else None


def _first_number(*values: Any) -> float | None:
    for value in values:
        number = _number(value)
        if number is not None:
            return number
    return None


def _number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, dict) and "val" in value:
        return _number(value.get("val"))
    try:
        return float(str(value).strip().rstrip("%"))
    except ValueError:
        return None


def _clamp(value: float) -> float:
    return max(0.0, min(100.0, value))


def _first_str(*values: Any) -> str:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _file_stamp(files: list[dict[str, Any]]) -> str:
    parts = [str(item.get("auth_index") or item.get("name") or "") for item in files]
    parts.sort()
    return ",".join(parts[:80])
