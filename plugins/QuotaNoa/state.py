"""配置快照与热重载。

- ``data/quotanoa_config.json`` 是唯一配置源（除 ``.env`` 里的 ``QUOTANOA_CONFIG_FILE``）。
- ``ensure_fresh()`` 在每个命令入口做廉价的 mtime/size 检查，变化才重新解析。
- 解析失败**保留上一份好快照**（fail-soft），错误可通过 ``last_error()`` 查询。
- 重载后按固定顺序失效下游缓存：别名 → 主题注册表 → 额度缓存。

本模块属根模块，只允许依赖 ``config``；对子包（cpa/render）的调用
一律用函数内延迟 import，以避免模块级循环依赖。
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any, Callable, Mapping

from . import config as config_module
from .config import (
    DEFAULT_CONFIG_FILE,
    ClientRegistry,
    ConfigError,
    ConfigSnapshot,
    Config,
    read_config_file,
    snapshot_from_raw,
)

try:
    from nonebot.log import logger
except Exception:  # pragma: no cover
    import logging

    logger = logging.getLogger("QuotaNoa.state")

_hooks: list[Callable[[ConfigSnapshot], None]] = []
_lock = threading.RLock()

_snapshot: ConfigSnapshot | None = None
_generation: int = 0
_signature: tuple[int, int] | None = None
_last_error: str = ""
_loaded_path: Path | None = None
_memory_only = False

_env_config: Config | None = None
_path_override: Path | None = None


def register_reload_hook(hook: Callable[[ConfigSnapshot], None]) -> None:
    """注册一个在配置重载后调用的同步回调。"""
    if hook not in _hooks:
        _hooks.append(hook)


def config_file_path() -> Path:
    """解析配置文件的绝对路径。"""
    global _env_config
    if _path_override is not None:
        return _path_override
    raw = DEFAULT_CONFIG_FILE
    try:
        if _env_config is None:
            from nonebot import get_plugin_config

            _env_config = get_plugin_config(Config)
        env_config = _env_config
        if env_config is not None:
            raw = env_config.quotanoa_config_file or DEFAULT_CONFIG_FILE
    except Exception:
        raw = DEFAULT_CONFIG_FILE
    return Path(raw).expanduser().resolve()


# --------------------------------------------------------------------------- #
# 远程客户端服务端（Server 模式）
# --------------------------------------------------------------------------- #


def client_registry(snapshot: ConfigSnapshot | None = None) -> ClientRegistry:
    """从配置快照派生客户端内存视图（持久化在主配置的 ``clients``）。"""
    data = snapshot if snapshot is not None else get_snapshot()
    return ClientRegistry(clients=data.clients)


def _sync_client_hub(snapshot: ConfigSnapshot, previous: ConfigSnapshot | None = None) -> None:
    """把服务端设置与客户端列表同步给 Hub。

    - 客户端列表 / 监听参数：直接 ``configure``（列表热生效）。
    - ``server.enabled`` 变化：在事件循环中调度 Hub 启停（热切换）。
    - 监听参数变化且 Hub 正在运行：由 ``Hub.reconcile`` 告警需重启。
    """
    try:
        from .clienthub import get_hub

        get_hub().configure(snapshot.server, ClientRegistry(clients=snapshot.clients))
        if previous is not None and previous.server.enabled != snapshot.server.enabled:
            _schedule_hub_reconcile()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"同步远程客户端 Hub 配置失败：{exc}")


def _schedule_hub_reconcile() -> None:
    """在运行中的事件循环里调度 Hub 启停；无循环（如导入期）时忽略。"""
    import asyncio

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    try:
        from .clienthub import get_hub

        loop.create_task(get_hub().reconcile())
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"调度远程客户端服务端切换失败：{exc}")


def _read_signature(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return (stat.st_mtime_ns, stat.st_size)


def _warn_legacy(path: Path) -> None:
    """首次生成配置时，提示旧的 env / 数据文件不再生效，避免“配置消失”困惑。

    用户明确要求“不做自动迁移”，因此这里只告警、不迁移。
    """
    legacy_env = False
    for name in ("CPA_BASE_URL", "CPA_MANAGEMENT_KEY", "CPA_ADMINS", "CPA_ALIAS_FILE"):
        if os.environ.get(name):
            legacy_env = True
            break
    legacy_files = []
    data_dir = path.parent
    for name in ("cpa_aliases.json", "quota_aliases.json", "quotabot_config.json", "cpa_render_settings.json"):
        if (data_dir / name).is_file():
            legacy_files.append(str(data_dir / name))
    if not legacy_env and not legacy_files:
        return
    logger.warning(
        "检测到旧版 QuotaNoa 配置："
        + ("环境变量 CPA_* " if legacy_env else "")
        + ("文件 " + ", ".join(legacy_files) if legacy_files else "")
        + "。这些不再生效；别名文件已更名为 data/quotanoa_aliases.json（未做自动迁移）。"
        "请改用 %s（/quotanoa config show 查看，/quotanoa config reload 重载）。",
        path,
    )


def get_snapshot() -> ConfigSnapshot:
    """返回当前快照；首次调用时惰性加载（缺失则生成默认文件）。"""
    with _lock:
        if _snapshot is None:
            _load(force=True, generate=True)
        return _snapshot  # type: ignore[return-value]


def generation() -> int:
    return _generation


def last_error() -> str:
    return _last_error


def snapshot_path() -> Path | None:
    return _loaded_path


def _load(*, force: bool, generate: bool) -> bool:
    """内部加载。返回是否发生了替换。调用方需持有 _lock。"""
    global _snapshot, _generation, _signature, _last_error, _loaded_path

    if _memory_only:
        if _snapshot is None:
            _snapshot = config_module.snapshot_from_raw(config_module.default_config_dict())
            _generation += 1
        return False

    path = config_file_path()
    signature = _read_signature(path)
    if not force and signature is not None and signature == _signature and _snapshot is not None:
        return False

    try:
        if signature is None:
            if not generate:
                raise ConfigError(f"配置文件不存在：{path}")
            generated = not path.is_file()
            raw = config_module.ensure_config_file(path)
            if generated:
                _warn_legacy(path)
        else:
            raw = read_config_file(path)
        snapshot = snapshot_from_raw(raw)
    except ConfigError as exc:
        _last_error = str(exc)
        if _snapshot is None:
            # 没有任何可用配置时，至少保证有默认值可用。
            _snapshot = config_module.snapshot_from_raw(config_module.default_config_dict())
            _generation += 1
        return False
    except Exception as exc:  # noqa: BLE001 - 任何解析异常都不能让消息处理崩溃
        _last_error = f"配置解析失败：{exc}"
        if _snapshot is None:
            _snapshot = config_module.snapshot_from_raw(config_module.default_config_dict())
            _generation += 1
        return False

    previous = _snapshot
    _snapshot = snapshot
    _generation += 1
    _signature = _read_signature(path)
    _last_error = ""
    _loaded_path = path
    _invalidate(snapshot, previous)
    return True


def _cpa_connection_slice(snapshot: ConfigSnapshot) -> tuple[tuple[str, str, str, float], ...]:
    """实例连接相关字段的指纹。实例增删或连接变化都需要重建 HTTP 客户端。"""
    return tuple(
        (instance.name, instance.base_url, instance.management_key, instance.timeout)
        for instance in snapshot.cpa.instances
    )


def _invalidate(snapshot: ConfigSnapshot, previous: ConfigSnapshot | None = None) -> None:
    """按固定顺序失效下游缓存。任何异常只记录，不阻断消息处理。"""
    try:
        from . import aliases

        aliases.reset_alias_cache()
    except Exception as exc:
        logger.warning(f"配置重载后刷新别名失败：{exc}")
    try:
        from .render.themes import get_theme_registry

        registry = get_theme_registry()
        registry.refresh()
        theme = (snapshot.render.theme or "").strip().lower()
        if theme and not registry.is_valid_theme(theme):
            allowed = ", ".join(registry.list_canonical_names())
            logger.warning(
                f"配置 render.theme={snapshot.render.theme!r} 不是有效主题，将回退到 default。可用主题：{allowed}"
            )
    except Exception as exc:
        logger.warning(f"配置重载后刷新主题失败：{exc}")
    try:
        from .cpa.quota import clear_quota_cache

        clear_quota_cache()
    except Exception as exc:
        logger.warning(f"配置重载后清除额度缓存失败：{exc}")
    try:
        from .cache import clear_boards

        clear_boards()
    except Exception as exc:
        logger.warning(f"配置重载后清除渠道缓存失败：{exc}")
    # 仅当连接相关配置（base_url/management_key/timeout）变化时才重建 HTTP 客户端，
    # 避免主题等无关修改无谓地重建连接池。
    connection_changed = previous is None or _cpa_connection_slice(previous) != _cpa_connection_slice(snapshot)
    if connection_changed:
        try:
            from .cpa.client import reset_client

            reset_client()
        except Exception as exc:
            logger.warning(f"配置重载后重建 HTTP 客户端失败：{exc}")
    # 远程客户端服务端：同步设置/列表，并在 enabled 变化时热启停。
    _sync_client_hub(snapshot, previous)
    for hook in list(_hooks):
        try:
            hook(snapshot)
        except Exception as exc:
            logger.warning(f"配置重载钩子执行失败：{exc}")


def ensure_fresh() -> bool:
    """廉价检查配置文件是否变化，变化则重载。返回是否重载。"""
    with _lock:
        if _memory_only:
            return False
        return _load(force=False, generate=True)


def reload_config() -> ConfigSnapshot:
    """强制从磁盘重载配置。"""
    with _lock:
        _load(force=True, generate=True)
        return _snapshot  # type: ignore[return-value]


def update_config(patch: Mapping[str, Any]) -> ConfigSnapshot:
    """把 patch 深合并进配置文件并重载（用于 /quotanoa config 与主题设置）。

    磁盘模式下以**磁盘上的当前内容**为合并底，避免覆盖操作者刚手改的字段；
    内存模式只合并内存快照，绝不读写磁盘。
    """
    with _lock:
        if _memory_only:
            global _snapshot, _generation
            merged = config_module.deep_merge(dict(get_snapshot().raw), patch)
            _snapshot = snapshot_from_raw(merged)
            _generation += 1
            _invalidate(_snapshot)
            return _snapshot
        path = config_file_path()
        if path.is_file():
            base_raw = config_module.read_config_file(path)
        else:
            base_raw = dict(get_snapshot().raw)
        merged = config_module.deep_merge(base_raw, patch)
        current_sig = _read_signature(path)
        if _signature is not None and current_sig is not None and current_sig != _signature:
            raise ConfigError("配置文件已被外部修改，本次写入已放弃；下一条消息会自动重载最新配置，请重试。")
        config_module.atomic_write_json(path, merged)
        _load(force=True, generate=False)
        return _snapshot  # type: ignore[return-value]


def repair_config() -> config_module.RepairResult:
    """补齐磁盘配置缺失项（先备份旧文件）。内存模式不支持，抛 ``ConfigError``。

    完成后强制重载，使补入的默认值立即生效。
    """
    with _lock:
        if _memory_only:
            raise ConfigError("当前为内存配置模式，无法修补磁盘配置文件。")
        path = config_file_path()
        result = config_module.repair_config_file(path)
        if result.changed:
            _load(force=True, generate=False)
        return result


def invalidate_downstream() -> None:
    """仅供别名等外部改动后手动触发下游失效。"""
    with _lock:
        if _snapshot is not None:
            _invalidate(_snapshot)
