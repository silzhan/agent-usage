# agent-usage — AI CLI Token 用量看板

多 AI 编程助手（Qoder CLI 国际/国内、Claude Code、CodeBuddy、Cline CLI/插件、Reasonix、Codex、Gemini、OpenCode）的本地 token 用量统计与可视化。

Qoder CLI 本地记录的 usage 字段全为 0，本工具通过扫描会话转录里的字符量来**估算** token 消耗，
覆盖两个 CLI 目录：

- 国际版：`~\.qoder\projects`（qoder.sh）
- 国内版：`~\.qoder-cn\projects`（qoder.com.cn）

## 使用

双击 `start.bat`，或：

```
python server.py [端口]     # 默认 8765
```

打开 http://127.0.0.1:8765/ 。首次扫描约需 1 分钟，之后缓存 10 分钟；
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

- **模型名归一化**（`collect.ps1` 的 `NormModel`、`collect-ccswitch.py` 的 `norm_model`、`index.html` 的 `normModel` 三份实现必须逐字一致，已用 58 条向量交叉验证零差异）：
  转小写 → 取最后一个 `/` 之后的部分（`anthropic/claude-3-5-sonnet-20241022` → `claude-3-5-sonnet`）
  → 去尾部日期 `-([a-z]{1,4})?-[0-9]{4,8}`（`gpt-4o-20250801` → `gpt-4o`、`deepseek-v4-flash-0731` → `deepseek-v4-flash`）
  → 去尾部分隔符；空值和 Claude 的 `<synthetic>` 都落到 `unknown`。
  不做别名合并（`k3` 与 `kimi-k3` 各自成条），因为猜错别名会把两个真实模型并成一条假数据。
- **Top-7 + 其他**：按输入 token 取前 7 个模型，其余并入 `其他`，正好用完现有 8 个配色；`其他` 可能混合真实值与估算，标记为"混合"。
  切标签页或改日期区间会重算 Top-7（排序带模型名 tie-break，保证图例、图表、表格选出的那 7 个一致）。
- **成本只来自 cc-switch**：其它来源没有可靠单价，一律不估价。所以 `真实值` ≠ 一定有 `$`——本地转录有真实 token 但无金额，
  只有 cc-switch 覆盖的模型才带 `total_cost_usd`。cc-switch 的聚合止于清理前最后一天，若日期区间没覆盖到它，模型视图不会出现任何 `$`。
- 采集状态文件升到 `version 6`，首次运行会全量重扫一次（约 20 秒 / 数百文件），之后仍是约 1 秒的增量扫描。

## 文件

| 文件 | 说明 |
|---|---|
| `collect.ps1` | 扫描转录文件（Qoder 国际/国内 + Claude Code 本地），输出 JSON |
| `collect-ccswitch.py` | 读取 cc-switch SQLite 数据库的日聚合用量（真实 token + 美元成本） |
| `server.py` | 本地 Web 服务（仅 Python 标准库），合并上述两个采集源并去重 |
| `index.html` | 看板页面（原生 SVG 图表，无 CDN 依赖） |
| `cache.json` | 最近一次扫描结果缓存（自动生成） |
| `files-state.json` | 增量扫描状态（自动生成） |

## 数据源说明

- **Qoder CLI**（`.qoder` / `.qoder-cn`）：本地 usage 字段全为 0，字符估算（≈0.32 token/字符；内置模型 200K / BYOK `qoder-custom-*` 300K 窗口封顶）
- **Claude Code 本地转录**（`.claude`）：真实 usage token；但 Claude Code 默认自动清理 30 天前的会话，历史会丢
- **CodeBuddy 本地转录**（`.codebuddy`）：真实 usage（input_tokens/output_tokens，毫秒时间戳）
- **Cline CLI**（`.cline/data/sessions/*.messages.json`）：每条消息带 `metrics.{inputTokens, outputTokens, cacheReadTokens}` + 毫秒时间戳；另外 `.cline/data/db/sessions.db` 的 `metadata_json` 存每会话聚合 usage（可交叉验证）
- **Cline VSCode 插件**（`%APPDATA%\Code\User\globalStorage\saoudrizwan.claude-dev\tasks`）：每任务的 `api_conversation_history.json`，assistant 条目带 `metrics.tokens`（prompt/completion/cached）——注意这份数据是旧的（3-4 月），默认 30 天窗口内看不到
- **Reasonix**（`%APPDATA%\reasonix\stats`）：逐请求 stats（prompt/completion/cache_hit/cache_miss，带时区 ISO 时间戳）
- **cc-switch 汇总**（`.cc-switch/cc-switch.db`）：cc-switch 在清理前预聚合的真实日用量，含 USD 成本，覆盖 Claude / Codex / Gemini / OpenCode——Claude Code 丢失的历史靠它补回；与本地转录重叠日期由 server.py 自动去重

