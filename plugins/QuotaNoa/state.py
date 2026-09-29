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
    DEFAULT_CLIENT_CONFIG_FILE,
    DEFAULT_CONFIG_FILE,
    ClientRegistry,
    ClientServerConfig,
    ConfigError,
    ConfigSnapshot,
    Config,
    read_client_config_file,
    read_config_file,
    snapshot_from_raw,
    write_client_config_file,
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

#: 远程客户端注册表（``data/quotanoa_client.json``）内存快照。
_client_path_override: Path | None = None
_client_registry: ClientRegistry | None = None
_client_signature: tuple[int, int] | None = None
_client_server_cfg: ClientServerConfig | None = None


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
# 远程客户端注册表 / 服务端监听设置
# --------------------------------------------------------------------------- #


def client_config_path() -> Path:
    """解析客户端注册表文件的绝对路径（``QUOTANOA_CLIENT_FILE``）。"""
    global _env_config
    if _client_path_override is not None:
        return _client_path_override
    raw = DEFAULT_CLIENT_CONFIG_FILE
    try:
        if _env_config is None:
            from nonebot import get_plugin_config

            _env_config = get_plugin_config(Config)
        env_config = _env_config
        if env_config is not None:
            raw = env_config.quotanoa_client_file or DEFAULT_CLIENT_CONFIG_FILE
    except Exception:
        raw = DEFAULT_CLIENT_CONFIG_FILE
    return Path(raw).expanduser().resolve()


def client_server_config() -> ClientServerConfig:
    """从 `.env`（插件 Config）构建客户端服务端监听设置，并做范围钳制。"""
    global _env_config, _client_server_cfg
    if _client_server_cfg is not None:
        return _client_server_cfg
    try:
        if _env_config is None:
            from nonebot import get_plugin_config

            _env_config = get_plugin_config(Config)
    except Exception:
        _env_config = None
    env_config = _env_config
    if env_config is None:
        cfg = ClientServerConfig()
    else:
        from .protocol import normalize_client_name, valid_client_name

        server_name = normalize_client_name(env_config.quotanoa_client_server_name or "")
        if not valid_client_name(server_name):
            server_name = config_module.DEFAULT_CLIENT_SERVER_NAME
        host = str(env_config.quotanoa_client_host or "").strip() or config_module.DEFAULT_CLIENT_HOST
        port = max(1, min(65535, int(env_config.quotanoa_client_port or config_module.DEFAULT_CLIENT_PORT)))
        timeout = max(1.0, float(env_config.quotanoa_client_request_timeout or config_module.DEFAULT_CLIENT_REQUEST_TIMEOUT))
        ws_max_size = max(1024, int(env_config.quotanoa_client_ws_max_size or config_module.DEFAULT_CLIENT_WS_MAX_SIZE))
        max_accounts = max(1, int(env_config.quotanoa_client_max_accounts or config_module.DEFAULT_CLIENT_MAX_ACCOUNTS))
        cfg = ClientServerConfig(
            enabled=bool(env_config.quotanoa_client_server_enabled),
            server_name=server_name,
            host=host,
            port=port,
            request_timeout=timeout,
            ws_max_size=ws_max_size,
            max_accounts=max_accounts,
        )
    _client_server_cfg = cfg
    return cfg


def _read_client_signature(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return (stat.st_mtime_ns, stat.st_size)


def _sync_client_hub() -> None:
    """把当前监听设置与注册表同步给 Hub（不触发注册表重载，避免递归）。"""
    if _client_registry is None:
        return
    try:
        from .clienthub import get_hub

        get_hub().configure(client_server_config(), _client_registry)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"同步远程客户端 Hub 配置失败：{exc}")


def get_client_registry() -> ClientRegistry:
    """返回客户端注册表内存快照；文件缺失=空注册表（**不生成文件**）。"""
    global _client_registry, _client_signature, _last_error
    with _lock:
        if _memory_only:
            if _client_registry is None:
                _client_registry = ClientRegistry()
            _sync_client_hub()
            return _client_registry
        path = client_config_path()
        signature = _read_client_signature(path)
        if _client_registry is not None and signature == _client_signature:
            return _client_registry
        try:
            registry = read_client_config_file(path)
        except ConfigError as exc:
            _last_error = str(exc)
            if _client_registry is None:
                _client_registry = ClientRegistry()
            return _client_registry
        _client_registry = registry
        _client_signature = signature
        _sync_client_hub()
        return registry


def reload_client_registry() -> ClientRegistry:
    """强制重新读取客户端注册表。"""
    global _client_signature
    with _lock:
        if _memory_only:
            return get_client_registry()
        _client_signature = None
        return get_client_registry()


def save_client_registry(registry: ClientRegistry) -> ClientRegistry:
    """原子写回客户端注册表（首次调用即生成文件），并刷新内存快照。"""
    global _client_registry, _client_signature
    with _lock:
        if _memory_only:
            _client_registry = registry
            _sync_client_hub()
            return registry
        path = client_config_path()
        write_client_config_file(path, registry)
        _client_registry = registry
        _client_signature = _read_client_signature(path)
        _sync_client_hub()
        return registry


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
