"""/cpa 管理命令：实例管理、凭证巡检、OAuth 登录、Codex 重置。

多实例模型：几乎所有操作都显式带上实例名（第一个位置参数）。实例的增删改写在
``data/quotanoa_config.json`` 的 ``cpa.instances[]``。
"""

from __future__ import annotations

from typing import Any, Mapping

from arclet.alconna import Alconna, Args, CommandMeta, MultiVar, Option, Subcommand, store_true
from nonebot.adapters import Bot, Event
from nonebot.exception import IgnoredException
from nonebot.message import event_preprocessor
from nonebot_plugin_alconna import Arparma, Query, UniMessage, on_alconna

from .. import state
from ..config import ConfigError, DEFAULT_CPA_BASE_URL, normalize_name, valid_name
from ..cpa.client import CPAError, get_client
from ..cpa.format import (
    display_name,
    format_ambiguous,
    format_auth_detail,
    format_auth_list,
    format_models,
    format_probe,
    looks_like_oauth_callback,
    match_auth,
)
from ..cpa.oauth import (
    cancel_login,
    discover_auth_urls,
    has_pending,
    resolve_auth_path,
    start_login,
    submit_callback,
)
from ..cpa.quota import refresh_codex_quota
from ..clienthub import HubError, get_hub
from ..help import cpa_help_text, parse_help
from ..protocol import normalize_client_name
from ..query import TEXT_FLAGS, strip_quota_head, tokenize

from .common import (
    CPA_ADMIN,
    _can_refresh_codex,
    _require_one,
    _require_platform_account,
    _set_disabled,
    _text,
    _without,
    instance_names,
    send_help,
)
from .quota import quota_entry

cpa = on_alconna(
    Alconna(
        ["/", ""],
        "cpa",
        Subcommand(
            "instance",
            Subcommand("list", help_text="列出已配置实例"),
            Subcommand(
                "add",
                Args["name", str]["base_url", str],
                Option("--key", Args["key", str], dest="key", help_text="管理密钥"),
                Option("--timeout", Args["timeout", str], dest="timeout", help_text="请求超时秒"),
                Option("--quota-timeout", Args["quota_timeout", str], dest="quota_timeout", help_text="额度查询超时秒"),
                Option("--concurrency", Args["concurrency", str], dest="concurrency", help_text="额度查询并发"),
                Option("--cache-ttl", Args["cache_ttl", str], dest="cache_ttl", help_text="额度缓存秒"),
                Option("--no-image", action=store_true, dest="no_image", help_text="该实例只发文字"),
                help_text="新增 CPA 实例",
            ),
            Subcommand(
                "remove|rm|delete",
                Args["name", str],
                Option("--yes|-y", action=store_true, dest="yes", help_text="确认删除"),
                dest="remove",
                help_text="删除 CPA 实例",
            ),
            Subcommand("show", Args["name", str], help_text="查看实例详情（密钥脱敏）"),
            help_text="CPA 实例管理",
        ),
        Subcommand("status", Args["instance", str], help_text="探活与凭证概览"),
        Subcommand(
            "quota",
            Args["a?", str]["b?", str]["tail", MultiVar(str, "*")],
            help_text="查询额度（同 /quotanoa）：/cpa quota [平台] [实例] [--fresh|--text]",
        ),
        Subcommand(
            "auth",
            Subcommand(
                "list",
                Args["instance", str]["provider?", str],
                Option("--disabled", action=store_true, dest="disabled", help_text="包含已禁用账号"),
                help_text="凭证摘要列表",
            ),
            Subcommand("show", Args["instance", str]["query", str], help_text="凭证详情"),
            Subcommand("on|enable", Args["instance", str]["query", str], dest="on", help_text="启用凭证"),
            Subcommand("off|disable", Args["instance", str]["query", str], dest="off", help_text="禁用凭证"),
            Subcommand("models", Args["instance", str]["query", str], help_text="凭证支持的模型"),
            Subcommand(
                "delete",
                Args["instance", str]["query", str],
                Option("--yes|-y", action=store_true, help_text="确认删除"),
                help_text="删除凭证文件",
            ),
            help_text="凭证管理",
        ),
        Subcommand(
            "codex",
            Subcommand(
                "refresh",
                Args["a", str]["b?", str],
                Option("--client|-c", Args["client", str], dest="client", help_text="指定远程客户端（此时第 1 个位置参数为查询词）"),
                help_text="消耗一次 Codex 重置次数并刷新额度",
            ),
            help_text="Codex 上游额度操作",
        ),
        Subcommand(
            "login",
            Subcommand("cancel", help_text="取消进行中的登录"),
            Subcommand("callback", Args["url", str], help_text="提交浏览器回调链接"),
            Args["instance", str]["provider?", str],
            help_text="OAuth / 设备码登录",
        ),
        meta=CommandMeta(
            description="CLIProxyAPI 管理（仅管理员）",
            usage="发送 cpa 或 /cpa 查看完整帮助",
            example="cpa instance add Home http://127.0.0.1:8317 --key KEY\ncpa status Home\ncpa auth list Home claude\ncpa codex refresh Home user@example.com\ncpa login Home claude",
        ),
    ),
    permission=CPA_ADMIN,
    auto_send_output=True,
    skip_for_unmatch=False,
    use_cmd_start=True,
    block=True,
)


