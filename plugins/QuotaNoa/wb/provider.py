"""WorkBuddy 额度 Provider：读取配置网关 → 并发查询 → 统一额度板。

本地渠道（非 CPA 实例）：凭据来自本机 ``workbuddy.servers[]``。多个网关的账号
汇总到**同一块** ``workbuddy`` 板，各自以网关名写入 ``AccountQuota.instance``，
因此沿用统一的 ``[网关名]`` 前缀展示。
"""

from __future__ import annotations

import asyncio
import time

from .. import state
from ..aliases import resolve_alias_for_keys
from ..cache import board_key, boards
from ..config import WorkbuddyServer
from ..model import AccountQuota, QuotaBoard, board_from_accounts
from .client import WorkbuddyError, fetch_quota
from .quota import parse_quota_accounts

CHANNEL = "workbuddy"


def clear_cache() -> None:
    boards.clear(f"{CHANNEL}:")


async def _server_reports(server: WorkbuddyServer) -> list[AccountQuota]:
    try:
        payload = await fetch_quota(server)
    except WorkbuddyError as exc:
        # 单网关失败不影响其他网关：以一条错误行呈现。
        return [
            AccountQuota(
                platform=CHANNEL,
                name=server.name,
                auth_index="",
                instance=server.name,
                status="error",
                error=str(exc),
            )
        ]
    reports = parse_quota_accounts(payload, server_name=server.name)
    for report in reports:
        alias = resolve_alias_for_keys(CHANNEL, [report.auth_index, report.name])
        if alias:
            report.name = alias
    return reports


async def collect_board(
    servers: list[WorkbuddyServer] | None = None, *, force: bool = False
) -> QuotaBoard:
    """查询全部（或指定）WorkBuddy 网关，返回聚合额度板。"""
    configs = list(servers if servers is not None else state.get_snapshot().workbuddy.servers)
    key = board_key(CHANNEL, ",".join(server.name for server in configs))
    if not force:
        cached = boards.get(key)
        if cached is not None:
            cached.cached = True
            return cached
    if not configs:
        return board_from_accounts([])
    grouped = await asyncio.gather(*(_server_reports(server) for server in configs))
    reports = [report for group in grouped for report in group]
    board = board_from_accounts(reports)
    board.fetched_at = time.time()
    boards.set(key, board, state.get_snapshot().cache_ttl(CHANNEL))
    return board
