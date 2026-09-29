"""QuotaNoa 统一帮助文本与结构解析模块。

本模块是帮助文本与帮助图解析的单一事实来源（single source of truth）。
同时提供命令行展示纯文本生成与基于现有排版格式的无损解析（AST），
不依赖任何 QuotaNoa 子包，可被 commands 与 render 双向安全引用而无循环依赖。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = [
    "HelpEntry",
    "HelpNote",
    "HelpSection",
    "HelpDoc",
    "parse_help",
    "quota_help_text",
    "cpa_help_text",
]

_SECTION_RE = re.compile(r"^【(?P<title>[^】]*)】\s*(?P<intro>.*)$")
_COMMAND_RE = re.compile(r"^(?:/|(?:cpa|quotanoa)(?:\s|$))")
_DESC_SPLIT_RE = re.compile(r"\s{2,}")


@dataclass(frozen=True)
class HelpEntry:
    usage: str
    desc: str = ""
    details: tuple[str, ...] = ()


@dataclass(frozen=True)
class HelpNote:
    lines: tuple[str, ...] = ()


@dataclass(frozen=True)
class HelpSection:
    title: str
    intro: str = ""
    items: tuple[HelpEntry | HelpNote, ...] = ()
    raw: tuple[str, ...] = ()

    @property
    def entries(self) -> tuple[HelpEntry, ...]:
        return tuple(item for item in self.items if isinstance(item, HelpEntry))

    @property
    def notes(self) -> tuple[HelpNote, ...]:
        return tuple(item for item in self.items if isinstance(item, HelpNote))


@dataclass(frozen=True)
class HelpDoc:
    title: str
    subtitle: tuple[str, ...] = ()
    sections: tuple[HelpSection, ...] = ()
    raw: tuple[str, ...] = ()

    def to_text(self) -> str:
        return "\n".join(self.raw)


def quota_help_text() -> str:
    return "\n".join(
        [
            "QuotaNoa 额度查询（仅超级用户 / admins）",
            "命令固定带 / 前缀（指令头）。",
            "",
            "【查询】默认优先展示本地渠道（火山 / WorkBuddy / Qoder）；多实例时 CPA 结果按 [实例名] 前缀区分。",
            "  /quotanoa",
            "    无参数：本地渠道（火山 / WorkBuddy / Qoder）+ quotanoa_additional_channel 追加的渠道。",
            "    /cpa quota 无参：全部 CPA 平台 + cpa_additional_channel 追加的渠道。",
            "    追加渠道写 all（或 *）时该入口直接输出全部渠道。",
            "  /quotanoa all",
            "    查询全部渠道：本地渠道 + 全部 CPA 平台（同义 --all / -a）。",
            "  /quotanoa help",
            "    查看本帮助（同义 --help / -h）。",
            "  /quotanoa <平台>",
            "    claude / codex(gpt, openai) / antigravity(反重力, agy) / kimi / xai / 火山(volcengine, ark) / workbuddy(wb) / qoder(qd)",
            "  /quotanoa <实例>",
            "    只查指定 CPA 实例。例：/quotanoa Home",
            "  /quotanoa <平台> <实例>",
            "    例：/quotanoa antigravity Home  或  /quotanoa Home antigravity",
            "  /quotanoa <查询词>",
            "    单个账号的额度卡（跨全部实例搜索）。",
            "  /quotanoa --instance <实例>   显式指定实例，避免与渠道名冲突",
            "  /quotanoa --fresh     忽略缓存，强制重查上游",
            "  /quotanoa --text      只发文字总览（排障 / 无浏览器）",
            "  /quotanoa --client <名称>   只查指定远程客户端（--client all 查全部在线客户端）",
            "  /quotanoa cooling     只看冷却中的凭证（全部实例）",
            "  /quotanoa reset <查询词>   清除配额/冷却并恢复路由（跨实例搜索）",
            "",
            "【别名】分渠道存储（data/quotanoa_aliases.json）。",
            "  /quotanoa alias list [--disabled]",
            "  /quotanoa alias set <渠道> <查询词> <别名>",
            "    例：/quotanoa alias set antigravity user@example.com AG-1",
            "    渠道名可用文件里的 channel_keywords 自定义（如 agy → antigravity）。",
            "  /quotanoa alias del <查询词>    删除（跨渠道全部删除）",
            "",
            "【火山方舟】本地渠道，凭据存 data/quotanoa_config.json 的 volcengine.accounts。",
            "  支持 Coding Plan 与 Agent Plan，双套餐额度合并为一张卡片展示（含 Coding/Agent 档位徽章与到期时间）。",
            "  /quotanoa volc list",
            "  /quotanoa volc add <名称> <AK> <SK> [region]",
            "  /quotanoa volc remove <名称> --yes",
            "",
            "【WorkBuddy】本地渠道，网关存 data/quotanoa_config.json 的 workbuddy.servers。",
            "  /quotanoa wb              查询全部网关额度（同 workbuddy）",
            "  /quotanoa wb list",
            "  /quotanoa wb add <名称> <base_url> --user U --pass P [--timeout N]",
            "  /quotanoa wb login <名称>  校验账号密码并刷新会话",
            "  /quotanoa wb remove <名称> --yes",
            "",
            "【Qoder】本地渠道，代理存 data/quotanoa_config.json 的 qoder.servers。",
            "  /quotanoa qoder           查询全部代理号池额度（同 qd）",
            "  /quotanoa qoder list",
            "  /quotanoa qoder add <名称> <base_url> --key <API_KEY> [--timeout N]",
            "  /quotanoa qoder remove <名称> --yes",
            "",
            "【远程客户端】Server 模式：Go 客户端主动连接，额度来自远端 CPA/本地渠道。",
            "  配置在 data/quotanoa_config.json 的 server 段与顶层 clients（首建即生成，服务器模式默认关）。",
            "  /quotanoa client server on|off|show   服务端开关（enabled 热切换）与监听设置",
            "  /quotanoa client list",
            "  /quotanoa client add <名称> [--key K] [--allow-refresh] [--note N]",
            "  /quotanoa client show <名称>",
            "  /quotanoa client key <名称> [--rotate]",
            "  /quotanoa client remove <名称> --yes",
            "  刷新由客户端本地配置决定（默认关闭）；服务端仅在客户端上报允许时才发起。",
            "",
            "【主题与排版】修改后立刻生效并持久化。",
            "  /quotanoa theme           查看当前主题与可选主题",
            "  /quotanoa theme set <主题>",
            "  /quotanoa card            查看每行卡片数",
            "  /quotanoa card row N      设置每行卡片数（1..6）",
            "",
            "【配置】",
            "  /quotanoa config show     查看生效配置（密钥脱敏）与最近解析错误",
            "  /quotanoa config reload   强制从磁盘重载配置",
            "  /quotanoa config fix      补齐缺失配置项（先备份旧文件到 data/backup/）",
            "",
            "【管理】CPA 实例 / 凭证 / 登录 / Codex 重置请用 /cpa。",
        ]
    )


def cpa_help_text(providers: str) -> str:
    return "\n".join(
        [
            "CLIProxyAPI 管理（仅超级用户 / admins）",
            "命令前缀 / 可有可无：/cpa 与 cpa 相同。",
            "除登录回调外，所有子命令都要在第一个位置写 CPA 实例名。",
            "",
            "【实例管理】",
            "  cpa instance list",
            "  cpa instance add <名称> <base_url> [--key K] [--timeout N] [--quota-timeout N] [--concurrency N] [--cache-ttl N] [--no-image]",
            "  cpa instance show <名称>",
            "  cpa instance remove <名称> --yes",
            "",
            "【探活】",
            "  cpa status <实例>",
            "    版本、凭证 ready / 禁用 / 冷却计数。不回传配置正文。",
            "",
            "【凭证】",
            "  cpa auth list <实例> [渠道] [--disabled]",
            "    摘要列表。默认隐藏已禁用账号；加 --disabled 才显示。",
            "    渠道如 claude / codex(gpt, openai) / antigravity(反重力) / kimi / xai。",
            "  cpa auth show <实例> <查询词>",
            "  cpa auth on|off <实例> <查询词>",
            "  cpa auth models <实例> <查询词>",
            "  cpa auth delete <实例> <查询词> --yes",
            "",
            "【Codex 重置】消耗官方重置次数，立刻刷新 5h/周窗口。",
            "  仅 codex_refresh_admin 可执行。",
            "  cpa codex refresh <实例> <查询词>",
            "  cpa codex refresh <查询词> --client <客户端>   远程客户端刷新（需客户端本地已开启刷新）",
            "",
            "【登录】授权链接优先私聊。",
            f"  可用渠道：{providers}",
            "  cpa login <实例> <渠道>",
            "    完成后把浏览器地址栏完整回调链接发到当前聊天（会自动归属到该实例）。",
            "  cpa login <实例> callback <回调链接>",
            "  cpa login <实例> cancel",
            "",
            "【额度】默认查询全部 CPA 平台，可用 cpa_additional_channel 追加渠道（如 qoder / workbuddy；写 all = 全部渠道）。",
            "  cpa quota [平台] [实例] [--instance <实例>] [--fresh] [--text]",
            "    例：cpa quota xai JP-AI   只查 JP-AI 实例的 xAI 额度",
            "        cpa quota xai         查全部实例的 xAI 额度",
            "        cpa quota             查全部 CPA 实例（或配置的默认渠道）",
            "        cpa quota all         查本地渠道 + 全部 CPA 实例",
            "        cpa quota help        查看帮助",
        ]
    )


def parse_help(text: str) -> HelpDoc:
    """将既有帮助文本解析为 HelpDoc 结构。

    规则：
    1) 第一个非空行 = title；
    2) 第一个 section 之前的连续非缩进且不以【开头的行 = subtitle；
    3) 匹配 ^【(?P<title>[^】]*)】\\s*(?P<intro>.*)$ 的行开始一个 section；
    4) section 内缩进恰好 2 空格的行：
       若 stripped 行匹配 ^(?:/|(?:cpa|quotanoa)(?:\\s|$)) 则为 HelpEntry
       （在 2+ 空白处单次切分为 usage / desc）；
       否则为 HelpNote(lines=(stripped,))；
    5) 缩进 >= 4 空格的行作为 details 追加到最近的 entry/note；若无则新建 HelpNote；
    6) 空行仅参与 raw / to_text，不参与结构树。
    """
    lines = text.split("\n")
    raw = tuple(lines)

    title = ""
    subtitle: list[str] = []
    sections: list[HelpSection] = []

    # 状态
    in_section = False
    current_title = ""
    current_intro = ""
    current_items: list[HelpEntry | HelpNote] = []
    current_raw: list[str] = []

    for line in lines:
        stripped = line.strip()

        # 检查是否是新的 Section 标题行
        sec_match = _SECTION_RE.match(line)
        if sec_match:
            if in_section:
                sections.append(
                    HelpSection(
                        title=current_title,
                        intro=current_intro,
                        items=tuple(current_items),
                        raw=tuple(current_raw),
                    )
                )
            in_section = True
            current_title = sec_match.group("title")
            current_intro = sec_match.group("intro").strip()
            current_items = []
            current_raw = [line]
            continue

        if not in_section:
            # 尚未进入任何 section
            if not stripped:
                continue
            if not title:
                title = stripped
            elif not line.startswith((" ", "\t")) and not line.startswith("【"):
                subtitle.append(stripped)
            continue

        # 已处于 section 内部
        current_raw.append(line)
        if not stripped:
            continue

        # 计算缩进空格数
        indent = len(line) - len(line.lstrip(" "))

        if indent >= 4:
            # 缩进 >= 4 空格：追加到最近的 entry / note，若无则新建 HelpNote
            if current_items:
                last_item = current_items[-1]
                if isinstance(last_item, HelpEntry):
                    current_items[-1] = HelpEntry(
                        usage=last_item.usage,
                        desc=last_item.desc,
                        details=(*last_item.details, stripped),
                    )
                elif isinstance(last_item, HelpNote):
                    current_items[-1] = HelpNote(
                        lines=(*last_item.lines, stripped),
                    )
            else:
                current_items.append(HelpNote(lines=(stripped,)))
        else:
            # 缩进 < 4 空格（通常恰好 2 空格）
            if _COMMAND_RE.match(stripped):
                parts = _DESC_SPLIT_RE.split(stripped, maxsplit=1)
                usage = parts[0]
                desc = parts[1] if len(parts) > 1 else ""
                current_items.append(HelpEntry(usage=usage, desc=desc, details=()))
            else:
                current_items.append(HelpNote(lines=(stripped,)))

    if in_section:
        sections.append(
            HelpSection(
                title=current_title,
                intro=current_intro,
                items=tuple(current_items),
                raw=tuple(current_raw),
            )
        )

    return HelpDoc(
        title=title,
        subtitle=tuple(subtitle),
        sections=tuple(sections),
        raw=raw,
    )