@event_preprocessor
async def _capture_oauth_callback(bot: Bot, event: Event) -> None:
    try:
        if getattr(event, "post_type", "") == "message_sent":
            return
        if not has_pending(bot, event):
            return
        if not await CPA_ADMIN(bot, event):
            return
        text = event.get_plaintext().strip()
    except Exception:
        return
    if not text or text.lower().startswith(("cpa ", "/cpa ", "quotanoa ", "/quotanoa ")):
        return
    if not looks_like_oauth_callback(text):
        return
    try:
        message = await submit_callback(bot, event, text)
    except CPAError as exc:
        await UniMessage(str(exc)).send()
        raise IgnoredException("cpa oauth callback") from exc
    await UniMessage(message).send()
    raise IgnoredException("cpa oauth callback")


@cpa.assign("$main")
async def cpa_help(event: Event) -> None:
    providers = "（暂时无法获取，CPA 未连通时仍可看本帮助）"
    names = instance_names()
    if names:
        try:
            mapping = await discover_auth_urls(names[0])
            known = ", ".join(sorted(set(mapping)))
            if known:
                providers = known
        except CPAError:
            pass
    parts = strip_quota_head(tokenize(event.get_plaintext()))
    await send_help(
        parse_help(cpa_help_text(providers)),
        text=any(p.lower() in TEXT_FLAGS for p in parts),
    )


# --------------------------------------------------------------------------- #
# 实例管理
# --------------------------------------------------------------------------- #


def _instances_raw() -> list[dict[str, Any]]:
    raw = state.get_snapshot().raw
    cpa_raw = raw.get("cpa") if isinstance(raw, Mapping) else None
    inst = cpa_raw.get("instances") if isinstance(cpa_raw, Mapping) else None
    if not isinstance(inst, list):
        return []
    return [dict(item) for item in inst if isinstance(item, Mapping)]


def _write_instances(instances: list[dict[str, Any]]) -> None:
    state.update_config({"cpa": {"instances": instances}})


@cpa.assign("instance.list")
async def instance_list() -> None:
    snapshot = state.get_snapshot()
    if not snapshot.cpa.instances:
        await UniMessage("还没有配置 CPA 实例。新增：/cpa instance add <名称> <base_url> [--key K]").finish()
        return
    lines = ["【CPA 实例】"]
    for item in snapshot.cpa.instances:
        key = "已设置" if item.management_key else "（未设置）"
        lines.append(f"  {item.name}  {item.base_url}  key={key}")
    await UniMessage("\n".join(lines)).finish()


@cpa.assign("instance.show")
async def instance_show(name: Query[str] = Query("instance.show.name")) -> None:
    from ..cpa.format import mask_secret

    raw = _text(name)
    instance = state.get_snapshot().cpa.get(raw)
    if instance is None:
        await UniMessage(f"没有名为「{raw}」的实例。查看：/cpa instance list").finish()
        return
    lines = [
        f"【实例 {instance.name}】",
        f"  base_url：{instance.base_url}",
        f"  management_key：{mask_secret(instance.management_key) if instance.management_key else '（未设置）'}",
        f"  timeout：{instance.timeout}s",
        f"  oauth：poll {instance.oauth_poll_interval}s / timeout {instance.oauth_timeout}s",
        f"  quota：timeout {instance.quota_timeout}s  concurrency {instance.quota_concurrency}  cache_ttl {instance.quota_cache_ttl}s",
        f"  quota_image：{instance.quota_image}",
    ]
    await UniMessage("\n".join(lines)).finish()


