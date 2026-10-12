"""泛式额度适配器：未专门适配的 provider 也能显示额度条。

三级降级（按优先级，命中即返回）：

1. ``POST /v0/management/quota/fetch`` —— CPA 的厂商无关归一化额度契约。CPA 侧
   要么注册了 ``QuotaProvider`` 插件（``supports_quota`` + ``quota_provider``），
   要么凭证 metadata 里带了声明式 ``quota_probe``；两者皆无时返回 ``501``。
2. ``/auth-files`` 条目的 ``quota.signals`` —— CPA 被动观测到的上游配额水位
   （仅 ``claude`` / ``codex`` 采集，其余渠道为空）。
3. 配置内 ``generic.probes`` —— Bot 侧声明式探测，语法镜像 CPA 的 ``quota_probe``。

三级都拿不到时**不报错**，只留空窗口（卡片显示「无上游额度」），因为「未适配」
本身是正常状态而非故障。只有用户在 ``generic.probes`` 里显式配置了探测却失败，
才如实上报错误。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Mapping

from ..model import AccountQuota, QuotaWindow, sort_windows
from .client import CPAError, ManagementClient

#: ``/quota/fetch`` 返回「没有可用额度 provider」的 HTTP 状态。
NO_PROVIDER_STATUS = 501

#: 单个探测响应体的字节上限（防止误配到巨型端点）。
MAX_PROBE_BYTES = 262144

#: 信号键的 snake_case 后缀 → 语义（Devin 形态：``daily_quota_remaining_percent``）。
_SNAKE_SIGNAL_SUFFIXES: tuple[tuple[str, str], ...] = (
    ("_quota_remaining_percent", "remaining_percent"),
    ("_quota_reset_at", "reset_at"),
)

#: 信号键的 kebab-case 后缀 → 语义（Codex/Claude 形态：``x-codex-primary-used-percent``）。
_DASH_SIGNAL_SUFFIXES: tuple[tuple[str, str], ...] = (
    ("-reset-after-seconds", "reset_after_seconds"),
    ("-remaining-percent", "remaining_percent"),
    ("-used-percent", "used_percent"),
    ("-limit-reached", "limit_reached"),
    ("-window-minutes", "window_minutes"),
    ("-plan-type", "plan_type"),
    ("-reset-at", "reset_at"),
    ("-allowed", "allowed"),
)

#: 信号前缀（去掉厂商命名空间后）→ 展示名。
_SIGNAL_PREFIX_LABELS: dict[str, str] = {
    "primary": "主额度",
    "secondary": "次额度",
    "daily": "日额度",
    "weekly": "周额度",
    "monthly": "月额度",
    "hourly": "小时额度",
    "5h": "5h",
    "five-hour": "5h",
    "week": "周额度",
    "month": "月额度",
    "day": "日额度",
    "total": "总额度",
}

#: 需要剥离的厂商命名空间前缀（信号键的冗余部分）。
_SIGNAL_NAMESPACES = ("anthropic-ratelimit-unified-", "x-codex-", "x-ratelimit-", "codex-")


@dataclass
class GenericQuota:
    """泛式适配结果：套餐名 + 窗口列表（含 ``kind="metric"`` 的指标行）。"""

    plan: str = ""
    windows: list[QuotaWindow] = field(default_factory=list)


def _quota_module():
    """延迟导入 ``cpa.quota``：本模块被它导入，模块级互引会成环。"""
    from . import quota

    return quota


def _clamp(value: float) -> float:
    return max(0.0, min(100.0, value))


def _number(value: Any) -> float | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, Mapping) and "val" in value:
        return _number(value.get("val"))
    try:
        return float(str(value).strip().rstrip("%"))
    except ValueError:
        return None


def _first_str(*values: Any) -> str:
    for value in values:
        if isinstance(value, str):
            text = value.strip()
            if text:
                return text
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            return str(value)
    return ""


def _slug(value: Any, fallback: str = "quota") -> str:
    text = re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-")
    return text[:40] or fallback


def _unique_id(seen: set[str], base: str) -> str:
    candidate = base
    index = 2
    while candidate in seen:
        candidate = f"{base}-{index}"
        index += 1
    seen.add(candidate)
    return candidate


def _fraction(*values: Any) -> float | None:
    """把剩余比例归一为 0..1；``>1`` 时按百分数处理。"""
    number: float | None = None
    for value in values:
        number = _number(value)
        if number is not None:
            break
    if number is None:
        return None
    if number > 1:
        number = number / 100.0
    return max(0.0, min(1.0, number))


def _parse_ts(value: Any) -> float | None:
    return _quota_module()._parse_any_ts(value)


def _reset_label(value: Any) -> str:
    return _quota_module()._human_reset(value)


def _metric_text(value: float, fmt: str, unit: str, currency: str) -> str:
    if fmt == "currency" and currency:
        return f"{currency} {value:,.2f}"
    if float(value).is_integer():
        text = f"{int(value):,}"
    else:
        text = f"{value:,.2f}".rstrip("0").rstrip(".")
    return f"{text} {unit}".strip()


def _plan_from_values(*values: Any) -> str:
    sanitize = _quota_module()._sanitize_plan
    for value in values:
        text = sanitize(value)
        if text:
            return text
    return ""


def _window_from_fraction(
    *,
    window_id: str,
    label: str,
    fraction: float,
    reset_value: Any,
) -> QuotaWindow:
    return QuotaWindow(
        id=window_id,
        label=label,
        remaining_percent=_clamp(fraction * 100.0),
        used_percent=_clamp(100.0 - fraction * 100.0),
        reset_label=_reset_label(reset_value),
        reset_at=_parse_ts(reset_value),
        direction="remaining",
    )


# --------------------------------------------------------------------------- #
# L1：POST /v0/management/quota/fetch 归一化契约
# --------------------------------------------------------------------------- #


def normalize_quota_fetch(payload: Any, *, channel: str) -> GenericQuota | None:
    """解析 CPA ``/quota/fetch`` 的 ``QuotaFetchResponse``。

    形状（``sdk/pluginapi/types.go``，camel 与 snake 双写）：

    ``{"subscription": {...}, "summary": [...], "groups": [...],
    "serverTimeOffsetMs": 0}``
    """
    if not isinstance(payload, Mapping):
        return None

    subscription = payload.get("subscription")
    plan = ""
    if isinstance(subscription, Mapping):
        plan = _plan_from_values(
            subscription.get("plan"),
            subscription.get("tierName"),
            subscription.get("tier_name"),
            subscription.get("tierId"),
            subscription.get("tier_id"),
        )

    seen: set[str] = set()
    windows: list[QuotaWindow] = []

    groups = payload.get("groups")
    group_list = [item for item in groups if isinstance(item, Mapping)] if isinstance(groups, list) else []
    for gindex, group in enumerate(group_list, start=1):
        group_name = _first_str(group.get("displayName"), group.get("display_name"))
        buckets = group.get("buckets")
        if not isinstance(buckets, list):
            continue
        for bindex, bucket in enumerate(buckets, start=1):
            if not isinstance(bucket, Mapping):
                continue
            fraction = _fraction(bucket.get("remainingFraction"), bucket.get("remaining_fraction"))
            if fraction is None:
                continue
            window_name = _first_str(bucket.get("window"))
            description = _first_str(bucket.get("description"))
            label = description or window_name or f"配额 {bindex}"
            if group_name and len(group_list) > 1:
                label = f"{group_name} · {label}"
            reset_value = bucket.get("resetTime") or bucket.get("reset_time")
            windows.append(
                _window_from_fraction(
                    window_id=_unique_id(
                        seen,
                        f"{channel}-{_slug(window_name or description or f'{gindex}-{bindex}')}",
                    ),
                    label=label,
                    fraction=fraction,
                    reset_value=reset_value,
                )
            )

    summary = payload.get("summary")
    if isinstance(summary, list):
        for item in summary:
            if not isinstance(item, Mapping):
                continue
            label = _first_str(item.get("label"), item.get("key"))
            value = _number(item.get("value"))
            if not label or value is None:
                continue
            windows.append(
                QuotaWindow(
                    id=_unique_id(seen, f"{channel}-metric-{_slug(label, 'metric')}"),
                    label=label,
                    reset_note=_metric_text(
                        value,
                        _first_str(item.get("format")),
                        _first_str(item.get("unit")),
                        _first_str(item.get("currency")),
                    ),
                    kind="metric",
                    direction="remaining",
                )
            )

    if not windows and not plan:
        return None
    return GenericQuota(plan=plan, windows=windows)


# --------------------------------------------------------------------------- #
# L2：/auth-files 的 quota.signals 被动观测
# --------------------------------------------------------------------------- #


def _split_signal_key(key: str) -> tuple[str, str] | None:
    lowered = str(key or "").strip().lower()
    if not lowered:
        return None
    for suffix, kind in _SNAKE_SIGNAL_SUFFIXES:
        if lowered.endswith(suffix):
            return lowered[: -len(suffix)].strip("_-"), kind
    dashed = lowered.replace("_", "-")
    for suffix, kind in _DASH_SIGNAL_SUFFIXES:
        if dashed.endswith(suffix):
            return dashed[: -len(suffix)].strip("-"), kind
    return None


def _signal_prefix_label(prefix: str) -> str:
    narrowed = prefix
    for namespace in _SIGNAL_NAMESPACES:
        if narrowed.startswith(namespace):
            narrowed = narrowed[len(namespace) :]
            break
    known = _SIGNAL_PREFIX_LABELS.get(narrowed)
    if known:
        return known
    words = [word for word in narrowed.split("-") if word]
    if not words:
        return "额度"
    return " ".join(word.upper() if len(word) <= 2 and word.isalpha() else word.capitalize() for word in words)


def _is_truthy(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return text in {"1", "true", "yes", "on"}


def windows_from_signals(signals: Any, *, channel: str) -> GenericQuota | None:
    """把 ``quota.signals`` 字符串映射解析为窗口列表。

    按前缀分组，每个前缀出一根额度条；``*-plan-type`` 作为套餐名。
    """
    if not isinstance(signals, Mapping) or not signals:
        return None

    grouped: dict[str, dict[str, Any]] = {}
    plan = ""
    for raw_key, raw_value in signals.items():
        split = _split_signal_key(raw_key)
        if split is None:
            continue
        prefix, kind = split
        if kind == "plan_type":
            plan = _plan_from_values(raw_value) or plan
            continue
        if not prefix:
            continue
        grouped.setdefault(prefix, {})[kind] = raw_value

    seen: set[str] = set()
    windows: list[QuotaWindow] = []
    for prefix in sorted(grouped):
        entry = grouped[prefix]
        window_id = _unique_id(seen, f"{channel}-{_slug(prefix, 'signal')}")
        label = _signal_prefix_label(prefix)
        remaining_pct = _number(entry.get("remaining_percent"))
        used_pct = _number(entry.get("used_percent"))
        reset_value = entry.get("reset_at")
        reset_seconds = _number(entry.get("reset_after_seconds"))

        window = QuotaWindow(id=window_id, label=label, direction="remaining")
        if remaining_pct is not None:
            window.remaining_percent = _clamp(remaining_pct)
            window.used_percent = _clamp(100.0 - window.remaining_percent)
        elif used_pct is not None:
            window.direction = "used"
            window.used_percent = _clamp(used_pct)
            window.remaining_percent = _clamp(100.0 - window.used_percent)

        if entry.get("limit_reached") is not None and _is_truthy(entry.get("limit_reached")):
            window.direction = "used"
            window.used_percent = 100.0
            window.remaining_percent = 0.0
        if entry.get("allowed") is not None and not _is_truthy(entry.get("allowed")):
            window.direction = "used"
            window.used_percent = 100.0
            window.remaining_percent = 0.0

        reset_at = _parse_ts(reset_value) if reset_value else None
        if reset_at is None and reset_seconds:
            reset_at = time.time() + reset_seconds
        if reset_at is not None:
            window.reset_at = reset_at
            window.reset_label = _quota_module()._relative_from_ts(reset_at)

        if window.remaining_percent is None and window.used_percent is None:
            continue
        windows.append(window)

    if not windows and not plan:
        return None
    return GenericQuota(plan=plan, windows=windows)


def signals_of(file: Mapping[str, Any]) -> Mapping[str, Any]:
    """从 ``/auth-files`` 条目安全取出 ``quota.signals``。"""
    quota = file.get("quota") if isinstance(file, Mapping) else None
    if not isinstance(quota, Mapping):
        return {}
    signals = quota.get("signals")
    return signals if isinstance(signals, Mapping) else {}


# --------------------------------------------------------------------------- #
# L3：配置内声明式探测（镜像 CPA 的 quota_probe）
# --------------------------------------------------------------------------- #

_INDEX_RE = re.compile(r"\[(\d+)\]")


def _path_get(data: Any, path: str) -> Any:
    """按 ``a.b[0].c`` 形式取值；任一段缺失返回 ``None``。"""
    text = str(path or "").strip()
    if not text:
        return None
    node = data
    for part in _INDEX_RE.sub(r".\1", text).split("."):
        if not part:
            continue
        if isinstance(node, Mapping):
            node = node.get(part)
        elif isinstance(node, list):
            try:
                node = node[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
        if node is None:
            return None
    return node


def _resolve(value: Any, source: Any) -> Any:
    """字符串先按路径解析；解析不到则按字面量使用（与 CPA 语义一致）。"""
    if not isinstance(value, str):
        return value
    resolved = _path_get(source, value)
    if resolved is not None and not isinstance(resolved, (Mapping, list)):
        return resolved
    return value


def _text_of(value: Any, source: Any) -> str:
    resolved = _resolve(value, source)
    if isinstance(resolved, (Mapping, list)):
        return ""
    return str(resolved or "").strip() if resolved is not None else ""


def _buckets_from_path(
    source: Any,
    group: Mapping[str, Any],
    *,
    channel: str,
    seen: set[str],
    group_name: str,
    multiple_groups: bool,
) -> list[QuotaWindow]:
    buckets_path = _first_str(group.get("buckets_path"))
    if not buckets_path:
        return []
    items = _path_get(source, buckets_path)
    if not isinstance(items, list):
        return []

    window_key = _first_str(group.get("window_key")) or "window"
    fraction_key = _first_str(group.get("remaining_fraction_key"))
    if not fraction_key and not group.get("remaining_amount_key"):
        fraction_key = "remaining_fraction"
    amount_key = _first_str(group.get("remaining_amount_key"))
    total_key = _first_str(group.get("total_amount_key"))
    reset_key = _first_str(group.get("reset_time_key")) or "reset_time"
    description_key = _first_str(group.get("description_key")) or "description"

    windows: list[QuotaWindow] = []
    for index, item in enumerate(items, start=1):
        if not isinstance(item, Mapping):
            continue
        fraction = _fraction(_path_get(item, fraction_key)) if fraction_key else None
        if fraction is None and amount_key and total_key:
            remaining = _number(_path_get(item, amount_key))
            total = _number(_path_get(item, total_key))
            if remaining is not None and total is not None and total > 0:
                fraction = max(0.0, min(1.0, remaining / total))
        if fraction is None:
            continue
        window_name = _text_of(_path_get(item, window_key), source) if window_key else ""
        description = _text_of(_path_get(item, description_key), source) if description_key else ""
        label = description or window_name or f"配额 {index}"
        if group_name and multiple_groups:
            label = f"{group_name} · {label}"
        windows.append(
            _window_from_fraction(
                window_id=_unique_id(seen, f"{channel}-{_slug(window_name or description or index)}"),
                label=label,
                fraction=fraction,
                reset_value=_path_get(item, reset_key) if reset_key else None,
            )
        )
    return windows


def _buckets_explicit(
    source: Any,
    group: Mapping[str, Any],
    *,
    channel: str,
    seen: set[str],
    group_name: str,
    multiple_groups: bool,
) -> list[QuotaWindow]:
    bucket_specs = group.get("buckets")
    if not isinstance(bucket_specs, list):
        return []

    windows: list[QuotaWindow] = []
    for index, spec in enumerate(bucket_specs, start=1):
        if not isinstance(spec, Mapping):
            continue
        fraction = _fraction(_resolve(spec.get("remaining_fraction"), source))
        if fraction is None and spec.get("remaining_amount") is not None and spec.get("total_amount") is not None:
            remaining = _number(_resolve(spec.get("remaining_amount"), source))
            total = _number(_resolve(spec.get("total_amount"), source))
            if remaining is not None and total is not None and total > 0:
                fraction = max(0.0, min(1.0, remaining / total))
        if fraction is None:
            continue
        window_name = _text_of(spec.get("window"), source)
        description = _text_of(spec.get("description"), source)
        label = description or window_name or f"配额 {index}"
        if group_name and multiple_groups:
            label = f"{group_name} · {label}"
        windows.append(
            _window_from_fraction(
                window_id=_unique_id(seen, f"{channel}-{_slug(window_name or description or index)}"),
                label=label,
                fraction=fraction,
                reset_value=_resolve(spec.get("reset_time"), source),
            )
        )
    return windows


def map_probe_response(source: Any, mapping: Mapping[str, Any], *, channel: str) -> GenericQuota | None:
    """按声明式 ``mapping`` 把任意上游 JSON 映射为统一额度。

    ``mapping`` 语法与 CPA 的 ``quota_probe.mapping`` 对齐：

    - ``plan`` / ``tier_name`` / ``tierName`` / ``tier_id`` / ``tierId``：套餐路径；
    - ``groups[].display_name``：组名（字面量，或路径求值后回退字面量）；
    - ``groups[].buckets_path``：数组路径 + 各 ``*_key`` 指定字段名；
    - ``groups[].buckets[]``：显式逐桶声明，字段值为路径或字面量；
    - ``summary[]``：``{key, label, value, unit, format, currency}``，值为路径或字面量。
    """
    if not isinstance(mapping, Mapping) or not mapping:
        return normalize_quota_fetch(source, channel=channel)

    plan = ""
    for key in ("plan", "tier_name", "tierName", "tier_id", "tierId"):
        raw = mapping.get(key)
        if not isinstance(raw, str) or not raw:
            continue
        # 与 CPA 一致：套餐路径必须真解析出值，不做字面量回退（否则会把路径当套餐名）。
        resolved = _path_get(source, raw)
        if resolved is None or isinstance(resolved, (Mapping, list)):
            continue
        plan = _plan_from_values(resolved)
        if plan:
            break

    seen: set[str] = set()
    windows: list[QuotaWindow] = []

    groups = mapping.get("groups")
    group_list = [item for item in groups if isinstance(item, Mapping)] if isinstance(groups, list) else []
    multiple = len(group_list) > 1
    for group in group_list:
        group_name = _text_of(group.get("display_name") or group.get("displayName"), source)
        windows.extend(
            _buckets_from_path(
                source,
                group,
                channel=channel,
                seen=seen,
                group_name=group_name,
                multiple_groups=multiple,
            )
        )
        windows.extend(
            _buckets_explicit(
                source,
                group,
                channel=channel,
                seen=seen,
                group_name=group_name,
                multiple_groups=multiple,
            )
        )

    summary = mapping.get("summary")
    if isinstance(summary, list):
        for spec in summary:
            if not isinstance(spec, Mapping):
                continue
            label = _text_of(spec.get("label"), source) or _text_of(spec.get("key"), source)
            value = _number(_resolve(spec.get("value"), source))
            if not label or value is None:
                continue
            windows.append(
                QuotaWindow(
                    id=_unique_id(seen, f"{channel}-metric-{_slug(label, 'metric')}"),
                    label=label,
                    reset_note=_metric_text(
                        value,
                        _text_of(spec.get("format"), source),
                        _text_of(spec.get("unit"), source),
                        _text_of(spec.get("currency"), source),
                    ),
                    kind="metric",
                    direction="remaining",
                )
            )

    if not windows and not plan:
        return None
    return GenericQuota(plan=plan, windows=windows)


def _parse_json_body(body: Any) -> Any:
    if isinstance(body, (Mapping, list)):
        return body
    if isinstance(body, str) and body.strip():
        if len(body) > MAX_PROBE_BYTES:
            raise CPAError("探测响应体过大，已拒绝解析。")
        try:
            return json.loads(body)
        except ValueError:
            return None
    return None


async def probe_generic_quota(
    client: ManagementClient,
    cfg: Any,
    report: AccountQuota,
    probe: Any,
    *,
    channel: str,
) -> GenericQuota:
    """执行一条声明的探测；失败抛 :class:`CPAError`（消息可直接发管理员）。"""
    url = str(getattr(probe, "url", "") or "").strip()
    if not url:
        raise CPAError(f"generic.probes.{channel} 未配置 url。")
    method = (str(getattr(probe, "method", "") or "GET")).strip().upper() or "GET"
    header = {str(key): str(value) for key, value in dict(getattr(probe, "header", {}) or {}).items()}
    data = str(getattr(probe, "data", "") or "").strip() or None

    response = await client.api_call(
        report.auth_index,
        method,
        url,
        header=header,
        data=data,
        timeout=cfg.quota_timeout,
        allow_configured=True,
    )
    status = int(response.get("status_code") or response.get("statusCode") or 0)
    if status and not (200 <= status < 300):
        raise CPAError(f"探测上游 HTTP {status}")
    parsed = _parse_json_body(response.get("body"))
    if parsed is None:
        raise CPAError("探测上游返回无法解析的 JSON。")

    mapping = dict(getattr(probe, "mapping", {}) or {})
    result = map_probe_response(parsed, mapping, channel=channel)
    if result is None:
        raise CPAError("探测响应未匹配到任何额度字段，请检查 generic.probes 的 mapping。")
    return result


# --------------------------------------------------------------------------- #
# 降级编排
# --------------------------------------------------------------------------- #


def _apply(report: AccountQuota, result: GenericQuota) -> None:
    report.windows = sort_windows(result.windows)
    report.plan = _plan_from_values(result.plan) or report.plan
    report.error = ""
    report.status = _quota_module()._status_from_windows(report.windows, report)


async def fill_generic(
    client: ManagementClient,
    cfg: Any,
    file: dict[str, Any],
    report: AccountQuota,
) -> None:
    """对一个「未专门适配」的凭证按三级降级填充额度。

    三级全空且未配置探测时静默返回（保留空窗口 = 「无上游额度」），不产生错误卡。
    """
    from .. import state

    generic = state.get_snapshot().generic
    channel = report.platform

    if generic.use_quota_fetch:
        payload = await client.quota_fetch(report.auth_index)
        if payload:
            fetched = normalize_quota_fetch(payload, channel=channel)
            if fetched is not None and fetched.windows:
                _apply(report, fetched)
                return

    if generic.use_signals:
        observed = windows_from_signals(signals_of(file), channel=channel)
        if observed is not None and observed.windows:
            _apply(report, observed)
            return

    probe = generic.probes.get(channel)
    if probe is None:
        return
    result = await probe_generic_quota(client, cfg, report, probe, channel=channel)
    _apply(report, result)
