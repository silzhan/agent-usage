# agent-usage — AI CLI Token 用量看板

多 AI 编程助手（Qoder CLI 国际/国内、Qoder 桌面端、Claude Code、CodeBuddy、WorkBuddy、Cline CLI/插件、Reasonix、Codex、Gemini、OpenCode、ZCode）的本地 token 用量统计与可视化。

Qoder CLI 本地记录的 usage 字段全为 0，本工具通过扫描会话转录里的字符量来**估算** token 消耗，
覆盖两个 CLI 目录：

- 国际版：`~\.qoder\projects`（qoder.sh）
- 国内版：`~\.qoder-cn\projects`（qoder.com.cn）

## 使用

双击 `start.bat`，或：

```
python server.py [端口]     # 默认 8765
```

打开 http://127.0.0.1:8765/ 。首次扫描约需 1 分钟，之后缓存 1 分钟；
页面右上角"重新扫描"可强制刷新。

也可以单独跑命令行版：

```
powershell -NoProfile -ExecutionPolicy Bypass -File collect.ps1
```

输出 JSON（总量 + 按天分布）。

## 估算口径

- 每个 assistant 请求按"重发全部前文上下文"计（与真实计费口径一致），单请求上下文封顶 60 万字符（≈200K token 窗口）
- 字符 → token 系数 0.32（中英混合代码场景约 3 字符/token），可在 `collect.ps1` 顶部调整 `$conv` / `$CAP`
- 日期按 UTC 日切分（北京时间会晚 8 小时跨天）
- 结果为估算：量级与分布可信，精确值以 Qoder 服务端为准

## 按模型统计

工具栏的 **按来源 / 按模型** 切换复用同一套卡片、堆叠图、图例和明细表，只是把聚合轴从"来源"换成"模型"：
把各来源每天的 `byModel` 跨来源合并成一个虚拟 root（同名模型在 Qoder / Claude Code / cc-switch 里出现几次都归一条）。

- **模型名归一化**（`collect.ps1` 的 `NormModel`、`collect-ccswitch.py` 和 `collect-sqlite.py` 的 `norm_model`、`index.html` 的 `normModel` 四份实现必须逐字一致，已用 58 条向量交叉验证零差异）：
  转小写 → 取最后一个 `/` 之后的部分（`anthropic/claude-3-5-sonnet-20241022` → `claude-3-5-sonnet`）
  → 去尾部日期 `-([a-z]{1,4})?-[0-9]{4,8}`（`gpt-4o-20250801` → `gpt-4o`、`deepseek-v4-flash-0731` → `deepseek-v4-flash`）
  → 去尾部分隔符；空值和 Claude 的 `<synthetic>` 都落到 `unknown`。
  不做别名合并（`k3` 与 `kimi-k3` 各自成条），因为猜错别名会把两个真实模型并成一条假数据。
- **Top-7 + 其他**：按输入 token 取前 7 个模型，其余并入 `其他`，正好用完现有 8 个配色；`其他` 可能混合真实值与估算，标记为"混合"。
  切标签页或改日期区间会重算 Top-7（排序带模型名 tie-break，保证图例、图表、表格选出的那 7 个一致）。
- **成本只来自 cc-switch**：其它来源没有可靠单价，一律不估价。所以 `真实值` ≠ 一定有 `$`——本地转录有真实 token 但无金额，
  只有 cc-switch 覆盖的模型才带 `total_cost_usd`。cc-switch 的聚合止于清理前最后一天，若日期区间没覆盖到它，模型视图不会出现任何 `$`。
- 采集状态文件升到 `version 7`，首次运行会全量重扫一次（约 20 秒 / 数百文件），之后仍是约 1 秒的增量扫描。

### Qoder 模型别名 → 产品名

转录里的 `"model"` **只记选择器别名**，不是模型名：`efficient` 39,848 次、`qmodel_preview` 5,021 次、
`lite` 3,951 次、`qmodel_latest` 3,574 次、`qfmodel` 2,627 次、`gfmodel` 2,329 次……
真名也不在 `~\.qoder\.models\<uid>\catalog-v6` 里（那份是加密的：base64 解出 67,445 字节，无 gzip/zlib 头）。

可用的明文来源是 **`~\.qoder{,-cn}\.auth\dynamic-texts.json`**——模型选择器的界面文案，
路径 `locales.<en|zh-CN>["modelSelector.item.<别名>"]`。`server.py` 读它并随 payload 返回 `modelNames`，
`index.html` 在渲染时映射，**不进缓存、不改 `$StateVersion`、不需要重扫**。

- 两份 bundle 的 namespace 不同（`qoder-ide` 1.0.322 / `qoder-cli` 1.0.9），取并集 32 条；冲突按文件 mtime 新的赢
  （`mmodel`：MiniMax-M3 vs MiniMax-M2.7）。优先用 `en`，`zh-CN` 只在档位上不同（经济/轻量/极致）。