@cpa.assign("instance.add")
async def instance_add(
    arp: Arparma,
    name: Query[str] = Query("instance.add.name"),
    base_url: Query[str] = Query("instance.add.base_url"),
    # Option 取值比 store_true 多一层：值在 <option>.<arg> 路径上（如 instance.add.key.key）。
    key: Query[str] = Query("instance.add.key.key"),
    timeout: Query[str] = Query("instance.add.timeout.timeout"),
    quota_timeout: Query[str] = Query("instance.add.quota_timeout.quota_timeout"),
    concurrency: Query[str] = Query("instance.add.concurrency.concurrency"),
    cache_ttl: Query[str] = Query("instance.add.cache_ttl.cache_ttl"),
) -> None:
    raw_name = normalize_name(_text(name))
    if not valid_name(raw_name):
        await UniMessage(f"实例名称非法：{_text(name)}（1–32 字符，不能含空白或 / \\ :）").finish()
        return
    url = _text(base_url).rstrip("/")
    if not url:
        await UniMessage("base_url 不能为空。").finish()
        return
    instances = _instances_raw()
    if any(normalize_name(str(item.get("name") or "")) == raw_name for item in instances):
        await UniMessage(f"实例「{raw_name}」已存在。查看：/cpa instance list").finish()
        return
    entry: dict[str, Any] = {"name": raw_name, "base_url": url}
    if key.available and _text(key):
        entry["management_key"] = _text(key)
    for dest, query in (
        ("timeout", timeout),
        ("quota_timeout", quota_timeout),
        ("quota_concurrency", concurrency),
        ("quota_cache_ttl", cache_ttl),
    ):
        if query.available and _text(query):
            entry[dest] = _text(query)
    if arp.find("instance.add.no_image"):
        entry["quota_image"] = False
    instances.append(entry)
    try:
        _write_instances(instances)
    except ConfigError as exc:
        await UniMessage(f"写入配置失败：{exc}").finish()
        return
    await UniMessage(f"已新增 CPA 实例「{raw_name}」→ {url}。查看：/cpa instance list").finish()


@cpa.assign("instance.remove")
async def instance_remove(
    arp: Arparma,
    name: Query[str] = Query("instance.remove.name"),
) -> None:
    raw_name = normalize_name(_text(name))
    instances = _instances_raw()
    remaining = [item for item in instances if normalize_name(str(item.get("name") or "")) != raw_name]
    if len(remaining) == len(instances):
        await UniMessage(f"没有名为「{raw_name}」的实例。查看：/cpa instance list").finish()
        return
    if not arp.find("instance.remove.yes"):
        await UniMessage(f"即将删除实例「{raw_name}」。确认请发送：\ncpa instance remove {raw_name} --yes").finish()
        return
    try:
        _write_instances(remaining)
    except ConfigError as exc:
        await UniMessage(f"写入配置失败：{exc}").finish()
        return
    await UniMessage(f"已删除 CPA 实例「{raw_name}」。").finish()


# --------------------------------------------------------------------------- #
# 探活 / 凭证 / 登录 / codex
# --------------------------------------------------------------------------- #


@cpa.assign("quota")
async def cpa_quota(event: Event) -> None:
    """`/cpa quota` 额度查询入口：默认查全部 CPA 实例。"""
    await quota_entry(event, entry="cpa")


@cpa.assign("status")
async def cpa_status(instance: Query[str] = Query("status.instance")) -> None:
    name = _text(instance)
    try:
        client = get_client(name)
        headers = await client.probe()
        files = await client.list_auth_files()
        latest = await client.latest_version()
    except CPAError as exc:
        await UniMessage(str(exc)).finish()
        return
    await UniMessage(f"[{name}] " + format_probe(headers, files, latest)).finish()


@cpa.assign("auth.list")
async def auth_list(
    arp: Arparma,
    instance: Query[str] = Query("auth.list.instance"),
    provider: Query[str] = Query("auth.list.provider"),
) -> None:
    name = _text(instance)
    try:
        files = await get_client(name).list_auth_files()
    except CPAError as exc:
        await UniMessage(str(exc)).finish()
        return
    if provider.available:
        needle = provider.result.strip().lower()
        files = [item for item in files if str(item.get("provider") or "").lower() == needle]
        if not files:
            await UniMessage(f"[{name}] 没有 provider={provider.result} 的凭证。").finish()
            return
    await UniMessage(
        f"[{name}]\n"
        + format_auth_list(files, include_disabled=bool(arp.find("auth.list.disabled")), instance=name)
    ).finish()


@cpa.assign("auth.show")
async def auth_show(
    instance: Query[str] = Query("auth.show.instance"),
    query: Query[str] = Query("auth.show.query"),
) -> None:
    file = await _require_one(_text(instance), _text(query))
    await UniMessage(format_auth_detail(file)).finish()


@cpa.assign("auth.on")
async def auth_on(
    instance: Query[str] = Query("auth.on.instance"),
    query: Query[str] = Query("auth.on.query"),
) -> None:
    await _set_disabled(_text(instance), _text(query), disabled=False)


@cpa.assign("auth.off")
async def auth_off(
    instance: Query[str] = Query("auth.off.instance"),
    query: Query[str] = Query("auth.off.query"),
) -> None:
    await _set_disabled(_text(instance), _text(query), disabled=True)


