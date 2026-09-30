# QuotaNoa Bot

NoneBot2 + Alconna 插件，让管理员在聊天里操作 [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) 管理接口：探活、凭证巡检、OAuth 登录、额度/冷却查看与 `reset-quota`。

命令收发走 `UniMessage`，不绑定单一适配器。本仓库默认装了 OneBot V11 与 Telegram。

## 启动

```bash
uv sync
cp .env.example .env.prod   # Windows: Copy-Item .env.example .env.prod
# 编辑 .env.prod：SUPERUSERS、适配器；插件业务配置在 data/quotanoa_config.json

# 要用额度卡片图时必须先装浏览器，否则自动回退文字
uv run playwright install chromium

uv run nb run
```

未执行 `uv run playwright install chromium` 时，`/quotanoa` 会回退纯文字，也可设 `cpa.quota_image=false` 或加 `--text`。出图失败的原因只写终端日志，不在聊天里提示。

## 配置

写在 NoneBot 的 `.env` / `.env.prod` 里。仓库已提供 `.env.example`，复制后改占位符即可。管理密钥、Bot Token、`data/` 不要提交到 git。

### 通用

```env
DRIVER=~fastapi+~httpx+~websockets+~aiohttp
SUPERUSERS=["12345678"]
COMMAND_START=["/"]
```

`SUPERUSERS` 填各平台的 user id（QQ 号 / Telegram 数字 id）。可同时写多个。

### OneBot V11

反向 WebSocket：NoneBot 主动连接协议端（NapCat / Lagrange / LLOneBot 等）。

```env
ONEBOT_WS_URLS=["ws://127.0.0.1:3001"]
ONEBOT_ACCESS_TOKEN=your_onebot_token
```

协议端需开启反向 WS，地址与 token 和这里一致。

### Telegram

