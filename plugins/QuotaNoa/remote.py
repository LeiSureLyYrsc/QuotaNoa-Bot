"""远程客户端额度 DTO → 本地 ``QuotaBoard`` 转换。

远程客户端（协议 v2）返回的账户 DTO 与本机 CPA/本地渠道产出同构，直接映射为
``model.AccountQuota`` / ``model.QuotaWindow`` 后即可复用现有渲染与汇总路径。
``instance`` 一律填客户端名（区别于 CPA 实例名），``auth_index`` 恒为空。
"""

from __future__ import annotations

import time
import unicodedata
from datetime import datetime, timezone
from typing import Any

from .model import AccountQuota, QuotaBoard, QuotaWindow, board_from_accounts

_INT_OR_NONE = (int, float)


def _clean(value: Any, limit: int) -> str:
    """清理远端字符串：去控制字符、折叠空白、限长。

    远端客户端返回的字段会进入聊天文本与渲染，必须视为不可信输入。
    """
    text = str(value or "")
    text = "".join(ch for ch in text if not unicodedata.category(ch).startswith("C"))
    text = " ".join(text.split())
    if limit > 0 and len(text) > limit:
        text = text[: limit - 1] + "…"
    return text


def _clean_token(value: Any, limit: int = 32) -> str:
    """把远端值清理为安全的单 token（小写字母/数字/-/_），用于渠道名等。"""
    text = _clean(value, limit).lower().replace("_", "-")
    allowed = "".join(ch for ch in text if ch.isalnum() or ch == "-")
    return allowed[:limit]


def _opt_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, _INT_OR_NONE):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _opt_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _direction(value: Any) -> str:
    text = _clean_token(value, 16)
    return text if text in {"remaining", "used"} else ""


def _window_from_dto(dto: Any) -> QuotaWindow:
    return QuotaWindow(
        id=_clean(getattr(dto, "id", ""), 48),
        label=_clean(getattr(dto, "label", ""), 48),
        used_percent=_opt_float(getattr(dto, "used_percent", None)),
        remaining_percent=_opt_float(getattr(dto, "remaining_percent", None)),
        remaining=_opt_float(getattr(dto, "remaining", None)),
        limit=_opt_float(getattr(dto, "limit", None)),
        reset_label=_clean(getattr(dto, "reset_label", "-") or "-", 32),
        reset_at=_opt_float(getattr(dto, "reset_at", None)),
        reset_note=_clean(getattr(dto, "reset_note", ""), 120),
        direction=_direction(getattr(dto, "direction", "")),
    )


def _badges(value: Any) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    if isinstance(value, (list, tuple)):
        for item in value:
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                result.append((_clean_token(item[0], 16), _clean(item[1], 48)))
    return result


def _account_from_dto(dto: Any, *, client_name: str) -> AccountQuota:
    windows = [_window_from_dto(window) for window in (getattr(dto, "windows", None) or [])]
    platform = _clean_token(getattr(dto, "platform", ""), 32) or "other"
    return AccountQuota(
        platform=platform,
        name=_clean(getattr(dto, "name", ""), 64) or "(unknown)",
        auth_index="",
        plan=_clean(getattr(dto, "plan", ""), 48),
        plan_badges=_badges(getattr(dto, "plan_badges", None)),
        subscription_badges=_badges(getattr(dto, "subscription_badges", None)),
        status=_clean_token(getattr(dto, "status", "unknown"), 16) or "unknown",
        error=_clean(getattr(dto, "error", ""), 200),
        windows=windows,
        disabled=bool(getattr(dto, "disabled", False)),
        cooling=bool(getattr(dto, "cooling", False)),
        instance=client_name,
        subscription_expires_at=_opt_float(getattr(dto, "subscription_expires_at", None)),
        subscription_expires_label=_clean(getattr(dto, "subscription_expires_label", ""), 48),
        reset_credits=_opt_int(getattr(dto, "reset_credits", None)),
    )


def _remote_fetched_at(result: Any) -> float | None:
    """由客户端上报的元数据推算本板数据的真实取数时间。

    优先用客户端自算的 ``cache_age``（免疫两端时钟偏差）；缺失时回退解析绝对
    ``queried_at``。都没有则返回 ``None``（展示为未知）。
    """
    age = _opt_float(getattr(result, "cache_age", None))
    if age is not None:
        return time.time() - max(0.0, age)
    raw = getattr(result, "queried_at", "") or ""
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    return None


def board_from_result(result: Any) -> QuotaBoard:
    """把远程 ``QuotaQueryResult`` 转为本机 ``QuotaBoard``。"""
    client_name = str(getattr(result, "client_name", "") or "")
    accounts = [
        _account_from_dto(dto, client_name=client_name)
        for dto in (getattr(result, "accounts", None) or [])
    ]
    board = board_from_accounts(accounts, cached=bool(getattr(result, "cached", False)))
    board.fetched_at = _remote_fetched_at(result)
    return board
