"""把火山 Coding Plan 的 ``Result.QuotaUsage[]`` 解析为统一的额度窗口。

返回结构（实测）：

```json
{"Result": {"Status": "Running", "UpdateTimestamp": 1790132662,
  "QuotaUsage": [
    {"Level": "session", "Percent": 0.0701325, "ResetTimestamp": 1790146415, "Cap": 100},
    {"Level": "weekly",  "Percent": 0.009351,  "ResetTimestamp": 1790524800, "Cap": 100},
    {"Level": "monthly", "Percent": 0.0046755, "ResetTimestamp": 1792771199, "Cap": 100}
  ], "HasReward": false}}
```

- ``Percent`` 是**已用**百分比（0–100），剩余 = 100 - Percent。
- 各窗口无独立降级逻辑；``Status != Running`` 或缺 ``QuotaUsage`` 时返回空窗口，由调用方降级为文字。
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from ..config import VolcengineAccount
from ..model import AccountQuota, QuotaWindow

#: Level → (窗口 id, 展示名)
_LEVEL_META = {
    "session": ("volc-session", "5h"),
    "weekly": ("volc-week", "周"),
    "monthly": ("volc-month", "月"),
}

_LEVEL_ORDER = ("session", "weekly", "monthly")

#: 火山方舟小时额度（5h 窗口）规则：重置后不立即开始计时，而是在「重置后首次调用」时才
#: 滚动计时（见 tests/volcengine.md）。因此官方 ``ResetTimestamp`` 换算出的倒计时会误导
#: 用户（未调用时实际并未计时），统一改用固定文案说明该规则。
_HOURLY_RESET_NOTE = "将会在首次调用后进行重置计时"


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _reset_label(reset_ts: float | None) -> str:
    if reset_ts is None:
        return "-"
    delta = reset_ts - time.time()
    if delta <= 0:
        return "-"
    days, rem = divmod(int(delta), 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days > 0:
        return f"{days}d{hours}h" if hours else f"{days}d"
    if hours > 0:
        return f"{hours}h{minutes}m" if minutes else f"{hours}h"
    return f"{max(minutes, 1)}m"


def parse_coding_plan_usage(payload: dict[str, Any]) -> tuple[list[QuotaWindow], str]:
    """解析额度响应，返回 (窗口列表, 状态)。窗口方向为"已用"。

    仅当套餐 ``Status == Running`` 时才返回窗口；否则返回空窗口，由调用方降级展示。
    """
    result = payload.get("Result") if isinstance(payload, dict) else None
    if not isinstance(result, dict):
        return [], ""
    status = str(result.get("Status") or "")
    if status and status.lower() != "running":
        # 套餐未生效 / 停机 / 欠费：不展示历史额度，避免误判为正常。
        return [], status
    raw_items = result.get("QuotaUsage")
    if not isinstance(raw_items, list):
        return [], status

    by_level: dict[str, dict[str, Any]] = {}
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        level = str(item.get("Level") or "").strip().lower()
        if level:
            by_level[level] = item

    windows: list[QuotaWindow] = []
    for level in _LEVEL_ORDER:
        item = by_level.get(level)
        if item is None:
            continue
        window_id, label = _LEVEL_META[level]
        used = _num(item.get("Percent"))
        if used is None:
            used = _num(item.get("UsedPercent"))
        reset_ts = _num(item.get("ResetTimestamp"))
        if used is None:
            remaining_percent = None
        else:
            remaining_percent = max(0.0, 100.0 - used)
        windows.append(
            QuotaWindow(
                id=window_id,
                label=label,
                used_percent=used,
                remaining_percent=remaining_percent,
                # 火山的 Cap 是"百分比上限"（固定 100），不是绝对额度；不要写入 limit，
                # 否则 build_board 会把 limit 累加，文字模式会误报"绝对剩余 0/300"。
                reset_label=_reset_label(reset_ts),
                reset_at=reset_ts,
                reset_note=_HOURLY_RESET_NOTE if level == "session" else "",
                direction="used",
            )
        )
    return windows, status


_AGENT_WINDOWS = (
    ("AFPFiveHour", "volc-agent-5h", "5h"),
    ("AFPWeekly", "volc-agent-week", "周"),
    ("AFPMonthly", "volc-agent-month", "月"),
    ("AFPDaily", "volc-agent-day", "日"),
)


def parse_agent_plan_usage(payload: dict[str, Any]) -> list[QuotaWindow]:
    """解析 GetAFPUsage 响应为 Agent Plan 窗口（5h/周/月/日），方向为"已用"。"""
    result = payload.get("Result") if isinstance(payload, dict) else None
    if not isinstance(result, dict):
        return []
    windows: list[QuotaWindow] = []
    for field, window_id, label in _AGENT_WINDOWS:
        item = result.get(field)
        if not isinstance(item, dict):
            continue
        quota = _num(item.get("Quota"))
        used = _num(item.get("Used"))
        if quota is None or quota <= 0 or used is None:
            continue
        if field == "AFPDaily" and used <= 0:
            # AFPDaily 是「视觉/生图/视频模型专用」的每日硬顶（Quota 常高于周额度），
            # 纯文本/编程用量不会消耗它，故 Used 恒为 0；无消耗时不展示，避免误导性的 0% 条目。
            # 一旦产生视觉模型日消耗即自动出现，实现按档位/用量自适应。
            continue
        used_percent = max(0.0, min(100.0, used / quota * 100.0))
        reset_ts = _num(item.get("ResetTime"))
        if reset_ts is not None and reset_ts > 1e11:
            reset_ts = reset_ts / 1000.0
        windows.append(QuotaWindow(
            id=window_id, label=label, used_percent=used_percent,
            remaining_percent=max(0.0, 100.0 - used_percent),
            reset_label=_reset_label(reset_ts), reset_at=reset_ts,
            reset_note=_HOURLY_RESET_NOTE if field == "AFPFiveHour" else "",
            direction="used",
        ))
    return windows


def _iso_epoch(value: Any) -> float | None:
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def parse_plan(payload: dict[str, Any]) -> dict[str, Any]:
    """解析 GetPersonalPlan 响应为 {plan_type,status,start_at,end_at,auto_renew}；无套餐返回 {}。"""
    if not isinstance(payload, dict):
        return {}
    result = payload.get("Result")
    if not isinstance(result, dict):
        return {}
    info: dict[str, Any] = {}
    for key in ("PlanType", "plan_type", "Plan", "plan"):
        value = result.get(key)
        if value:
            info["plan_type"] = str(value).strip()
            break
    status = result.get("Status")
    if status:
        info["status"] = str(status)
    for out_key, in_key in (("start_at", "StartTime"), ("end_at", "EndTime")):
        epoch = _iso_epoch(result.get(in_key))
        if epoch is not None:
            info[out_key] = epoch
    auto = result.get("AutoRenew")
    if isinstance(auto, bool):
        info["auto_renew"] = auto
    return info


def format_expiry_label(ts: float) -> str:
    """到期时间：本地日期 + 剩余倒计时，如 `2026-10-23 (剩28天12小时)`。"""
    dt = datetime.fromtimestamp(ts).astimezone()
    date_str = dt.strftime("%Y-%m-%d")
    diff = ts - time.time()
    if diff <= 0:
        return f"{date_str} (已过期)"
    secs = int(diff)
    days, rem = divmod(secs, 86400)
    hours, _ = divmod(rem, 3600)
    if days > 0:
        rel = f"{days}天{hours}小时" if hours else f"{days}天"
    elif hours > 0:
        rel = f"{hours}小时"
    else:
        rel = "不足1小时"
    return f"{date_str} (剩{rel})"


def parse_personal_plan(payload: dict[str, Any]) -> str:
    """从 ``GetPersonalPlan`` 响应里提取套餐档位名（如 ``Lite`` / ``Pro``）。

    无 ``Result`` / ``PlanType`` 时返回空串（视为无套餐）。
    """
    return parse_plan(payload).get("plan_type", "")


def _build_report(
    account: VolcengineAccount,
    kind: str,
    plan_info: dict[str, Any] | None,
    windows: list[QuotaWindow],
    status: str,
) -> AccountQuota:
    """构造单张计划卡（kind ∈ {"coding","agent"}）：一种套餐一张卡。"""
    tier = str((plan_info or {}).get("plan_type") or "").strip()
    label = f"{kind.capitalize()} {tier}".strip()
    plan_badges = [(kind, label)] if (tier or windows) else []
    subscription_badges: list[tuple[str, str]] = []
    end_ts = (plan_info or {}).get("end_at")
    if end_ts:
        subscription_badges.append(
            (kind, f"{kind.capitalize()} 到期 {format_expiry_label(float(end_ts))}")
        )
    report = AccountQuota(
        platform="volcengine",
        name=account.name,
        auth_index="",
        plan=label if plan_badges else "",
        plan_badges=plan_badges,
        subscription_badges=subscription_badges,
        status=status or "unknown",
        windows=list(windows),
    )
    if report.windows:
        report.status = "ok"
    return report


def accounts_from_usage(
    account: VolcengineAccount,
    coding_payload: dict[str, Any] | None = None,
    agent_payload: dict[str, Any] | None = None,
    *,
    coding_plan: dict[str, Any] | None = None,
    agent_plan: dict[str, Any] | None = None,
) -> list[AccountQuota]:
    """把单个火山账号拆成 Coding / Agent 两张卡：各自独立的档位、额度与订阅到期。

    只在该套餐有档位信息或有额度窗口时才产出一张卡；两者皆无时返回空列表，
    由调用方（provider）统一降级为一张错误卡。
    """
    reports: list[AccountQuota] = []

    coding_windows, coding_status = parse_coding_plan_usage(coding_payload or {})
    coding_info = coding_plan or {}
    # 已知但未生效的套餐（停机/欠费）也要出一张卡，避免与另一张卡并存时问题被静默吞掉。
    coding_inactive = bool(coding_status) and coding_status.lower() != "running"
    if coding_info or coding_windows or coding_inactive:
        report = _build_report(account, "coding", coding_info, coding_windows, coding_status)
        if not report.windows and coding_inactive:
            report.error = f"套餐状态：{coding_status}"
        reports.append(report)

    agent_windows = parse_agent_plan_usage(agent_payload or {})
    agent_info = agent_plan or {}
    if agent_info or agent_windows:
        reports.append(
            _build_report(
                account, "agent", agent_info, agent_windows, str(agent_info.get("status") or "")
            )
        )

    return reports
