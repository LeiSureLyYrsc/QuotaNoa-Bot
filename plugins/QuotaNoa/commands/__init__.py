"""命令层包：/quotanoa 根（额度 + 别名/主题/卡片/配置）与 /cpa 管理根。

import 本包即完成全部事件响应器注册。
"""

from __future__ import annotations

from nonebot.adapters import Bot, Event
from nonebot.message import event_preprocessor

from .. import state

from . import quota as quota  # noqa: E402, F401  (注册 /quotanoa 根 + 查询 + cooling/reset)
from . import alias as alias  # noqa: E402, F401  (注册 /quotanoa alias)
from . import theme as theme  # noqa: E402, F401  (注册 /quotanoa theme / card)
from . import config as config  # noqa: E402, F401  (注册 /quotanoa config)
from . import client as client  # noqa: E402, F401  (注册 /quotanoa client 远程客户端管理)
from . import wb as wb  # noqa: E402, F401  (注册 /quotanoa wb 网关管理)
from . import qoder as qoder  # noqa: E402, F401  (注册 /quotanoa qoder 代理管理)
from . import cpa as cpa  # noqa: E402, F401  (注册 /cpa 管理根)


@event_preprocessor
async def _refresh_config(bot: Bot, event: Event) -> None:
    """每个消息入口做一次廉价的配置热重载检查。"""
    try:
        state.ensure_fresh()
    except Exception:
        pass