@cpa.assign("auth.models")
async def auth_models(
    instance: Query[str] = Query("auth.models.instance"),
    query: Query[str] = Query("auth.models.query"),
) -> None:
    name = _text(instance)
    file = await _require_one(name, _text(query))
    filename = str(file.get("name") or "")
    if not filename:
        await UniMessage("该凭证没有文件名，无法查询模型列表。").finish()
        return
    try:
        models = await get_client(name).auth_models(filename)
    except CPAError as exc:
        await UniMessage(str(exc)).finish()
        return
    await UniMessage(format_models(models)).finish()


@cpa.assign("auth.delete")
async def auth_delete(
    arp: Arparma,
    instance: Query[str] = Query("auth.delete.instance"),
    query: Query[str] = Query("auth.delete.query"),
) -> None:
    name = _text(instance)
    file = await _require_one(name, _text(query))
    confirmed = arp.find("auth.delete.yes")
    filename = str(file.get("name") or "")
    if not filename:
        await UniMessage("该凭证没有可删除的磁盘文件（可能是 runtime_only）。").finish()
        return
    if not confirmed:
        await UniMessage(
            f"即将从实例「{name}」删除凭证 {filename}。确认请发送：\ncpa auth delete {name} {query.result} --yes"
        ).finish()
        return
    try:
        await get_client(name).delete_auth_file(filename)
    except CPAError as exc:
        await UniMessage(str(exc)).finish()
        return
    await UniMessage(f"已从「{name}」删除 {filename}。").finish()


@cpa.assign("codex.refresh")
async def codex_refresh(
    event: Event,
    a: Query[str] = Query("codex.refresh.a"),
    b: Query[str] = Query("codex.refresh.b"),
    client: Query[str] = Query("codex.refresh.client.client"),
) -> None:
    if not _can_refresh_codex(event):
        await UniMessage("未配置 codex_refresh_admin，或你不在名单中，无法刷新。").finish()
        return
    server = state.get_client_snapshot().server
    server_name = normalize_client_name(server.server_name)
    client_param = _text(client) if client.available else ""
    target = normalize_client_name(client_param) if client_param else ""
    # 远程客户端：cpa codex refresh <查询词> --client <客户端>
    if client_param and target != server_name:
        account_query = _text(a)
        if not account_query:
            await UniMessage("查询词不能为空。").finish()
            return
        known = get_hub().known_names(state.client_registry())
        if target not in known:
            await UniMessage(f"未知客户端：{client_param}").finish()
            return
        try:
            res = await get_hub().refresh_codex(target, account_query)
        except HubError as exc:
            await UniMessage(str(exc)).finish()
            return
        extra = f"\n剩余重置次数：{res.remaining_credits}" if res.remaining_credits is not None else ""
        await UniMessage(f"[客户端 {target}] {res.message}{extra}").finish()
        return
    # 本机：cpa codex refresh <实例> <查询词>
    if not b.available or not _text(b):
        await UniMessage(
            "用法：cpa codex refresh <实例> <查询词>\n"
            "      或 cpa codex refresh <查询词> --client <客户端>"
        ).finish()
        return
    instance_name = _text(a)
    account_query = _text(b)
    file = await _require_platform_account(instance_name, "codex", account_query)
    try:
        message = await refresh_codex_quota(file, instance_name)
    except CPAError as exc:
        await UniMessage(str(exc)).finish()
        return
    await UniMessage(message).finish()


@cpa.assign("login.cancel")
async def login_cancel(bot: Bot, event: Event) -> None:
    try:
        message = await cancel_login(bot, event)
    except CPAError as exc:
        await UniMessage(str(exc)).finish()
        return
    await UniMessage(message).finish()


@cpa.assign("login.callback")
async def login_callback(bot: Bot, event: Event, url: Query[str] = Query("login.callback.url")) -> None:
    try:
        message = await submit_callback(bot, event, _text(url))
    except CPAError as exc:
        await UniMessage(str(exc)).finish()
        return
    await UniMessage(message).finish()


@cpa.assign("login", additional=_without("login.cancel", "login.callback"))
async def login_start(
    bot: Bot,
    event: Event,
    instance: Query[str] = Query("login.instance"),
    provider: Query[str] = Query("login.provider"),
) -> None:
    name = _text(instance)
    if not provider.available:
        mapping = await discover_auth_urls(name)
        known = ", ".join(sorted(set(mapping)))
        await UniMessage(f"用法：cpa login <实例> <渠道>\n可用渠道：{known or '（无法获取）'}").finish()
        return
    try:
        canonical, path = await resolve_auth_path(name, _text(provider))
        payload = await get_client(name).start_login(path)
        await start_login(bot, event, name, canonical, payload)
    except CPAError as exc:
        await UniMessage(str(exc)).finish()