- **按产品名分桶会合并同一模型**：cc-switch 记 `glm-5.3-flash`、Qoder 转录记 `gfmodel`，映射后是同一个模型，
  合成一行（实测 424,374,880 = 297,353,704 + 127,021,176）。全量数据里共 4 组会合并：
  `glm-5.3-flash`、`glm-5.2`、`deepseek-v4-pro`、`deepseek-flash`。图例和卡片用浅色标出贡献了哪些别名。
- ⚠️ **档位不是模型**：`efficient` / `lite` / `ultimate` / `auto` / `performance` / `fast` 是"经济/轻量/极致/自动"档位，
  服务端每次可能路由到不同底座，还原不出具体型号。`efficient` 恰是最大头（39,848 次、6.02B 输入），
  这部分永远只能是档位名。
- ⚠️ 这是**产品对外名称**，不等于 provider 上游型号；`modelSelector.item.<别名>.description` 里偶尔带日期，比标题更准
  （`dmodel` → 描述写 DeepSeek-V4-Pro-0813，`qmodel_38max` → Qwen3.8-Max-0902）。

## 文件

| 文件 | 说明 |
|---|---|
| `collect.ps1` | 扫描转录文件（Qoder 国际/国内 + Claude Code 本地 + CodeBuddy / WorkBuddy / Cline / Reasonix），输出 JSON |
| `collect-ccswitch.py` | 读取 cc-switch SQLite 数据库的日聚合用量（真实 token + 美元成本） |
| `collect-sqlite.py` | 读取 SQLite 直存的来源（ZCode `~\.zcode\cli\db\db.sqlite`、Qoder 桌面端 `%APPDATA%\Qoder\...\local.db`），全部以 `mode=ro` 打开 |
| `server.py` | 本地 Web 服务（仅 Python 标准库），合并上述三个采集源并去重 |
| `index.html` | 看板页面（原生 SVG 图表，无 CDN 依赖） |
| `cache.json` | 最近一次扫描结果缓存（自动生成） |
| `files-state.json` | 增量扫描状态（自动生成） |

## 数据源说明

- **Qoder CLI**（`.qoder` / `.qoder-cn`）：本地 usage 字段全为 0，字符估算（≈0.32 token/字符；内置模型 200K / BYOK `qoder-custom-*` 300K 窗口封顶）
- **Claude Code 本地转录**（`.claude`）：真实 usage token；但 Claude Code 默认自动清理 30 天前的会话，历史会丢
- **CodeBuddy 本地转录**（`.codebuddy`）：真实 usage（`providerData.usage.inputTokens/outputTokens`，毫秒时间戳）
- **WorkBuddy 本地转录**（`.workbuddy`）：与 CodeBuddy 同一套记录结构，走 `collect.ps1` 里同一个分支。
  ⚠️ 记录里的 `"credit":0.31` 是**额度单位、不是美元**（本机 132 条合计 403.46），本工具不解析它，所以 WorkBuddy 不显示 `$`
- **ZCode**（`~\.zcode\cli\db\db.sqlite` 的 `model_usage` 表）：真实 token，按 `started_at` 毫秒时间戳切 UTC 日。
  只取 `model_usage`，**排除 `selection_side_chat`（划词侧边对话）**——那是临时问答、不是会话主线用量。
  同一份数据在 `~\.zcode\cli\projects` 的 JSONL 里也有，但那份是残缺的，所以以 SQLite 为准
- **Qoder 桌面端**（`%APPDATA%\Qoder\SharedClientCache\cache\db\local.db` 的 `chat_message` 表）：
  取 `role='assistant'` 行的 `token_info` JSON（`prompt_tokens` / `completion_tokens`），模型名在 `model_info`。
  注意这台机器上这份数据**停在 2026-06-03**，默认 30 天窗口内看不到，把日期范围放宽才有数字
- **cc-switch 汇总**（`.cc-switch/cc-switch.db`）：cc-switch 在清理前预聚合的真实日用量，含 USD 成本，覆盖 Claude / Codex / Gemini / OpenCode——Claude Code 丢失的历史靠它补回；与本地转录重叠日期由 server.py 自动去重
- **Trae 系（Trae / Trae CN / TRAE SOLO CN）：不支持**（2026-10-13 实测，三类存储全扫过）。
  - **会话与用量都在 SQLCipher 加密库**：`%APPDATA%\TRAE SOLO CN\ModularData\ai-agent\database.db`（61 MB，2026-10-10 还在写）、
    `ModularData\ckg_server\env_codekg.db`、`uno_packages.db`、`Local Storage\config.db`
    —— 文件头都不是 `SQLite format 3`，明文打不开。当前在用的是 **TRAE SOLO CN**（近 90 天 4466 个文件有写入），
    `Trae CN` 停在 2026-05-15，国际版 `Trae` 的主库停在 2026-04-27。
  - **明文 Electron 存储里一条消息都没有**：`Local Storage`（3.6 MB）、`Session Storage`（47 KB）、`IndexedDB`（6 个文件 2979 字节）
    按 `"role":"assistant|user|system|tool"` 扫，命中 **0**；220 个 leveldb 文件按 `prompt_tokens` / `inputTokens` 等键名扫，命中也是 **0**。
    所以**连字符估算都做不了**——没有可读的对话文本可估。
  - 唯一带 role 的明文对话在**国际版老 Trae** 的 `User\workspaceStorage\*\state.vscdb`（`memento/icube-ai-agent-storage*`、
    `icube-ai-ng-chat-storage*`，含模型名与毫秒时间戳），共 230 条消息，但日期全在 **2025-02-25 .. 2025-08-08**，
    且带数字后缀的 key 是同一份数据的迁移副本（求和会翻倍）。加进来看板 = 一根 14 个月前的柱子，默认 30 天窗口里永远是空的。
  - 日志侧只剩 `%APPDATA%\Trae CN\logs\*\Modular\ai-agent_*_stdout.log` 里的 `TimingCost` 事件（有模型名、无 token），
    全机器只留着 **2026-03-27 的 3 条**。
  - 结论：看板里没有 Trae 标签页，是数据不可读，不是漏配。
