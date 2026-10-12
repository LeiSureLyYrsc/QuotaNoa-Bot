"""分渠道账号别名与自定义渠道关键字。

存储文件默认 ``data/quotanoa_aliases.json``，**首次运行会自动生成可编辑模板**，
结构为“渠道 → 身份键 → 别名”：

```json
{
  "_readme": [ "用法提示…" ],
  "*":           { "user@example.com": "AG-1" },
  "antigravity": { "user@example.com": "AG-1" },
  "volcengine":  { "volc-1": "火山主号" },
  "channel_keywords": { "agy": "antigravity", "gpt": "codex" }
}
```

- ``*`` 为全局桶，任何渠道在自身桶未命中时会回退到全局桶。
- ``channel_keywords`` 为**保留键**：自定义渠道查询关键字 → canonical 渠道名，
  仅影响 ``/quotanoa``、``/cpa quota`` 的位置参数解析（不影响出图 / 文本渲染）。
  例如写入 ``{"agy": "antigravity"}`` 后 ``/quotanoa agy`` 等同 ``/quotanoa antigravity``。
  内置关键字优先，本表只补足 / 覆盖未内置的组合。
- 以 ``_`` 开头的键（如 ``_readme``）为**注释/元数据**，读取时忽略。
- 旧版扁平格式（``{"身份键": "别名"}``）读取时自动归一化进 ``*`` 桶。
- **热重载**：本模块自带 mtime 检查（1 秒节流），在聊天里手改文件后下一条命令即生效；
  文件被删除时也会自动重新生成模板。
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .model import PLATFORM_ORDER, channel_of, platform_key

_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

GLOBAL_BUCKET = "*"
#: 保留键：自定义渠道查询关键字表（不参与别名列表展示）。
CHANNEL_KEYWORDS_KEY = "channel_keywords"
#: 保留键：模板里的用法注释（读取时忽略）。
README_KEY = "_readme"
_CHECK_INTERVAL = 1.0

_store: dict[str, dict[str, str]] = {}
_channel_keywords: dict[str, str] = {}
_loaded = False
_memory_only = False
_signature: tuple[int, int] | None = None
_checked_at = 0.0


def default_aliases_dict() -> dict[str, Any]:
    """返回别名文件的默认模板（含用法注释与各渠道空桶），便于用户手改。"""
    payload: dict[str, Any] = {
        README_KEY: [
            "账号别名：渠道 → 身份键 → 显示名。",
            "身份键可用 auth_index、凭证文件名（可省 .json）、邮箱，或其前缀。",
            f"“{GLOBAL_BUCKET}” 为全局桶：任何渠道自身桶未命中时回退到这里。",
            f"{CHANNEL_KEYWORDS_KEY}：自定义渠道查询关键字（只影响 /quotanoa 解析，不改出图/文本）。",
            "值可写内置渠道名，也可写未适配 provider 名（如 devin / iflow）以启用泛式额度查询。",
            "保存后自动热重载：下一条 /quotanoa 即生效，无需重启。",
        ],
        CHANNEL_KEYWORDS_KEY: {"agy": "antigravity"},
    }
    for channel in PLATFORM_ORDER:
        if channel == "other":
            continue
        payload.setdefault(channel, {})
    payload.setdefault(GLOBAL_BUCKET, {})
    return payload



# --------------------------------------------------------------------------- #
# 身份键
# --------------------------------------------------------------------------- #


def identity_keys(file: Mapping[str, Any]) -> list[str]:
    keys: list[str] = []
    seen: set[str] = set()
    for field in ("auth_index", "email", "name", "id", "label"):
        raw = str(file.get(field) or "").strip()
        if not raw:
            continue
        candidates = [raw]
        if raw.lower().endswith(".json"):
            candidates.append(raw[:-5])
        for item in candidates:
            lowered = item.lower()
            if lowered in seen:
                continue
            seen.add(lowered)
            keys.append(item)
    scoped = _scoped_email_key(file)
    if scoped and scoped.lower() not in seen:
        keys.append(scoped)
    return keys


def unique_identity_keys(file: Mapping[str, Any]) -> list[str]:
    """跨平台同邮箱时只绑本条凭证，避免别名串号。"""
    keys: list[str] = []
    seen: set[str] = set()
    for field in ("auth_index", "name"):
        raw = str(file.get(field) or "").strip()
        if not raw:
            continue
        candidates = [raw]
        if raw.lower().endswith(".json"):
            candidates.append(raw[:-5])
        for item in candidates:
            lowered = item.lower()
            if lowered in seen:
                continue
            seen.add(lowered)
            keys.append(item)
    scoped = _scoped_email_key(file)
    if scoped and scoped.lower() not in seen:
        keys.append(scoped)
    if keys:
        return keys
    return identity_keys(file)


def _scoped_email_key(file: Mapping[str, Any]) -> str:
    email = str(file.get("email") or file.get("account") or "").strip()
    if "@" not in email:
        return ""
    provider = str(file.get("provider") or file.get("type") or "").strip().lower()
    if not provider:
        return ""
    return f"{provider}:{email}"


# --------------------------------------------------------------------------- #
# 读取
# --------------------------------------------------------------------------- #


def aliases_path() -> Path:
    from . import state

    try:
        raw = state.get_snapshot().aliases_file
    except Exception:
        from .config import DEFAULT_ALIASES_FILE

        raw = DEFAULT_ALIASES_FILE
    return Path(raw).expanduser().resolve()


def _read_signature(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return (stat.st_mtime_ns, stat.st_size)


def _read_file(path: Path) -> Any:
    try:
        import json

        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _clean_map(data: Any) -> dict[str, str]:
    cleaned: dict[str, str] = {}
    if not isinstance(data, Mapping):
        return cleaned
    for key, value in data.items():
        name = str(key).strip()
        alias = str(value).strip()
        if name and alias:
            cleaned[name] = alias
    return cleaned


def _normalize_store(data: Any) -> dict[str, dict[str, str]]:
    if not isinstance(data, Mapping):
        return {}
    nested: dict[str, dict[str, str]] = {}
    flat: dict[str, str] = {}
    for key, value in data.items():
        name = str(key).strip()
        if not name or name.startswith("_") or name == CHANNEL_KEYWORDS_KEY:
            # 注释 / 保留键单独处理，不能当作渠道桶，否则会污染别名列表。
            continue
        if isinstance(value, Mapping):
            bucket = _clean_map(value)
            if bucket:
                nested.setdefault(name, {}).update(bucket)
        else:
            alias = str(value).strip()
            if alias:
                flat[name] = alias
    if flat:
        nested.setdefault(GLOBAL_BUCKET, {}).update(flat)
    return nested


def _normalize_channel_keywords(data: Any) -> dict[str, str]:
    """解析 ``channel_keywords`` 保留键：关键字 → 渠道名。

    接受内置渠道与**未适配 provider 的动态 token**，因此可以给任意凭证
    注册查询关键字（如 ``{"agy": "antigravity", "dv": "devin"}``）。
    """
    if not isinstance(data, Mapping):
        return {}
    raw = data.get(CHANNEL_KEYWORDS_KEY)
    if not isinstance(raw, Mapping):
        return {}
    cleaned: dict[str, str] = {}
    for key, value in raw.items():
        keyword = str(key).strip()
        if not keyword:
            continue
        channel = platform_key(str(value))
        if channel:
            cleaned[keyword.lower()] = channel
    return cleaned


def _ensure_file(path: Path) -> bool:
    """确保别名文件存在；缺失时生成可编辑模板。返回是否新建。"""
    if path.is_file():
        return False
    try:
        from .config import atomic_write_json

        atomic_write_json(path, default_aliases_dict())
        return True
    except Exception:
        # 只读目录 / 权限不足时静默降级：本次以空表运行。
        return False


def ensure_aliases_file() -> Path:
    """确保别名文件存在（缺失时生成可编辑模板），返回其路径。

    供插件启动时调用，使用户在首次运行后即可直接编辑别名文件。
    """
    path = aliases_path()
    _ensure_file(path)
    return path


def _ensure_loaded() -> None:
    global _store, _channel_keywords, _signature, _checked_at, _loaded
    if _memory_only:
        return
    now = time.monotonic()
    if _loaded and now - _checked_at < _CHECK_INTERVAL:
        return
    _checked_at = now
    path = aliases_path()
    _ensure_file(path)
    signature = _read_signature(path)
    if _loaded and signature == _signature:
        return
    raw = _read_file(path)
    _store = _normalize_store(raw)
    _channel_keywords = _normalize_channel_keywords(raw)
    _signature = signature
    _loaded = True


def load_aliases() -> dict[str, dict[str, str]]:
    _ensure_loaded()
    return _store


def custom_channel_keywords() -> dict[str, str]:
    """返回用户自定义的渠道关键字表（关键字 → canonical 渠道名）。"""
    _ensure_loaded()
    return _channel_keywords


def _merged_lookup(channel: str) -> dict[str, str]:
    lookup: dict[str, str] = {}
    for key, alias in _store.get(GLOBAL_BUCKET, {}).items():
        lookup[key.lower()] = alias
    for key, alias in _store.get(channel, {}).items():
        lookup[key.lower()] = alias
    return lookup


# --------------------------------------------------------------------------- #
# 解析
# --------------------------------------------------------------------------- #


def resolve_alias_for_keys(channel: str, keys: Iterable[str]) -> str:
    store = load_aliases()
    if not store:
        return ""
    lookup = _merged_lookup(channel or GLOBAL_BUCKET)
    for key in keys:
        text = str(key).strip()
        if not text:
            continue
        alias = lookup.get(text.lower())
        if alias:
            return alias
    return ""


def resolve_alias(file: Mapping[str, Any], *, channel: str | None = None) -> str:
    return resolve_alias_for_keys(channel or channel_of(file), identity_keys(file))


def public_fallback(file: Mapping[str, Any]) -> str:
    label = str(file.get("label") or "").strip()
    if label and "@" not in label and not _EMAIL.search(label):
        return label
    provider = str(file.get("provider") or file.get("type") or "acct").strip() or "acct"
    index = str(file.get("auth_index") or "")
    short = index[:4] if index else "????"
    return f"{provider}-{short}"


# --------------------------------------------------------------------------- #
# 写入
# --------------------------------------------------------------------------- #


def set_alias_for_keys(channel: str, keys: Iterable[str], alias: str) -> str:
    name = (alias or "").strip()
    if not name:
        raise ValueError("别名不能为空。")
    if "@" in name:
        raise ValueError("别名不要包含邮箱。")
    cleaned = [str(key).strip() for key in keys if str(key).strip()]
    if not cleaned:
        raise ValueError("该账号没有可用于绑定的身份标识。")
    target = channel or GLOBAL_BUCKET
    load_aliases()
    bucket = dict(_store.get(target, {}))
    for key in cleaned:
        bucket[key] = name
    _store[target] = bucket
    _commit()
    return name


def set_alias(file: Mapping[str, Any], alias: str, *, channel: str | None = None) -> str:
    return set_alias_for_keys(channel or channel_of(file), unique_identity_keys(file), alias)


def delete_alias_for_keys(channel: str, keys: Iterable[str]) -> bool:
    load_aliases()
    drop = {str(key).strip().lower() for key in keys if str(key).strip()}
    if not drop:
        return False
    removed = False
    for bucket_name in (channel or GLOBAL_BUCKET, GLOBAL_BUCKET):
        bucket = _store.get(bucket_name)
        if not bucket:
            continue
        for key in list(bucket):
            if key.lower() in drop:
                bucket.pop(key, None)
                removed = True
        if not bucket:
            _store.pop(bucket_name, None)
    if removed:
        _commit()
    return removed


def delete_alias(file: Mapping[str, Any], *, channel: str | None = None) -> bool:
    return delete_alias_for_keys(channel or channel_of(file), unique_identity_keys(file))


def _commit() -> None:
    global _signature
    global _checked_at
    if _memory_only:
        return
    from . import state
    from .config import atomic_write_json

    path = aliases_path()
    # 写回时保留用户手改的注释（_ 前缀）与 channel_keywords 保留键，避免被覆盖丢失。
    raw = _read_file(path)
    payload: dict[str, Any] = {}
    if isinstance(raw, Mapping):
        for key, value in raw.items():
            if str(key).startswith("_"):
                payload[str(key)] = value
    if README_KEY not in payload:
        payload[README_KEY] = default_aliases_dict()[README_KEY]
    payload.update({key: dict(bucket) for key, bucket in _store.items()})
    if _channel_keywords:
        payload[CHANNEL_KEYWORDS_KEY] = dict(_channel_keywords)
    try:
        atomic_write_json(path, payload)
    except Exception:
        pass
    _signature = _read_signature(path)
    _checked_at = time.monotonic()
    # 别名变化 → 展示名与额度缓存里的 name 都需失效。
    state.invalidate_downstream()


# --------------------------------------------------------------------------- #
# 展示
# --------------------------------------------------------------------------- #


def list_aliases() -> dict[str, dict[str, str]]:
    """返回嵌套的别名表（渠道 → 身份键 → 别名）。"""
    return load_aliases()


def _hint() -> str:
    return "还没有账号别名。设置：/quotanoa alias set <渠道> <查询词> <别名>"


def format_alias_list(
    files: Sequence[Mapping[str, Any]] | None = None,
    *,
    include_disabled: bool = False,
) -> str:
    store = load_aliases()
    if not store:
        return _hint()
    lines: list[str] = []
    hidden = 0
    for channel in sorted(store):
        bucket = store[channel]
        if not bucket:
            continue
        grouped: dict[str, list[str]] = {}
        for key, alias in bucket.items():
            grouped.setdefault(alias, []).append(key)
        for alias in sorted(grouped):
            keys = grouped[alias]
            if files is not None and not include_disabled:
                matched = _files_for_keys(files, keys)
                if matched and all(item.get("disabled") for item in matched):
                    hidden += 1
                    continue
            shown = ", ".join(_public_key(item) for item in keys)
            label = "全局" if channel == GLOBAL_BUCKET else channel
            lines.append(f"[{label}] {alias}  ←  {shown}")
    if not lines:
        if hidden:
            return "没有可显示的别名（已隐藏 disabled 账号）。查看：/quotanoa alias list --disabled"
        return _hint()
    return "\n".join(lines)


def _files_for_keys(files: Sequence[Mapping[str, Any]], keys: list[str]) -> list[Mapping[str, Any]]:
    needles = {item.lower() for item in keys}
    matched: list[Mapping[str, Any]] = []
    seen: set[str] = set()
    for file in files:
        markers = {item.lower() for item in identity_keys(file)}
        if not (needles & markers):
            continue
        stamp = str(file.get("auth_index") or file.get("name") or id(file))
        if stamp in seen:
            continue
        seen.add(stamp)
        matched.append(file)
    return matched


def _public_key(key: str) -> str:
    if "@" in key:
        local, _, domain = key.partition("@")
        prefix = local[:2] if local else "*"
        return f"{prefix}***@{domain}"
    if len(key) > 18:
        return key[:8] + "…"
    return key


# --------------------------------------------------------------------------- #
# 生命周期
# --------------------------------------------------------------------------- #


def reset_alias_cache() -> None:
    """让下一次读取重新检查磁盘（内存模式下无操作）。"""
    global _store, _channel_keywords, _loaded, _signature, _checked_at
    if _memory_only:
        return
    _store = {}
    _channel_keywords = {}
    _loaded = False
    _signature = None
    _checked_at = 0.0
