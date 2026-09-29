"""/quotanoa 根命令：额度查询 + 别名 / 主题 / 卡片 / 配置 / 火山实例 子命令。

根前缀固定为 `/`（用户要求“quota 必须使用指令头”），裸 `quota` 不匹配。
查询主体（平台 / 实例 / 账号 / --fresh 等）由 ``query.parse_quota_command``
从 plaintext 解析，因此根上不再声明 --fresh/--text/--instance 等 Option，
避免 `$main` 因 components 非空而不触发。

多实例：默认查询按入口的主渠道优先展示（/quotanoa 本地渠道、/cpa quota 全部 CPA 平台），
并按实例名加前缀区分；``--instance <名>`` 或位置参数里的实例名可限定到单个实例。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Mapping, Sequence

from arclet.alconna import Alconna, Args, CommandMeta, MultiVar, Option, Subcommand, store_true
from nonebot.adapters import Bot, Event
from nonebot_plugin_alconna import Arparma, CustomNode, Image, Query, Text, UniMessage, on_alconna

from .. import state
from ..config import CpaConfig, ConfigError, normalize_name, valid_name, VolcengineAccount
from ..cpa.client import CPAError, get_client
from ..cpa.format import format_ambiguous, format_quota_list, is_cooling, match_auth
from ..cpa.quota import (
    PLATFORM_TITLES,
    QuotaBoard,
    clear_quota_cache,
    collect_quotas,
    format_quota_board,
    peek_quota_cache,
    platform_of,
)
from ..help import parse_help, quota_help_text
from ..model import LOCAL_CHANNELS, is_all_channels, normalize_channel
from ..query import QuotaSelection, TEXT_FLAGS, parse_quota_command, strip_quota_head, tokenize
from ..render.html import RenderError, render_board_images
from ..volcengine.provider import collect_board as collect_volcengine_board
from ..wb.provider import collect_board as collect_workbuddy_board
from ..qoder.provider import collect_board as collect_qoder_board

from .common import CPA_ADMIN, _require_one_across, _text, _without, send_help

try:
    from nonebot.log import logger
except Exception:  # pragma: no cover
    import logging

    logger = logging.getLogger("QuotaNoa.commands.quota")

#: 合并转发目标适配器名（NoneBot OneBot V11 适配器 ``get_name()`` 返回值）。
ONEBOT11_ADAPTER = "OneBot V11"

#: bot 昵称缓存：self_id → nickname（节点署名用，避免每条消息重复请求）。
_nickname_cache: dict[str, str] = {}

#: /quotanoa all 与默认查询使用的本地渠道顺序。
LOCAL_CHANNEL_LABELS = {"volcengine": "火山", "workbuddy": "WorkBuddy", "qoder": "Qoder"}

#: 本地渠道集合（与 ``model.LOCAL_CHANNELS`` 一致）。
_LOCAL_CHANNEL_SET = frozenset(LOCAL_CHANNELS)

#: 触发帮助的查询词（裸命令不再显示帮助）。
HELP_TOKENS = {"help", "--help", "-h"}


def _is_help_request(parts: Sequence[str]) -> bool:
    """请求是否查看帮助：首词是 help 令牌，其余只能是开关（如 --text）。"""
    return bool(parts) and parts[0].lower() in HELP_TOKENS and all(p.startswith("-") for p in parts[1:])


def _help_wants_text(parts: Sequence[str]) -> bool:
    return any(p.lower() in TEXT_FLAGS for p in parts)


# --------------------------------------------------------------------------- #
# 无参默认查询计划
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _QueryUnit:
    """默认查询计划中的一个查询单元。

    - ``local``：查询单个本地渠道（``channel`` 为本地渠道名）。
    - ``cpa_all``：查询全部 CPA 实例的全部平台。
    - ``cpa_platform``：查询全部 CPA 实例中 ``channel`` 对应平台的部分。
    """

    kind: str
    channel: str = ""


def _all_channels_plan() -> list[_QueryUnit]:
    """全部渠道：本地渠道在前，随后全部 CPA 平台（/quotanoa all 与 /cpa quota all 共用）。"""
    return [*(_QueryUnit("local", ch) for ch in LOCAL_CHANNELS), _QueryUnit("cpa_all")]


def _resolve_query_plan(configured, entry: str) -> list[_QueryUnit]:
    """按入口解析无参默认查询计划（有序）。

    主渠道在前，配置的额外渠道追加在后；已被主渠道覆盖的额外渠道自动去重。

    额外渠道里写入 ``all`` 等价于该入口的 ``all`` 子命令——输出全部渠道：

    - ``/quotanoa``：本地渠道（火山 / WorkBuddy / Qoder）在前，
      再叠加 ``quotanoa_additional_channel`` 里的额外渠道。
    - ``/cpa quota``：全部 CPA 平台在前，
      再叠加 ``cpa_additional_channel`` 里的额外本地渠道。
    """
    if any(is_all_channels(raw) for raw in configured):
        return _all_channels_plan()
    if entry == "cpa":
        units: list[_QueryUnit] = [_QueryUnit("cpa_all")]
        for raw in configured:
            channel = normalize_channel(raw)
            # 全部 CPA 平台已覆盖 CPA 渠道，只需追加本地渠道。
            if channel and channel in _LOCAL_CHANNEL_SET:
                units.append(_QueryUnit("local", channel))
        return units
    units = [_QueryUnit("local", ch) for ch in LOCAL_CHANNELS]
    seen_cpa: set[str] = set()
    for raw in configured:
        channel = normalize_channel(raw)
        # 本地渠道已在默认集中，只有 CPA 渠道需要额外查询。
        if not channel or channel in _LOCAL_CHANNEL_SET or channel in seen_cpa:
            continue
        seen_cpa.add(channel)
        units.append(_QueryUnit("cpa_platform", channel))
    return units


# --------------------------------------------------------------------------- #
# 子命令 Alconna 结构（别名/主题/卡片/配置在各自模块里挂 handler）
# --------------------------------------------------------------------------- #

quota = on_alconna(
    Alconna(
        ["/"],
        "quotanoa",
        Subcommand("help", help_text="查看帮助"),
        Subcommand("cooling", help_text="仅看冷却中的凭证"),
        Subcommand("reset", Args["query", str], help_text="清除配额/冷却并恢复路由"),
        Subcommand(
            "alias",
            Subcommand(
                "list",
                Option("--disabled", action=store_true, dest="disabled", help_text="包含已禁用账号"),
                help_text="列出账号别名",
            ),
            Subcommand(
                "set",
                Args["a", str]["b", str]["c?", str],
                help_text="设置别名：/quotanoa alias set <渠道> <查询词> <别名>",
            ),
            Subcommand("del|rm|delete", Args["query", str], dest="delete", help_text="删除别名"),
            help_text="分渠道账号别名",
        ),
        Subcommand(
            "theme",
            Subcommand("set", Args["name", str], help_text="设置额度图主题：/quotanoa theme set <主题>"),
            help_text="查看或设置额度图主题",
        ),
        Subcommand(
            "card",
            Subcommand("row", Args["count", str], help_text="设置每行卡片数：/quotanoa card row 1..6"),
            Subcommand("max", Args["count", str], help_text="设置每渠道最多账号卡片数：/quotanoa card max <数量>"),
            help_text="查看或设置卡片布局排版",
        ),
        Subcommand(
            "config",
            Subcommand("show", help_text="查看当前生效配置（密钥脱敏）"),
            Subcommand("reload", help_text="强制重载配置"),
            Subcommand("fix", help_text="修补配置文件：补齐缺失项并备份旧文件"),
            help_text="配置查看与重载",
        ),
        Subcommand(
            "volc",
            Subcommand("list", help_text="列出火山方舟账号"),
            Subcommand(
                "add",
                Args["name", str]["ak", str]["sk", str]["region?", str],
                help_text="新增火山方舟账号：/quotanoa volc add <名称> <AK> <SK> [region]",
            ),
            Subcommand(
                "remove|rm|delete",
                Args["name", str],
                Option("--yes|-y", action=store_true, dest="yes", help_text="确认删除"),
                dest="remove",
                help_text="删除火山方舟账号",
            ),
            help_text="火山方舟账号管理",
        ),
        Subcommand(
            "wb|workbuddy",
            Subcommand("list", help_text="列出 WorkBuddy 网关"),
            Subcommand(
                "add",
                Args["name", str]["base_url", str],
                Option("--user", Args["user", str], dest="user", help_text="控制台账号"),
                Option("--pass", Args["password", str], dest="password", help_text="控制台密码"),
                Option("--key", Args["key", str], dest="key", help_text="网关 api_key（跳过登录）"),
                Option("--timeout", Args["timeout", str], dest="timeout", help_text="请求超时秒"),
                help_text="新增 WorkBuddy 网关：/quotanoa wb add <名称> <base_url> --user U --pass P",
            ),
            Subcommand(
                "login",
                Args["name", str],
                help_text="校验账号密码并刷新会话：/quotanoa wb login <名称>",
            ),
            Subcommand(
                "remove|rm|delete",
                Args["name", str],
                Option("--yes|-y", action=store_true, dest="yes", help_text="确认删除"),
                dest="remove",
                help_text="删除 WorkBuddy 网关",
            ),
            help_text="WorkBuddy 网关额度查询与管理",
        ),
        Subcommand(
            "qoder|qd",
            Subcommand("list", help_text="列出 Qoder 代理"),
            Subcommand(
                "add",
                Args["name", str]["base_url", str],
                Option("--key", Args["key", str], dest="key", help_text="代理 API Key（Bearer）"),
                Option("--timeout", Args["timeout", str], dest="timeout", help_text="请求超时秒"),
                help_text="新增 Qoder 代理：/quotanoa qoder add <名称> <base_url> --key <API_KEY>",
            ),
            Subcommand(
                "remove|rm|delete",
                Args["name", str],
                Option("--yes|-y", action=store_true, dest="yes", help_text="确认删除"),
                dest="remove",
                help_text="删除 Qoder 代理",
            ),
            help_text="Qoder2OAPI 代理额度查询与管理",
        ),
        Args["a?", str]["b?", str]["tail", MultiVar(str, "*")],
        meta=CommandMeta(
            description="额度查询（仅管理员）",
            usage="发送 /quotanoa 查看帮助；/quotanoa 火山 查火山方舟",
            example="/quotanoa\n/quotanoa 火山\n/quotanoa claude\n/quotanoa claude Home\n/quotanoa --fresh\n/quotanoa cooling\n/quotanoa reset user@example.com\n/quotanoa alias set antigravity user@example.com AG-1\n/quotanoa volc add 火山主号 AK SK\n/quotanoa config show",
        ),
    ),
    permission=CPA_ADMIN,
    auto_send_output=True,
    skip_for_unmatch=False,
    use_cmd_start=True,
    block=True,
)


@quota.assign("$main")
async def quota_main(event: Event) -> None:
    await quota_entry(event, entry="quota")


@quota.assign("help")
async def quota_help(event: Event) -> None:
    parts = strip_quota_head(tokenize(event.get_plaintext()))
    await send_help(parse_help(quota_help_text()), text=_help_wants_text(parts))


async def quota_entry(event: Event, *, entry: str = "quota") -> None:
    """共享入口：/quotanoa 与 /cpa quota 复用。

    ``entry`` 区分入口（"quota" / "cpa"），决定无参数时的默认渠道集：

    - /quotanoa：本地渠道（火山 / WorkBuddy / Qoder）优先，
      再叠加 ``quotanoa_additional_channel`` 的额外渠道；
    - /cpa quota：全部 CPA 平台优先，
      再叠加 ``cpa_additional_channel`` 的额外渠道。

    查询：/quotanoa help（或 --help / -h）显示帮助。
    """
    parts = strip_quota_head(tokenize(event.get_plaintext()))
    if _is_help_request(parts):
        await send_help(parse_help(quota_help_text()), text=_help_wants_text(parts))
        return
    await quota_view(event, entry=entry)


# --------------------------------------------------------------------------- #
# 子命令：cooling / reset
# --------------------------------------------------------------------------- #


@quota.assign("cooling")
async def quota_cooling() -> None:
    names = state.get_snapshot().cpa.names()
    if not names:
        await UniMessage("没有配置 CPA 实例。新增：/cpa instance add <名称> <base_url>").finish()
        return
    lines: list[str] = []
    for name in names:
        try:
            files = await get_client(name).list_auth_files()
        except CPAError as exc:
            lines.append(f"[{name}] {exc}")
            continue
        cooling = [item for item in files if is_cooling(item)]
        if cooling:
            lines.append(format_quota_list(cooling, instance=name))
    await UniMessage("\n".join(lines) if lines else "当前没有冷却中的凭证。").finish()


@quota.assign("reset")
async def quota_reset(query: Query[str] = Query("reset.query")) -> None:
    instance, file = await _require_one_across(_text(query))
    auth_index = str(file.get("auth_index") or "")
    if not auth_index:
        await UniMessage("该凭证没有 auth_index，无法 reset-quota。").finish()
        return
    try:
        result = await get_client(instance).reset_quota(auth_index)
    except CPAError as exc:
        await UniMessage(str(exc)).finish()
        return
    await UniMessage(f"[{instance}] " + _format_reset_result(result, file)).finish()


def _format_reset_result(data: Any, file: dict[str, Any]) -> str:
    from ..cpa.format import display_name

    models = []
    if isinstance(data, dict):
        raw = data.get("models")
        if isinstance(raw, list):
            models = [str(item) for item in raw]
    extra = f"\n已恢复模型：{', '.join(models)}" if models else ""
    return f"已清除 {display_name(file, public=True)} 的配额/冷却。{extra}"


# --------------------------------------------------------------------------- #
# 火山方舟账号管理
# --------------------------------------------------------------------------- #


def _volcengine_raw() -> list[dict[str, Any]]:
    raw = state.get_snapshot().raw
    volc = raw.get("volcengine") if isinstance(raw, Mapping) else None
    accounts = volc.get("accounts") if isinstance(volc, Mapping) else None
    if not isinstance(accounts, list):
        return []
    return [dict(item) for item in accounts if isinstance(item, Mapping)]


def _write_volcengine(accounts: list[dict[str, Any]]) -> None:
    state.update_config({"volcengine": {"accounts": accounts}})


@quota.assign("volc.list")
async def volc_list() -> None:
    from ..cpa.format import mask_secret

    accounts = state.get_snapshot().volcengine.accounts
    if not accounts:
        await UniMessage("还没有配置火山方舟账号。新增：/quotanoa volc add <名称> <AK> <SK> [region]").finish()
        return
    lines = ["【火山方舟账号】"]
    for account in accounts:
        lines.append(f"  {account.name}  AK={mask_secret(account.access_key_id)}  region={account.region}")
    await UniMessage("\n".join(lines)).finish()


@quota.assign("volc.add")
async def volc_add(
    name: Query[str] = Query("volc.add.name"),
    ak: Query[str] = Query("volc.add.ak"),
    sk: Query[str] = Query("volc.add.sk"),
    region: Query[str] = Query("volc.add.region"),
) -> None:
    account_name = normalize_name(_text(name))
    if not valid_name(account_name):
        await UniMessage(f"账号名称非法：{_text(name)}（1–32 字符，不能含空白或 / \\）").finish()
        return
    ak_text = _text(ak)
    sk_text = _text(sk)
    if not ak_text or not sk_text:
        await UniMessage("AK / SK 不能为空。").finish()
        return
    accounts = _volcengine_raw()
    if any(normalize_name(str(item.get("name") or "")) == account_name for item in accounts):
        await UniMessage(f"火山账号「{account_name}」已存在。查看：/quotanoa volc list").finish()
        return
    entry: dict[str, Any] = {
        "name": account_name,
        "access_key_id": ak_text,
        "secret_access_key": sk_text,
    }
    if region.available and _text(region):
        entry["region"] = _text(region)
    accounts.append(entry)
    try:
        _write_volcengine(accounts)
    except ConfigError as exc:
        await UniMessage(f"写入配置失败：{exc}").finish()
        return
    await UniMessage(f"已新增火山账号「{account_name}」。查看：/quotanoa volc list").finish()


@quota.assign("volc.remove")
async def volc_remove(
    arp: Arparma,
    name: Query[str] = Query("volc.remove.name"),
) -> None:
    account_name = normalize_name(_text(name))
    accounts = _volcengine_raw()
    remaining = [item for item in accounts if normalize_name(str(item.get("name") or "")) != account_name]
    if len(remaining) == len(accounts):
        await UniMessage(f"没有名为「{account_name}」的火山账号。查看：/quotanoa volc list").finish()
        return
    if not arp.find("volc.remove.yes"):
        await UniMessage(            f"即将删除火山账号「{account_name}」。确认请发送：\n/quotanoa volc remove {account_name} --yes").finish()
        return
    try:
        _write_volcengine(remaining)
    except ConfigError as exc:
        await UniMessage(f"写入配置失败：{exc}").finish()
        return
    await UniMessage(f"已删除火山账号「{account_name}」。").finish()


# --------------------------------------------------------------------------- #
# 查询主体
# --------------------------------------------------------------------------- #


async def quota_view(event: Event, *, entry: str = "quota") -> None:
    snapshot = state.get_snapshot()
    selection = parse_quota_command(
        event.get_plaintext(),
        known_instances=set(snapshot.cpa.names()),
        extra_channels=_custom_channel_keywords(),
    )
    if selection.error:
        await UniMessage(selection.error).finish()
    # 火山方舟：本地渠道，凭据来自 volcengine.accounts。
    if selection.platform == "volcengine":
        await _send_volcengine_results(snapshot.cpa, selection)
        return
    # WorkBuddy：本地渠道，凭据来自 workbuddy.servers。
    if selection.platform == "workbuddy":
        await _send_workbuddy_results(snapshot.cpa, selection)
        return
    # Qoder：本地渠道，凭据来自 qoder.servers。
    if selection.platform == "qoder":
        await _send_qoder_results(snapshot.cpa, selection)
        return
    if selection.all_channels and not (selection.platform or selection.instance or selection.account):
        await _send_plan(snapshot, _all_channels_plan(), selection=selection)
        return
    if not (selection.platform or selection.instance or selection.account):
        if entry == "cpa":
            plan = _resolve_query_plan(snapshot.cpa_additional_channel, entry)
        else:
            plan = _resolve_query_plan(snapshot.quotanoa_additional_channel, entry)
        await _send_plan(snapshot, plan, selection=selection)
        return
    targets = _quota_targets(selection)
    if targets is None:
        return
    if not targets:
        await UniMessage("没有可查询的 CPA 实例。新增：/cpa instance add <名称> <base_url>").finish()
        return
    await UniMessage("正在按平台查询上游额度，可能需要几秒…").send()
    results: list[tuple[str, QuotaBoard | str]] = []
    for name in targets:
        try:
            board = await _instance_quota_board(name, selection)
            results.append((name, board))
        except CPAError as exc:
            results.append((name, str(exc)))
    await _send_quota_results(snapshot.cpa, results, want_text=selection.text, multi=len(targets) > 1)


async def _send_volcengine_results(cpa: CpaConfig, selection: QuotaSelection) -> None:
    accounts = list(state.get_snapshot().volcengine.accounts)
    if selection.account:
        accounts = _filter_volcengine_accounts(accounts, selection.account)
        if not accounts:
            await UniMessage(f"没有找到火山账号：{selection.account}").finish()
            return
    if not accounts:
        await UniMessage(
            "未配置火山方舟账号。用 /quotanoa volc add <名称> <AK> <SK> [region] 添加，"
            "或编辑 data/quotanoa_config.json 的 volcengine.accounts。"
        ).finish()
        return
    await UniMessage("正在查询火山方舟 Coding/Agent Plan 额度…").send()
    try:
        board = await collect_volcengine_board(accounts, force=selection.fresh)
    except Exception as exc:  # noqa: BLE001 - 兜底，避免单渠道异常打断消息处理
        await UniMessage(f"火山额度查询失败：{exc}").finish()
        return
    await _send_quota_results(cpa, [("火山", board)], want_text=selection.text, multi=False)


def _filter_volcengine_accounts(accounts: Sequence[VolcengineAccount], query: str) -> list[VolcengineAccount]:
    needle = query.strip().lower()
    if not needle:
        return list(accounts)
    from ..aliases import resolve_alias_for_keys

    matched: list[VolcengineAccount] = []
    for account in accounts:
        alias = resolve_alias_for_keys("volcengine", [account.name]) or account.name
        fields = [account.name.lower(), alias.lower()]
        if any(needle in field or field.startswith(needle) for field in fields):
            matched.append(account)
    return matched


# --------------------------------------------------------------------------- #
# WorkBuddy 网关额度查询（本地渠道，多网关聚合）
# --------------------------------------------------------------------------- #


async def _send_workbuddy_results(cpa: CpaConfig, selection: QuotaSelection) -> None:
    servers = list(state.get_snapshot().workbuddy.servers)
    if not servers:
        await UniMessage(
            "未配置 WorkBuddy 网关。用 /quotanoa wb add <名称> <base_url> --user U --pass P 添加，"
            "或编辑 data/quotanoa_config.json 的 workbuddy.servers。"
        ).finish()
        return
    await UniMessage("正在查询 WorkBuddy 额度…").send()
    try:
        board = await collect_workbuddy_board(servers, force=selection.fresh)
    except Exception as exc:  # noqa: BLE001 - 兜底，避免单渠道异常打断消息处理
        await UniMessage(f"WorkBuddy 额度查询失败：{exc}").finish()
        return
    await _send_quota_results(cpa, [("WorkBuddy", board)], want_text=selection.text, multi=False)


async def _send_qoder_results(cpa: CpaConfig, selection: QuotaSelection) -> None:
    servers = list(state.get_snapshot().qoder.servers)
    if not servers:
        await UniMessage(
            "未配置 Qoder 代理。新增：/quotanoa qoder add <名称> <base_url> --key <API_KEY>"
        ).finish()
        return
    await UniMessage("正在查询 Qoder 额度…").send()
    try:
        board = await collect_qoder_board(servers, force=selection.fresh)
    except Exception as exc:  # noqa: BLE001 - 兜底，避免单渠道异常打断消息处理
        await UniMessage(f"Qoder 额度查询失败：{exc}").finish()
        return
    await _send_quota_results(cpa, [("Qoder", board)], want_text=selection.text, multi=False)


async def _local_channel_board(snapshot, channel: str, *, force: bool) -> QuotaBoard | None:
    """收集某个本地渠道的额度板；未配置返回 None。"""
    if channel == "volcengine":
        accounts = list(snapshot.volcengine.accounts)
        if not accounts:
            return None
        return await collect_volcengine_board(accounts, force=force)
    if channel == "workbuddy":
        servers = list(snapshot.workbuddy.servers)
        if not servers:
            return None
        return await collect_workbuddy_board(servers, force=force)
    if channel == "qoder":
        servers = list(snapshot.qoder.servers)
        if not servers:
            return None
        return await collect_qoder_board(servers, force=force)
    return None


def _no_channel_configured_text() -> str:
    return (
        "没有可查询的渠道。\n"
        "本地渠道：/quotanoa volc add <名称> <AK> <SK>、/quotanoa wb add …、/quotanoa qoder add …\n"
        "CPA 实例：/cpa instance add <名称> <base_url>\n"
        "查看全部渠道：/quotanoa all"
    )


async def _send_plan(
    snapshot,
    plan: Sequence[_QueryUnit],
    *,
    selection: QuotaSelection,
) -> None:
    """按查询计划发送额度：本地渠道与 CPA 平台按计划顺序排列。"""
    cpa = snapshot.cpa
    instances = list(cpa.names())
    has_local_unit = any(unit.kind == "local" for unit in plan)
    if not instances and not has_local_unit:
        await UniMessage(_no_channel_configured_text()).finish()
        return
    results: list[tuple[str, QuotaBoard | str]] = []
    await UniMessage("正在查询额度，可能需要几秒…").send()
    for unit in plan:
        if unit.kind == "local":
            label = LOCAL_CHANNEL_LABELS.get(unit.channel, unit.channel)
            try:
                board = await _local_channel_board(snapshot, unit.channel, force=selection.fresh)
            except Exception as exc:  # noqa: BLE001 - 单渠道异常不阻断其它渠道
                results.append((label, f"额度查询失败：{exc}"))
                continue
            if board is None or not board.platforms:
                continue
            results.append((label, board))
            continue
        # CPA：全部平台或限定单一平台；无对应凭证的实例静默跳过。
        platform = unit.channel if unit.kind == "cpa_platform" else ""
        for name in instances:
            try:
                board = await _plan_instance_board(name, platform, selection)
            except CPAError as exc:
                results.append((name, str(exc)))
                continue
            if board is None:
                continue
            results.append((name, board))
    if not results:
        await UniMessage(_no_channel_configured_text()).finish()
        return
    await _send_quota_results(cpa, results, want_text=selection.text, multi=len(results) > 1)


async def _plan_instance_board(
    instance: str,
    platform: str,
    selection: QuotaSelection,
) -> QuotaBoard | None:
    """默认计划用：查询某实例（可限定平台）；无匹配凭证时返回 ``None``。"""
    files = await get_client(instance).list_auth_files()
    target = [item for item in files if platform_of(item) == platform] if platform else files
    if platform and not target:
        return None
    force = selection.fresh
    platform_filter = platform or None
    board = (
        None
        if force
        else peek_quota_cache(
            target, instance=instance, platform=platform_filter, skip_disabled=True
        )
    )
    if board is None:
        board = await collect_quotas(
            target,
            instance=instance,
            platform=platform_filter,
            force=force,
            skip_disabled=True,
        )
    return board


def _custom_channel_keywords() -> dict[str, str]:
    """用户自定义渠道关键字（quotanoa_aliases.json 的 channel_keywords）。"""
    try:
        from ..aliases import custom_channel_keywords

        return custom_channel_keywords()
    except Exception:
        return {}


# --------------------------------------------------------------------------- #
# /quotanoa wb 子命令（查询 + 网关管理）
# --------------------------------------------------------------------------- #


@quota.assign(
    "workbuddy",
    additional=_without("workbuddy.list", "workbuddy.add", "workbuddy.remove", "workbuddy.login"),
)
async def quota_workbuddy(event: Event) -> None:
    """`/quotanoa wb`：查询 WorkBuddy 全部网关额度（渠道查询，不查单个账号）。

    子命令只在无 `list`/`add`/`remove` 时触发；`--text` / `--fresh` 由
    ``parse_quota_command`` 从 plaintext 统一解析（不在子命令上声明 Option，
    否则会劫持根级 ``/quotanoa --text``）。
    """
    selection = parse_quota_command(
        event.get_plaintext(),
        known_instances=set(state.get_snapshot().cpa.names()),
        extra_channels=_custom_channel_keywords(),
    )
    await _send_workbuddy_results(state.get_snapshot().cpa, selection)


@quota.assign(
    "qoder",
    additional=_without("qoder.list", "qoder.add", "qoder.remove"),
)
async def quota_qoder(event: Event) -> None:
    """`/quotanoa qoder`：查询 Qoder 全部代理号池额度（渠道查询，不查单个账号）。"""
    selection = parse_quota_command(
        event.get_plaintext(),
        known_instances=set(state.get_snapshot().cpa.names()),
        extra_channels=_custom_channel_keywords(),
    )
    await _send_qoder_results(state.get_snapshot().cpa, selection)


def _quota_targets(selection: QuotaSelection) -> list[str] | None:
    """返回要查询的实例名列表；``None`` 表示已发送错误消息、调用方应直接返回。"""
    if selection.instance:
        if selection.instance not in set(state.get_snapshot().cpa.names()):
            # 已在解析层校验过格式，这里只剩“不存在”一种情况。
            return [selection.instance]
        return [selection.instance]
    return list(state.get_snapshot().cpa.names())


async def _instance_quota_board(instance: str, selection: QuotaSelection) -> QuotaBoard:
    files = await get_client(instance).list_auth_files()
    platform = selection.platform
    target = files
    single = False
    if selection.account:
        matched = match_auth(files, selection.account)
        if not matched:
            raise CPAError(f"[{instance}] 没有找到凭证：{selection.account}")
        if len(matched) > 1:
            raise CPAError(format_ambiguous(selection.account, matched, instance=instance))
        target = matched
        single = True
    elif platform:
        target = [item for item in files if platform_of(item) == platform]
        if not target:
            raise CPAError(f"[{instance}] 没有 {platform} 平台的凭证。")
    force = selection.fresh
    skip_disabled = not single
    board = None if force else peek_quota_cache(target, instance=instance, platform=platform, skip_disabled=skip_disabled)
    if board is None:
        board = await collect_quotas(
            target, instance=instance, platform=platform, force=force, skip_disabled=skip_disabled
        )
    return board


def _result_channels(item: QuotaBoard | str, name: str) -> list[str]:
    """该结果条目涉及的 canonical 渠道列表。

    board 取其平台名（本地渠道为单平台板、CPA 全平台板含多平台）；
    字符串（错误/回退文字）按标签归一（如「火山」→ ``volcengine``）。
    """
    if isinstance(item, QuotaBoard):
        return [section.platform for section in item.platforms]
    canonical = normalize_channel(name)
    return [canonical] if canonical else []


def _apply_pin_order(
    results: Sequence[tuple[str, QuotaBoard | str]],
    pin_order: Sequence[str],
) -> list[tuple[str, QuotaBoard | str]]:
    """按 ``pin-channel`` 把命中的渠道置顶（左→右 = 上→下），其余保持原相对顺序。

    两级稳定排序：先重排每个板内部的平台，再重排结果条目。含被 pin 平台的
    CPA 板整块上浮（如 ``/cpa quota all`` 下的 xai 排在本地渠道之前），未命中
    当前查询的 pin 渠道自动忽略。

    板被 :func:`dataclasses.replace` 浅拷贝后替换平台列表，**不会原地修改**
    ``platforms``（额度缓存里是同一个板对象）。
    """
    order = {channel: rank for rank, channel in enumerate(pin_order)}
    if not order:
        return list(results)
    fallback = len(order)

    def _board_rank(name: str, item: QuotaBoard | str) -> int:
        return min((order.get(c, fallback) for c in _result_channels(item, name)), default=fallback)

    reordered: list[tuple[str, QuotaBoard | str]] = []
    for name, item in results:
        if isinstance(item, QuotaBoard) and item.platforms:
            sections = sorted(item.platforms, key=lambda s: order.get(s.platform, fallback))
            if sections != item.platforms:
                item = replace(item, platforms=sections)
        reordered.append((name, item))
    return sorted(reordered, key=lambda entry: _board_rank(entry[0], entry[1]))


def _current_onebot_bot() -> Bot | None:
    """当前事件上下文的 bot 是否为 OneBot V11；否则 ``None``（非 matcher 上下文同样 ``None``）。"""
    try:
        from nonebot.internal.matcher import current_bot

        bot = current_bot.get()
    except Exception:
        return None
    try:
        if bot.adapter.get_name() == ONEBOT11_ADAPTER:
            return bot
    except Exception:
        return None
    return None


async def _bot_nickname(bot: Bot) -> str:
    """合并转发节点的署名：OneBot 真实昵称（失败回退 bot 号，再回退 QuotaNoa）。"""
    cached = _nickname_cache.get(bot.self_id)
    if cached:
        return cached
    nickname = ""
    try:
        info = await bot.get_login_info()  # type: ignore[attr-defined]
        if isinstance(info, Mapping):
            nickname = str(info.get("nickname") or "").strip()
    except Exception:
        nickname = ""
    if nickname:
        _nickname_cache[bot.self_id] = nickname
        return nickname
    return bot.self_id or "QuotaNoa"


def _build_forward_nodes(
    outgoing: Sequence[tuple[str, bytes | None]],
    *,
    uid: str,
    nickname: str,
) -> list[CustomNode]:
    """把 ``(caption, png)`` 列表转为合并转发节点：文字与图片都在同一节点内容里。"""
    nodes: list[CustomNode] = []
    for caption, png in outgoing:
        if png is None:
            content = [Text(caption)]
        else:
            content = [Image(raw=png, mimetype="image/png")]
        nodes.append(CustomNode(uid=uid, name=nickname, content=content))
    return nodes


def _apply_channel_cap(board: QuotaBoard, limit: int) -> QuotaBoard:
    """每渠道最多显示 limit 张账号卡片；超出截断并用 hidden 记录未显示数量（不改缓存对象）。"""
    if limit <= 0 or not board.platforms:
        return board
    changed = False
    sections = []
    for section in board.platforms:
        if len(section.accounts) > limit:
            sections.append(replace(section, accounts=section.accounts[:limit], hidden=len(section.accounts) - limit))
            changed = True
        else:
            sections.append(section)
    return replace(board, platforms=sections) if changed else board


async def _send_forwarded(bot: Bot, outgoing: Sequence[tuple[str, bytes | None]]) -> None:
    """把多条额度结果合并为一条 OneBot V11 转发消息（文字与图片都在节点内容里）。"""
    nickname = await _bot_nickname(bot)
    nodes = _build_forward_nodes(outgoing, uid=bot.self_id, nickname=nickname)
    await UniMessage.reference(*nodes).finish()


async def _send_quota_results(
    cpa: CpaConfig,
    results: list[tuple[str, QuotaBoard | str]],
    *,
    want_text: bool,
    multi: bool,
) -> None:
    prefix = multi
    pinned = _apply_pin_order(results, state.get_snapshot().pin_channel)
    limit = state.get_snapshot().render.max_cards_per_channel
    outgoing: list[tuple[str, bytes | None]] = []
    for name, item in pinned:
        if isinstance(item, QuotaBoard):
            item = _apply_channel_cap(item, limit)
        if isinstance(item, str):
            outgoing.append((f"[{name}] {item}" if prefix else item, None))
            continue
        label = f"[{name}] " if prefix else ""
        if want_text or not cpa_uses_image(cpa, name):
            chunks = format_quota_board(item)
            outgoing.extend((f"{label}{chunk}" if label else chunk, None) for chunk in chunks)
            continue
        try:
            packed = await render_board_images(item)
        except RenderError as exc:
            # 出图失败只写终端日志，聊天里静默回退为文字总览。
            logger.warning(f"[{name}] 额度图渲染失败，已回退为文字总览：{exc}")
            chunks = format_quota_board(item)
            outgoing.extend((f"{label}{chunk}" if label else chunk, None) for chunk in chunks)
            continue
        if not packed:
            outgoing.append((f"{label}没有可展示的额度账号。" if label else "没有可展示的额度账号。", None))
            continue
        cache_note = " · 缓存" if item.cached else ""
        for key, images in packed:
            title = PLATFORM_TITLES.get(key, key)
            total = len(images)
            for index, png in enumerate(images, start=1):
                extra = f" {index}/{total}" if total > 1 else ""
                outgoing.append((f"{label}{title} 额度{extra}{cache_note}", png))
    if not outgoing:
        await UniMessage("没有可展示的额度账号。").finish()
        return
    bot = _current_onebot_bot()
    if bot is not None and state.get_snapshot().onebot_v11_feature.forward_message:
        await _send_forwarded(bot, outgoing)
        return
    for caption, png in outgoing[:-1]:
        if png is None:
            await UniMessage(caption).send()
        else:
            await UniMessage(Image(raw=png, mimetype="image/png")).send()
    caption, png = outgoing[-1]
    if png is None:
        await UniMessage(caption).finish()
    else:
        await UniMessage(Image(raw=png, mimetype="image/png")).finish()


def cpa_uses_image(cpa: CpaConfig, instance: str) -> bool:
    """该实例是否启用图片渲染（未找到时回退全局默认 True）。"""
    found = cpa.get(instance)
    return found.quota_image if found is not None else True