可与 OneBot 同时启用。Token 从 [@BotFather](https://t.me/BotFather) 获取。

```env
telegram_bots=[{"token": "123456:ABC-DEF"}]
# TELEGRAM_PROXY=http://127.0.0.1:7890
```

走系统 / 本地代理访问 Telegram API 时取消注释 `TELEGRAM_PROXY`。

### QuotaNoa 插件

业务配置（CPA 实例、火山凭据、渲染主题、别名文件路径）全部放在 `data/quotanoa_config.json`，首次启动自动生成，支持热重载。`.env` 里只有这一项可选覆盖：

```env
# QUOTANOA_CONFIG_FILE=data/quotanoa_config.json
```

`quotanoa_config.json` 结构：

```jsonc
{
  "cpa": {
    "admins": [],                    // 额外管理员 user id（全局）
    "codex_refresh_admin": [],       // 全局
    "instances": [                   // 每个实例独立连接与额度设置
      {
        "name": "Home",
        "base_url": "http://127.0.0.1:8317",
        "management_key": "",        // 明文管理密钥
        "timeout": 15.0,
        "oauth_poll_interval": 3.0,
        "oauth_timeout": 1800.0,
        "quota_timeout": 25.0,
        "quota_concurrency": 4,
        "quota_cache_ttl": 0.0,
        "quota_image": true
      }
    ]
  },
  "volcengine": {
    "accounts": [
      { "name": "火山主号", "access_key_id": "AKLT…", "secret_access_key": "…", "region": "cn-beijing" }
    ]
  },
  "workbuddy": {
    "servers": [
      { "name": "wb-main", "base_url": "http://127.0.0.1:7863", "username": "admin", "password": "workbuddy", "timeout": 30.0 }
    ]
  },
  "qoder": {
    "servers": [                       // Qoder2OAPI 代理服务
      { "name": "qoder-main", "base_url": "http://127.0.0.1:8000", "api_key": "…", "timeout": 30.0 }
    ]
  },
  "refreshcache": {
    "default": 600,                // 未单独配置渠道的默认缓存秒数（默认 10 分钟；0 = 不缓存）
    "channels": {                  // 按渠道覆盖；键为渠道名（claude/codex/火山/workbuddy…）
      "claude": 120,
      "codex": 300,
      "volcengine": 0
    }
  },
  "render": { "theme": "default", "cards_per_row": 3, "max_cards_per_channel": 40 },
  "onebot-v11-feature": { "forward-message": false },
  "pin-channel": [],                // 渠道置顶顺序（通用，对所有适配器生效；如 ["xai", "火山"]）
  "quotanoa_additional_channel": [],  // /quotanoa 无参时在本地渠道之外追加的渠道（如 antigravity；写 all = 全部渠道）
  "cpa_additional_channel": []        // /cpa quota 无参时在全部 CPA 平台之外追加的渠道（如 qoder / workbuddy；写 all = 全部渠道）
}
```

| 段 | 说明 |
| --- | --- |
| `cpa.instances[]` | 每个 CLIProxyAPI 实例一项，自带 `base_url` / `management_key` / 超时 / 并发 / 缓存 / 图片开关。`base_url` 可写 `http://host:8317` 或带 `/v0/management` 的完整前缀 |
| `cpa.admins` / `cpa.codex_refresh_admin` | 全局权限名单（与实例无关） |
| `quotanoa_additional_channel` | `/quotanoa` 无参默认查询的**追加渠道**：默认先查本地渠道（`volcengine`/`workbuddy`/`qoder`），再把这里的渠道追加在后。填 CPA 平台名（`claude`/`codex`/`antigravity`/`kimi`/`xai`/`gemini-cli`）即让 `/quotanoa` 也带出这些 CPA 额度；填 `all`（或 `*`）则无参 `/quotanoa` 直接输出全部渠道 |
| `cpa_additional_channel` | `/cpa quota` 无参默认查询的**追加渠道**：默认先查全部 CPA 平台，再把这里的渠道追加在后。填本地渠道名（`volcengine`/`workbuddy`/`qoder`）即让 `/cpa quota` 也带出这些本地额度；填 `all`（或 `*`）则无参 `/cpa quota` 直接输出全部渠道 |
| `volcengine.accounts` | 火山方舟 Coding Plan / Agent Plan 查询凭据（控制面 AccessKey，需 `ArkReadOnlyAccess`） |
| `workbuddy.servers[]` | 每个 WorkBuddy2API 网关一项：`base_url`（如 `http://host:7863`）、`username` + `password`（控制台账号，插件自动登录换 `api_key`）、可选 `api_key`（跳过登录直连）、`timeout`。多个网关的账号会汇总到同一张 WorkBuddy 板，按网关名前缀区分 |
| `qoder.servers[]` | 每个 Qoder2OAPI 代理一项：`name`、`base_url`（如 `http://127.0.0.1:8000`）、`api_key`、`timeout`。多个代理的号池账号会汇总到同一张 Qoder 板，按代理名前缀区分 |
| `refreshcache` | 各渠道查询结果的缓存秒数（默认 `600` = 10 分钟）：`default` 为兜底，`channels` 按渠道名覆盖（支持别名如 `gpt`/`火山` 归一）。CPA 实例未命中渠道覆盖时回退到实例 `quota_cache_ttl`（`0` = 跟随 `default`）；`0` 表示该渠道不缓存。`/quotanoa --fresh` 仍强制重查。命中缓存时结果会标注 `缓存 N 分钟前`，实时查询标注 `现在` |
| `render` | 额度图主题、每行卡片数（默认 3 列，范围 1..6）与每渠道账号卡片上限（默认 40），可用 `/quotanoa theme`、`/quotanoa card row`、`/quotanoa card max` 修改 |
| `onebot-v11-feature.forward-message` | **仅 OneBot V11 适配器**生效：`true` 时把 `/quotanoa` / `/cpa quota` 的多条额度结果（标题文字 + 图片）合并成**一条合并转发**消息发出，节点署名取 Bot 真实昵称（失败回退 Bot 号）。Telegram 等其它适配器与 `false` 时按原样逐条发送；查询过程中的「正在查询…」提示始终单独发送，不参与合并 |
| `pin-channel` | **渠道置顶**（通用，对所有适配器生效）：数组顺序即发送顺序，**左 → 右 = 上 → 下**。命中的渠道整体前置，未命中当前查询列表的渠道自动忽略，其余渠道保持默认顺序。值支持别名（`火山`→`volcengine`、`wb`→`workbuddy`、`反重力`→`antigravity`）。例：`["xai", "火山"]` 下 `/cpa quota` 全部渠道时 xAI 在最顶部（含 xAI 的 CPA 板整块浮到本地渠道之前），`/quotanoa` 默认只查本地渠道、xai 不在列表里被忽略，火山置顶 |

别名文件路径由代码（`plugins/QuotaNoa/config.py` 的 `DEFAULT_ALIASES_FILE`）决定，默认 `data/quotanoa_aliases.json`，**不写入生成的配置文件**；如需改路径，可在 JSON 里显式加可选覆盖项 `"aliases_file"`（旧配置兼容）。

修改配置后**自动热重载**（也可 `/quotanoa config reload` 强制）；`/quotanoa config show` 查看当前生效值。旧的 `CPA_*` 环境变量与 `data/cpa_aliases.json` / `data/quota_aliases.json` / `data/quotabot_config.json` / `data/cpa_render_settings.json` 不再生效（启动时会告警，不做自动迁移）。旧的单实例 `cpa.base_url` 字段不再读取，请改为 `cpa.instances[]`。

Bot 与 CPA 不在同一台机器时，CPA 需要 `remote-management.allow-remote: true`，或设置环境变量 `MANAGEMENT_PASSWORD`（会强制允许远程）。未配置任何管理密钥时，`/v0/management` 会 404。

## 远程客户端（Server 模式）

Bot 可同时作为独立的远程客户端服务端（**独立 FastAPI 实例**，不与 NoneBot 共用），让位于其它机器 / NAT 后的 [QuotaNoa-Client](https://github.com/LeiSureLyYrsc/QuotaNoa-Client)（Go）主动连入，远程查询其本机 CLIProxyAPI 与本地渠道额度，并在受控条件下执行 Codex 重置。

### 服务端（Bot）

服务端监听设置与客户端列表都存在 `data/quotanoa_config.json`（首次启动自动生成，**服务器模式默认关**）：

```jsonc
{
  // ...其余业务配置...
  "server": {
    "enabled": false,          // 独立开关；可用 /quotanoa client server on 热开启
    "server_name": "Server",   // 本机保留名
    "host": "127.0.0.1",
    "port": 8320,
    "request_timeout": 40.0,
    "ws_max_size": 1048576,
    "max_accounts": 200
  },
  "clients": [
    { "name": "Home", "key": "<随机密钥>", "allow_refresh": false, "note": "" }
  ]
}
```

- `server.enabled` 支持热切换（`/quotanoa client server on|off`）；`host`/`port` 等监听参数变更需重启 Bot。
- `clients` 默认空；`/quotanoa client add` 自动追加。

客户端通过 `WS /v1/client/ws` 连接，携带 `Authorization: Bearer <key>` 与 `X-CPA-Client-Name: <名称>`；协议 v2，握手时上报 agent 版本与刷新能力。

### 命令

| 命令 | 作用 |
| --- | --- |
| `/quotanoa client server on` / `off` | 开启 / 关闭远程客户端服务端（热切换） |
| `/quotanoa client server show` | 查看监听设置与在线客户端 |
| `/quotanoa client add <名称> [--key K] [--allow-refresh] [--note N]` | 创建客户端实例（写入 `clients`） |
| `/quotanoa client list` | 列出客户端与在线状态、agent 版本、刷新能力 |
| `/quotanoa client show <名称>` | 查看详情（密钥脱敏） |
| `/quotanoa client key <名称> [--rotate]` | 查看 / 轮换密钥 |
| `/quotanoa client remove <名称> --yes` | 删除客户端实例 |
| `/quotanoa --client <名称>` | 查询该客户端全部渠道（`--client all` 查全部在线客户端） |
| `/quotanoa --all` | 本地渠道 + 全部 CPA 平台 + 全部在线客户端 |
| `/cpa codex refresh <查询词> --client <名称>` | 远程消耗 1 次 Codex 重置次数 |

### 刷新双重门禁

- 客户端本地配置 `refresh.enabled` 默认 **false**；握手时向服务端上报 `capabilities.refresh`。
- 服务端仅在「`clients` 中该客户端 `allow_refresh=true`」且「会话上报允许」时才发起 `codex.refresh`。
- 即使服务端伪造状态发起请求，客户端也以**本地配置为准**直接拒绝，且零网络副作用。

公网部署建议：`server.host` 保持 `127.0.0.1`，用 Caddy/Nginx 提供 TLS；客户端使用 `wss://`，每个客户端独立密钥。

## 命令

仅超级用户 / `cpa.admins` 可用。额度查询用 `/quotanoa`（必须带指令头 `/`）；CPA 管理用 `/cpa`（前缀可有可无）。

### 额度查询 `/quotanoa`

| 命令 | 作用 |
| --- | --- |
| `/quotanoa` | **默认**先查本地渠道（火山 / WorkBuddy / Qoder），再追加 `quotanoa_additional_channel` 里的渠道；多实例时 CPA 结果按 `[实例名]` 前缀区分 |
| `/quotanoa all` | 查询**全部渠道**：本地渠道 + 全部 CPA 平台（同义 `--all` / `-a`，与 `/cpa quota all` 内容一致） |
| `/quotanoa help` | 查看帮助图（同义 `--help` / `-h`；加 `--text` 只发文字）。帮助图主题跟随 `render.theme`（`/quotanoa theme`），渲染失败自动回退文字（原因只写终端日志，不在聊天里提示） |
| `/quotanoa <平台>` | 只看一个平台：`claude` / `codex`(gpt, openai) / `antigravity`(反重力, agy) / `kimi` / `xai` / `workbuddy`(wb) / `qoder` |
| `/quotanoa 火山` | 查询火山方舟 Coding Plan + Agent Plan 额度（档位、用量、订阅到期；同义：`volc` / `volcengine` / `ark` / `火山方舟`） |
| `/quotanoa workbuddy` | 查询全部 WorkBuddy 网关的积分额度（同义：`wb`） |
| `/quotanoa qoder` | 查询全部 Qoder2OAPI 代理的号池额度（同义：`qd`） |
| `/quotanoa <实例>` | 只查指定 CPA 实例。例：`/quotanoa Home` |
| `/quotanoa <平台> <实例>` | 例：`/quotanoa antigravity Home` 或 `/quotanoa Home antigravity` |
| `/quotanoa <查询词>` | 单个账号的额度卡（跨全部实例搜索） |
| `/quotanoa --fresh` | 忽略缓存，强制重查 |
| `/quotanoa --text` | 只发文字总览（排障 / 无浏览器时） |
| `/quotanoa --instance <实例>` | 显式指定实例，避免与平台名冲突 |
| `/quotanoa cooling` | 只看冷却中的凭证（全部实例） |
| `/quotanoa reset <查询词>` | `POST /reset-quota`（使用完整 `auth_index`，跨实例搜索） |
| `/quotanoa volc list` | 列出火山方舟账号 |
| `/quotanoa volc add <名称> <AK> <SK> [region]` | 新增火山方舟账号（写入配置） |
| `/quotanoa volc remove <名称> --yes` | 删除火山方舟账号 |
| `/quotanoa wb list` | 列出 WorkBuddy 网关 |
| `/quotanoa wb add <名称> <base_url> --user U --pass P [--timeout N]` | 新增 WorkBuddy 网关（用控制台账号密码；也可 `--key` 直连）。写入配置 |
| `/quotanoa wb login <名称>` | 校验账号密码并刷新会话 |
| `/quotanoa wb remove <名称> --yes` | 删除 WorkBuddy 网关 |
| `/quotanoa qoder list` | 列出 Qoder2OAPI 代理 |
| `/quotanoa qoder add <名称> <base_url> --key <API_KEY> [--timeout N]` | 新增 Qoder2OAPI 代理（写入配置） |
| `/quotanoa qoder remove <名称> --yes` | 删除 Qoder2OAPI 代理 |
| `/quotanoa alias list [--disabled]` | 列出账号显示别名（分渠道） |
| `/quotanoa alias set <渠道> <查询词> <别名>` | 为指定渠道账号设置别名 |
| `/quotanoa alias del <查询词>` | 删除别名（跨渠道全部删除） |
| `/quotanoa theme [set <主题>]` | 查看 / 设置额度图主题（`default` / `mac` / `md3` / `winxp` / `win7`） |
| `/quotanoa card [row <1..6>] [max <数量>]` | 查看每行卡片数与每渠道上限 / 设置每行卡片数（1..6）/ 设置每渠道卡片上限（如 `max 40`） |
| `/quotanoa config show` | 查看当前生效配置（密钥脱敏）与最近解析错误 |
| `/quotanoa config reload` | 强制从磁盘重载配置 |
| `/quotanoa config fix` | 修补配置文件：按内置默认补齐缺失的设置项（不覆盖已有值），写回前先把旧文件备份到 `data/backup/quotanoa_config_<日期>-<时间>_bak.json`（备份目录常量在 `plugins/QuotaNoa/config.py` 的 `DEFAULT_BACKUP_DIR`）。配置已完整时不做任何写盘 |

### CPA 管理 `/cpa`

除登录回调外，所有子命令都要在第一个位置写 CPA 实例名。裸 `/cpa` 显示帮助图（加 `--text` 只发文字）。

| 命令 | 作用 |
| --- | --- |
| `cpa instance list` | 列出已配置实例 |
| `cpa instance add <名称> <base_url> [--key K] [--timeout N] [--quota-timeout N] [--concurrency N] [--cache-ttl N] [--no-image]` | 新增实例（写入配置） |
| `cpa instance show <名称>` | 查看实例详情（密钥脱敏） |
| `cpa instance remove <名称> --yes` | 删除实例 |
| `cpa status <实例>` | 探活：版本头、凭证 ready / 禁用 / 冷却计数。不回传配置正文 |
| `cpa auth list <实例> [provider] [--disabled]` | 凭证摘要。默认隐藏已禁用账号 |
| `cpa auth show <实例> <查询词>` | 单条详情与近期请求桶 |
| `cpa auth on\|off <实例> <查询词>` | 启用 / 禁用（`enable` / `disable` 同义） |
| `cpa auth models <实例> <查询词>` | 该凭证支持的模型 |
| `cpa auth delete <实例> <查询词> --yes` | 删除磁盘凭证；无 `--yes` 只预告 |
| `cpa codex refresh <实例> <查询词>` | 消耗 1 次 Codex 官方重置次数并刷新额度。仅 `cpa.codex_refresh_admin` |
| `cpa login <实例> <渠道>` | 启动 OAuth / 设备码。授权完成后把浏览器回调链接发回聊天（自动归属该实例） |
| `cpa login <实例> callback <回调链接>` | 手动提交 localhost 回调 URL |
| `cpa login <实例> cancel` | 取消当前登录 |
| `cpa quota [平台] [实例] [--all] [--instance <实例>] [--fresh] [--text]` | 与 `/quotanoa` 同义：默认先查**全部 CPA 平台**，再追加 `cpa_additional_channel` 里的渠道（如 `qoder` / `workbuddy`）；`--all` 查询全部渠道，`cpa quota help` 查看帮助。例：`cpa quota xai JP-AI` 只查 JP-AI 的 xAI 额度 |

查询词可以是 email、文件名、label、别名或 `auth_index`（含前缀）。列表和额度图优先显示别名；未设别名时用 `渠道-短索引`，避免把邮箱发到聊天。同邮箱出现在多个渠道时用 `/quotanoa alias set antigravity user@example.com AG-1`。WorkBuddy 账号也可绑别名：`/quotanoa alias set workbuddy <uid> <别名>`。详情 `cpa auth show` 仍会列出原始字段，便于对照。

别名文件 `data/quotanoa_aliases.json` **首次运行自动生成**（含用例注释与各渠道空桶，直接编辑即可），并**支持热重载**：手改保存后下一条 `/quotanoa` 即生效，无需重启；文件被删除会自动重建。它还支持**自定义渠道查询关键字**（保留键 `channel_keywords`，仅手改 JSON，不加命令、不影响出图/文本）：

```json
{
  "_readme": [ "账号别名：渠道 → 身份键 → 显示名。保存后自动热重载。" ],
  "channel_keywords": { "agy": "antigravity" },
  "antigravity": { "user@example.com": "AG-1" }
}
```

写入后 `/quotanoa agy` 等同 `/quotanoa antigravity`。渠道名必须能归一到内置渠道（`claude` / `codex` / `antigravity` / `kimi` / `xai` / `gemini-cli` / `volcengine` / `workbuddy` / `qoder`）。

WorkBuddy 卡片/文字里的倒计时是**重置**语义，固定显示为 `最早的(空)套餐 XdXh 后过期`，表示该账号所有周期套餐中**最先到期**的那个还剩多久；其进度条百分比则是**所有套餐聚合**的剩余比例，两者口径不同。

内置登录渠道：`claude` / `anthropic`、`codex`、`antigravity`、`kimi`、`xai`。若 CPA 插件声明了 `supports_oauth`，还会动态发现 `/{provider}-auth-url`。不要写死已从 core 移除的 `gemini-cli` / `qwen` / `iflow`。

## 额度说明

CLIProxyAPI **没有**账号池额度聚合接口。`GET /auth-files` 只有健康 / 冷却状态。`/quotanoa` 的 CPA 部分和管理台 Quota 页同一思路：按 `provider` 分组后，用内部白名单 `POST /v0/management/api-call` 打各平台用量接口（`$TOKEN$` 由 CPA 替换）。聊天里**不会**开放通用代发。火山方舟部分则直接用控制面 OpenAPI（SigV4 签名）查询 `GetCodingPlanUsage` 与 `GetAFPUsage`。

默认跳过 `disabled` 凭证，与管理台「8 个文件 / 6 个参与额度」一致。

**合计不是百分比相加。** `86% + 91%` 不会写成 `177%`。每个窗口先换成剩余比例（0–1），再按账号求和：

```text
【Antigravity】4 账号
  合计：Gemini 5h 3.44/4 (86%) · Gemini 周 3.44/4 (86%) · Claude/GPT 周 1.64/4 (41%)
  account-a (Pro)  Gemini 5h 剩 86% →19m · Gemini 周 剩 86% →18h
```

`3.44/4 (86%)` 表示：该窗口剩余当量 3.44 个满额号，4 个账号均剩 86%。1.00 = 满额一个号。

默认用 Playwright 把同一平台的账号卡合并成图片发送（视觉对齐管理台 Quota 页，不含 Refresh / 时间轴；默认 3 列 × 3 行网格排版，首图含概览卡容纳 8 个账号，后续每图 9 个账号，超过时自动拆成多张图片；每渠道账号卡片数量受 `render.max_cards_per_channel` 限制，默认最多展示 40 张卡片，超出部分在概览卡显示 `仅显示` 提示）。发送图片时不附加前置文字标题。出图函数 `render_platform_images` / `render_board_images` 不依赖聊天会话，以后做定时推送可以直接复用。

开启 `onebot-v11-feature.forward-message` 且当前为 OneBot V11 会话时，上述拆出来的多条「标题 + 图」结果会再合并成**一条合并转发**发送；`pin-channel` 决定的渠道顺序在合并转发里同样生效。

多实例下，账号卡标题与文字总览都会带 **`[CPA 实例名]` 前缀**（如 `[JP-AI] Murasame…`），便于区分额度来自哪个实例；实例标签限长 8 字符、账号名限长 16 字符，超长以 `…` 截断，完整名称保留在悬浮提示里。火山方舟账号为本地渠道（无 CPA 实例），不加前缀；若同时开通 Coding 与 Agent Plan，会拆分为**两张独立卡片**（Coding 卡片与 Agent 卡片），各自带有专属档位徽章（如 `Coding Lite`、`Agent Small`）与订阅到期徽章（`Coding 到期 …`、`Agent 到期 …`）。无论在账号卡片还是汇总/合计视图中，均统一以套餐名作为分组标题（Coding 在前、Agent 在后），内部额度行显示简洁的 `5h`/`周`/`月`（Agent 视用量自适应展示 `日`）；文字模式下合计行按 `Coding：`/`Agent：` 分组缩进展示。其中 Agent Plan 的日额度为视觉模型专用硬顶，无消耗时自动隐藏，产生用量后才浮现。火山方舟的 **5h 小时额度**为「重置后首次调用才计时」的滚动窗口，官方 `ResetTimestamp` 的倒计时在未调用时并不准确，故该窗口的重置提示固定显示为 **`将会在首次调用后进行重置计时`**（周/月窗口不受影响，仍显示剩余倒计时）。

## 主题资源包

所有额度图主题均存放在独立资源目录中，渲染器会自动扫描：

```text
plugins/QuotaNoa/render/assets/
├─ quota.html
├─ base.css
├─ help.css
├─ brands/
└─ themes/
   ├─ default/
   ├─ mac/
   ├─ md3/
   ├─ winxp/
   └─ win7/
```

每个主题目录包含：

```text
themes/<主题名>/
├─ theme.json
├─ theme.css
├─ wrapper.html
└─ SOURCES.md
```

- `theme.json`：主题名称、显示名称、根 CSS class、可选别名和说明。
- `theme.css`：该主题独有的视觉样式；公共网格、卡片和额度组件样式位于 `base.css`。
- `wrapper.html`：主题窗口外壳，只能使用 `__TITLE__`、`__GRID__`、`__PAGE_NOTE__` 三个占位符，其中网格和分页占位符必须存在。
- `SOURCES.md`：素材来源、许可证与商标声明。

新增主题时只需复制一个现有目录、修改目录名及上述四个文件，然后重启 Bot。主题目录名必须与 `theme.json` 中的 `name` 相同，并使用小写字母、数字、下划线或连字符。无需修改 Python 注册表或命令代码。运行时 CSS 和 wrapper 禁止脚本、事件处理器、`@import` 和远程 HTTP(S) 资源。

默认主题的 canonical 名称为 `default`。旧配置中的 `"theme": "shadcn"` 会自动兼容并解析为 `default`。主题和卡片布局保存在 `data/quotanoa_config.json` 的 `render` 段（默认 `cards_per_row` 为 3、`max_cards_per_channel` 为 40，可用 `/quotanoa theme`、`/quotanoa card row`、`/quotanoa card max` 修改），不使用主题相关环境变量。

未安装 Chromium 时会自动回退文字，并提示执行 `playwright install chromium`（推荐：`uv run playwright install chromium`）。`cpa.quota_image=false` 或 `/quotanoa --text` 可强制只要文字。出图失败时聊天里静默回退为文字总览，失败原因只写终端日志。

### 帮助图与字段高亮

`/quotanoa help`（`--help` / `-h`）与裸 `/cpa` 默认把帮助排版成单张图片发送（无前置文字标题），沿用**同一套主题资源**（`base.css` + 各主题 `theme.css` + `help.css`），因此 `/quotanoa theme set mac` 之后帮助图也是 mac 风格。渲染失败（未装 Chromium 等）自动回退纯文字帮助（失败原因只写终端日志，不在聊天里提示），加 `--text`（如 `/quotanoa help --text`、`/cpa --text`）可强制只要文字。发送额度卡图片时，**每个渠道会先发一条汇总文字**（如「Claude 额度 共 12 个账号 分 2 张图片显示」），随后是该渠道的图片；帮助图不带前置文字。

帮助图与额度卡错误框共用一套**可复用字段高亮**（`plugins/QuotaNoa/render/highlight.py`）：把命令（`cmd`）、占位参数（`arg`）、开关（`opt`）、配置键（`key`）、告警（`warn`）在文本里自动标出。任意文本一行调用 `highlight_html(text)` 即可套用（输出已转义），配色由各主题的 `--hl-*` 变量决定。

支持的上游：Claude OAuth usage、Codex WHAM usage、Antigravity `retrieveUserQuotaSummary` + `loadCodeAssist`（套餐）、Kimi usages、xAI billing credits、**火山方舟 Coding Plan / Agent Plan**（控制面 `GetCodingPlanUsage` 与 `GetAFPUsage`）、**WorkBuddy2API**（`GET /v1/quota`，聚合积分 + 套餐数）、**Qoder2OAPI**（`GET /v1/dashboard/billing/credits`，号池聚合 + 每账号 general/addon/dedicated 分桶；账号 `user_type` 会映射为订阅档位：个人 = 体验版 / 专业版 / 高级版 / 旗舰版，企业 = 团队版 / 企业标准版，兼容 `personal_professional` 与 `PLAN_TIER_*` / `ORGANIZATION_PLAN_TIER_*` 两种写法）。Antigravity 的 `account_type=oauth` 只是登录方式，套餐来自 `paidTier`（Pro / Plus / Ultra）。未知 / API-key 渠道只显示本地健康状态。

火山方舟凭据请用**控制面 OpenAPI** 的 AccessKey ID + SecretAccessKey（`volcengine.accounts[]`，需子账户具备 `ArkReadOnlyAccess` 权限），**不是**推理 Key（`ark-...` 查不了额度）。Bot 直接对 `open.volcengineapi.com` 做 SigV4 签名请求。

不要用 `GET /usage-queue` 当「查用量」——它会把记录从队列里弹出，会和 WebUI / Redis `LPOP` 抢数据。全量刷新会打上游，群里连刷请用缓存或 `/quotanoa antigravity` 只查一个平台。

## OAuth 注意

- 机器人**不会**加 `is_webui=true`。该参数会在 CPA 本机 `51121` 起 callback，聊天场景通常不可达。
- 授权链接 / 设备码优先私聊下发；私聊失败才回当前会话并警告。
- 浏览器常会跳到 `localhost`。把地址栏完整回调链接发回当前聊天，或 `cpa login <实例> callback <url>`。插件会 `POST /oauth-callback`（`redirect_url`）转给 CPA。
- Session 约 30 分钟；超时或 `cpa login <实例> cancel` 会 `DELETE /oauth-session`。

## 刻意不暴露的接口

聊天里不会做这些操作（避免把管理密钥能力扩成任意写配置 / 泄密）：

- 整份 `GET /config`、`config.yaml` 读写
- 上传 / 下载 auth JSON、Vertex import
- 通用 `/api-call` 代发（额度巡检只用白名单 URL，且不把上游 body 回传到聊天）
- `/usage-queue` 出队查询
- 插件商店安装（会下载可执行文件）

## 鉴权失败

管理密钥错误时 CPA 会返回 401，插件直接把错误转达给管理员，**不做本地暂停/冷却**：修好 `management_key` 后下一条命令即可生效。若 CPA 与 Bot 不在同一台机器，403 通常表示需要在 CPA 侧开启 `remote-management.allow-remote`（或设置 `MANAGEMENT_PASSWORD`）。
