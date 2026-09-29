"""统一字段语法高亮模块。

本模块提供可复用的命令、参数、选项、键名及告警标记高亮解析器。
主要供帮助卡片渲染（help_page）及账号卡片错误信息（card error box）共享调用（通过 highlight_html）。

高亮类型说明表（Kinds）：
+-------+----------------------------------------------------+--------------------------------------------------+
| 类别  | 语义与规则                                         | 示例                                             |
+-------+----------------------------------------------------+--------------------------------------------------+
| cmd   | 指令根词与支持的子命令关键字连缀                   | /quotanoa, cpa auth on|off, /cpa quota           |
| arg   | 尖括号包裹的必选/位置参数                          | <实例>, <平台>, <查询词>                         |
| opt   | 短/长选项开关                                      | --instance, -a, --fresh, --disabled              |
| key   | 包含点、下划线或连字符的配置键/文件名              | quotanoa_config.json, volcengine.accounts        |
| warn  | 警示符号                                           | ⚠                                                |
+-------+----------------------------------------------------+--------------------------------------------------+
"""

from __future__ import annotations

import html
import re
from collections.abc import Iterable
from dataclasses import dataclass

__all__ = [
    "Segment",
    "highlight_line",
    "segments_to_text",
    "segments_to_html",
    "highlight_html",
    "segment_kinds",
    "KINDS",
]

KINDS: tuple[str, ...] = ("cmd", "arg", "opt", "key", "warn")

_WORDS = (
    "instance|auth|codex|login|quota|status|help|all|cooling|reset|alias|theme|"
    "card|config|volc|volcengine|workbuddy|wb|qoder|qd|list|add|remove|rm|delete|"
    "set|show|row|reload|fix|on|off|enable|disable|models|refresh|cancel|callback"
)

_HIGHLIGHT_RE = re.compile(
    rf"(?P<arg><[^<>\s]{{1,32}}>)"
    rf"|(?P<opt>(?<![\w-])--?[A-Za-z][\w-]*)"
    rf"|(?P<cmd>(?<![\w/-])(?:/)?(?:quotanoa|cpa)(?![\w.-])(?:[\s|]+(?:{_WORDS})(?![\w.-]))*)"
    rf"|(?P<key>(?<![\w-])[A-Za-z][A-Za-z0-9]*(?:[._-][A-Za-z0-9]+)+(?![\w-]))"
    rf"|(?P<warn>⚠)"
)


@dataclass(frozen=True)
class Segment:
    kind: str
    text: str


def highlight_line(text: str) -> tuple[Segment, ...]:
    """将单行文本切分为高亮段与普通文本段。

    保证无损（lossless），空字符串返回空元组。
    """
    if not text:
        return ()

    segments: list[Segment] = []
    last_end = 0

    for m in _HIGHLIGHT_RE.finditer(text):
        start, end = m.span()
        if start > last_end:
            segments.append(Segment("text", text[last_end:start]))
        kind = m.lastgroup or "text"
        segments.append(Segment(kind, m.group()))
        last_end = end

    if last_end < len(text):
        segments.append(Segment("text", text[last_end:]))

    return tuple(segments)


def segments_to_text(segments: Iterable[Segment]) -> str:
    """还原高亮段为原始文本（无损操作）。"""
    return "".join(seg.text for seg in segments)


def segments_to_html(segments: Iterable[Segment]) -> str:
    """将高亮段转换为转义后的 HTML。

    对所有内容执行 html.escape；
    kind == "text" 保持原样转义；
    其他种类外包 <span class="hl hl-<kind>">...</span>。
    """
    out: list[str] = []
    for seg in segments:
        escaped = html.escape(seg.text)
        if seg.kind == "text":
            out.append(escaped)
        else:
            out.append(f'<span class="hl hl-{seg.kind}">{escaped}</span>')
    return "".join(out)


def highlight_html(text: str) -> str:
    """单调用便捷函数：对文本执行切分并渲染为 HTML。"""
    return segments_to_html(highlight_line(text))


def segment_kinds(text: str) -> list[str]:
    """按出现顺序提取文本中包含的所有非 text 高亮类别（去重）。"""
    seen: set[str] = set()
    result: list[str] = []
    for seg in highlight_line(text):
        if seg.kind != "text" and seg.kind not in seen:
            seen.add(seg.kind)
            result.append(seg.kind)
    return result