- **Cline CLI**（`.cline/data/sessions/*.messages.json`）：每条消息带 `metrics.{inputTokens, outputTokens, cacheReadTokens}` + 毫秒时间戳；另外 `.cline/data/db/sessions.db` 的 `metadata_json` 存每会话聚合 usage（可交叉验证）
- **Cline VSCode 插件**（`%APPDATA%\Code\User\globalStorage\saoudrizwan.claude-dev\tasks`）：每任务的 `api_conversation_history.json`，assistant 条目带 `metrics.tokens`（prompt/completion/cached）——注意这份数据是旧的（3-4 月），默认 30 天窗口内看不到
- **Reasonix**（`%APPDATA%\reasonix\stats`）：逐请求 stats（prompt/completion/cache_hit/cache_miss，带时区 ISO 时间戳）

### 采集器报错不再静默

`collect-ccswitch.py` / `collect-sqlite.py` 抛异常时，`server.py` 以前只 `print` 到控制台并塞回空 `roots[]`，
表现就是**标签页莫名少几个**（真实事故：SQL 里选了不存在的 `request_model` 列，Codex/Gemini/OpenCode 三个标签页长期消失）。
现在错误会随 `/api/usage` 的 `collectorErrors` 返回，并显示在看板右上角状态里。

## 历史问题

### CodeBuddy 曾少算约 21 倍（2026-10-13 已修复）

旧判据要求行内含 `"role":"assistant"`，但 CodeBuddy 把工具调用轮次写成 `"type":"function_call"`、
**没有 role 字段**，而这类记录恰好是大头。2026-10-13 对 `~/.codebuddy/projects`（6 个 jsonl、6.2 MB）逐行实测：

| | 请求 | 输入 token | 输出 token |
|---|---|---|---|
| 修复前 | 24 | 2.83M | 26.5K |
| 真实 | **430** | **59.19M** | **283K** |

现行口径（`collect.ps1`，随 `$StateVersion` 7 全量重扫）：

```powershell
# 判据只看 usage 键在不在，不要用 role
$cbIn  = [regex]::Match($line, '"inputTokens":(\d+)')
$cbOut = [regex]::Match($line, '"outputTokens":(\d+)')
# 模型名锚定，绕开 } 跨界：providerData 里 model 紧跟 requestModelId
if ($line -match '"model":"([^"]+)","requestModelId"') { $rawM = $matches[1] }
# 日期仍是 "timestamp":(\d{13}) 毫秒 -> UTC 日
```

实测依据（430/430 命中，看板合计与逐行累加分毫不差）：

- token 取 `providerData.usage` 的**驼峰**键；同一行里 `message.usage.input_tokens`（蛇形）和
  `providerData.rawUsage.prompt_tokens`（provider 原生）是同一数值的三份冗余（430/430 相等），**只能取一份，不要相加**。
- `inputTokens` **已包含缓存命中**（`inputTokensDetails[].cached_tokens` 恒 ≤ `inputTokens`，本批 58.80M / 59.19M 命中），
  所以不像 Claude 分支那样需要再叠加 `cache_read` / `cache_creation`。
- 每行恰好一条 usage；430 条按顶层 `id` 全唯一，按 `providerData.conversationRequestId + messageId` 也全唯一。
  这两个 id 字段在 **`providerData` 里、不在顶层**，`(sessionId, conversationRequestId, messageId)` 顶层取法会全部取到 `null`。
- 430 条只落在 `function_call`(406) 和 `message`(24) 两类行上；`function_call_result` / `reasoning` / `summary` 不带 usage 键，天然被跳过。
- 每条 usage 记录 `providerData.usage.requests` 恒为 1，逐条累加不会重复计数。
- ⚠️ **别写成 `$line -match 'A' -and $line -match 'B'` 再取 `$matches[1]/$matches[2]`**：第二个 `-match` 会覆盖 `$matches`，
  于是 `outputTokens` 灌进输入侧、`$matches[2]` 不存在使输出恒为 0。用 `[regex]::Match` 各取各的。


