"""火山方舟额度 Provider：读取配置账号 → 查询 → 统一额度板。

本地渠道（``local_only``）：凭据来自本机 ``volcengine.accounts``，不属于任何 CPA 实例。
"""

from __future__ import annotations

import time

from .. import state
from ..aliases import resolve_alias_for_keys
from ..cache import board_key, boards
from ..config import VolcengineAccount
from ..model import AccountQuota, QuotaBoard, board_from_accounts
from .client import (
    VolcengineError,
    query_afp_usage,
    query_coding_plan_usage,
    query_personal_plan,
)
from .quota import accounts_from_usage, parse_plan

CHANNEL = "volcengine"


def clear_cache() -> None:
    boards.clear(f"{CHANNEL}:")


async def _plan_info(account: VolcengineAccount, plan: str) -> dict:
    """拉取某个套餐（CodingPlan/AgentPlan）档位信息；失败或未订阅返回 {}。"""
    try:
        payload = await query_personal_plan(account, plan=plan)
    except VolcengineError:
        return {}
    return parse_plan(payload)


async def _usage(account: VolcengineAccount, query) -> tuple[dict | None, str]:
    """查询额度用量；返回 (payload, error)，失败时 payload=None。"""
    try:
        payload = await query(account)
    except VolcengineError as exc:
        return None, str(exc)
    return (payload if isinstance(payload, dict) else None), ""


async def collect_board(accounts=None, *, force: bool = False) -> QuotaBoard:
    configs = list(accounts if accounts is not None else state.get_snapshot().volcengine.accounts)
    key = board_key(CHANNEL, ",".join(account.name for account in configs))
    if not force:
        cached = boards.get(key)
        if cached is not None:
            cached.cached = True
            return cached
    reports: list[AccountQuota] = []
    for account in configs:
        coding_plan = await _plan_info(account, "CodingPlan")
        agent_plan = await _plan_info(account, "AgentPlan")
        coding_payload, coding_err = await _usage(account, query_coding_plan_usage)
        agent_payload, agent_err = await _usage(account, query_afp_usage)
        account_reports = accounts_from_usage(
            account,
            coding_payload,
            agent_payload,
            coding_plan=coding_plan,
            agent_plan=agent_plan,
        )
        if not account_reports:
            account_reports = [
                AccountQuota(
                    platform=CHANNEL,
                    name=account.name,
                    auth_index="",
                    status="error",
                    error=coding_err
                    or agent_err
                    or "未查询到 Coding/Agent Plan 额度（可能未订阅）。",
                )
            ]
        alias = resolve_alias_for_keys(CHANNEL, [account.name])
        for report in account_reports:
            if alias:
                report.name = alias
            reports.append(report)
    board = board_from_accounts(reports)
    board.fetched_at = time.time()
    boards.set(key, board, state.get_snapshot().cache_ttl(CHANNEL))
    return board
