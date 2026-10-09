# 矿权日报 Agent — 运行手册

一个由 3 个 MCP server + 1 个 LangGraph Agent 组成的矿权日报工具：

- `mining-news`：Google News RSS + GDELT 新闻检索与正文抓取
- `mineral-pdf`：技术报告 PDF 资源量表解析（JORC / NI 43-101）
- `lme-price`：价格行情（Yahoo / metals-api / mock 可插拔 provider）
- `agent`：规划 → 并行取数 → 分析 → 写作 → 引用校验

离线也能跑：断网且不配 LLM key 时自动使用 `fixtures/` 快照、样例 PDF 和模拟价格，并在报告里诚实标注数据来源。

## 1. 环境要求

- Python 3.11+（推荐 3.12）
- [uv](https://docs.astral.sh/uv/)

验证环境：

```bash
uv run python scripts/check_env.py
```

## 2. 30 秒跑一次

```bash
cd mining-briefing
uv sync
uv run python -m agent.cli "给我生成一份关于 Pilbara 锂矿的今日简报"
```

输出打印到终端，并保存到 `reports/briefing-<日期>.md`。

常用参数：

```bash
# 完全离线：不联网、不调 LLM
uv run python -m agent.cli --offline "给我生成一份关于 Pilbara 锂矿的今日简报"

# 联网取数，但用离线模板写报告
uv run python -m agent.cli --no-llm "给我生成一份关于 Pilbara 锂矿的今日简报"

# 另存完整 state（调试）
uv run python -m agent.cli --json state.json "..."
```

## 3. 接入 LLM（OpenAI 兼容）

复制配置并填写：

```powershell
Copy-Item .env.example .env
```

`.env`：

```env
LLM_BASE_URL=https://api.openai.com/v1
LLM_API_KEY=sk-...
LLM_MODEL=gpt-4o-mini
```

任何 OpenAI Chat Completions 兼容服务都可用：

```env
# DeepSeek
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_MODEL=deepseek-chat

# Qwen / DashScope
LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
LLM_MODEL=qwen-plus

# 本地 Ollama
LLM_BASE_URL=http://localhost:11434/v1
LLM_API_KEY=ollama
LLM_MODEL=qwen2.5:7b
```

不填 `LLM_API_KEY` 时，Agent 自动走离线规则 + 模板，仍然生成完整简报。

## 4. 接入 Claude Desktop / Cursor

### 方式 A：一条命令自动注册（推荐）

```powershell
cd 项目目录
uv sync
uv run python scripts/claude_desktop.py install            # 写入 Claude Desktop 配置（自动备份原文件、保留已有的其它 server）
uv run python scripts/claude_desktop.py check --offline    # 按 Claude Desktop 的方式启动 3 个 server，调用全部 7 个工具
```

然后**完全退出** Claude Desktop（托盘图标右键 → 退出；只关窗口不行），再重新打开。在输入框的 🔨 / 「搜索和工具」里能看到 `mining-news`、`mineral-pdf`、`lme-price` 三个 server。

脚本会自动找到配置文件位置：

| 安装方式 | 配置文件 |
|---|---|
| 官网安装包 | `%APPDATA%\Claude\claude_desktop_config.json` |
| 微软商店（MSIX） | `%LOCALAPPDATA%\Packages\Claude_<id>\LocalCache\Roaming\Claude\claude_desktop_config.json` |
| macOS | `~/Library/Application Support/Claude/claude_desktop_config.json` |

注册的命令直接用项目 `.venv` 里的 `python.exe` 绝对路径：不依赖 PATH 里有没有 `uv`，也不会在 Claude Desktop 启动 server 时再同步依赖（避免启动超时）。想改用 `uv run` 可加 `--uv`。卸载：`uv run python scripts/claude_desktop.py uninstall`。

### 方式 B：手动粘贴

- `mcp-config.json`：`.venv\Scripts\python.exe` 绝对路径版（与方式 A 写入的内容相同）
- `mcp-config.uv.json`：`uv run --directory` 版（需要 Claude Desktop 能在 PATH 里找到 `uv`）

把其中的 `mcpServers` 合并进上表的配置文件（Cursor：`.cursor/mcp.json` 或 设置 → MCP）。两份文件里的路径是开发机路径，项目移动后用 `uv run python scripts/claude_desktop.py print` 重新生成。

### 在 Claude Desktop 里试一试

> 用 lme-price 查一下锂的最新价格和 30 天走势
> 用 mineral-pdf 解析 fixture://pilgangoora 的资源量
> 用 mining-news 搜索最近 7 天 Pilbara lithium 的新闻，并抓取第一篇正文

配置完成后可看到 3 个 server，工具清单：

| Server | 工具 |
|---|---|
| mining-news | `search`、`fetch_article` |
| mineral-pdf | `extract_resources`、`list_reports` |
| lme-price | `get_price`、`get_trend`、`list_commodities` |

## 5. 用 Docker Compose 运行

```bash
docker compose build
docker compose run --rm agent "给我生成一份关于 Pilbara 锂矿的今日简报"
```

该命令启动 3 个 HTTP server 和 agent，报告同时保存到宿主机 `reports/`。server 端口映射为：

- mining-news：`http://localhost:8001/mcp`
- mineral-pdf：`http://localhost:8002/mcp`
- lme-price：`http://localhost:8003/mcp`

只启动 server、用本机 agent 连 HTTP：

```bash
docker compose up news pdf price -d
uv run python -m agent.cli --transport http "..."
```

已在 Docker 29.8.2 / Compose v5.5.1 上实测：`build`、3 个 server 健康检查、`run --rm agent`（在线降级模式与 `MINING_OFFLINE=1` 离线模式）均通过，报告写入宿主机 `reports/`。

拉取 `python:3.12-slim` 失败（如 `failed to fetch oauth token` / 连接超时）通常是访问不了 Docker Hub：在 Docker Desktop → Settings → Docker Engine 中加入 `"registry-mirrors": ["https://docker.m.daocloud.io"]` 等可用镜像源后 Apply & Restart，或在 Resources → Proxies 配置代理。

## 6. 自测

```bash
# 环境 + 依赖 + 关键 SDK 接口
uv run python scripts/check_env.py

# 全量测试（3 个 server 单测 + agent 端到端）
uv run pytest -q

# 按 Claude Desktop 配置原样启动并调用全部 7 个工具
uv run python scripts/claude_desktop.py check

# 分别冒烟测试 3 个 server（stdio）
uv run python scripts/smoke_server.py mining_news
uv run python scripts/smoke_server.py mineral_pdf
uv run python scripts/smoke_server.py lme_price

# HTTP 冒烟测试（先启动对应 server）
uv run python scripts/smoke_server.py lme_price --http http://127.0.0.1:8003/mcp
```

## 7. 故障排查

**`uv` 找不到**：先安装 uv，或把 `mcp-config.json` 里的 `command` 改成系统 Python 路径，并把 `args` 改成 `["-m", "servers.xxx.server"]`（需要已执行 `uv sync` 的虚拟环境）。

**Windows 控制台中文乱码 / UnicodeEncodeError**：CLI 已自动把 stdout/stderr 重配为 UTF-8；仍异常时在终端先执行 `$OutputEncoding = [Console]::OutputEncoding = [Text.UTF8Encoding]::new()`。

**Claude Desktop 里看不到 server / 显示连接失败**：
1. 先跑 `uv run python scripts/claude_desktop.py check`，它读的就是 Claude Desktop 实际使用的配置文件，会告诉你哪一步失败；
2. 确认是**完全退出**后重开的 Claude Desktop；
3. 看日志：配置文件同目录下的 `logs\mcp-server-mining-news.log` 等（`check` 结束时会打印日志路径）；
4. 单独启动某个 server 看报错：`$env:LOG_LEVEL="DEBUG"; uv run python -m servers.lme_price.server`（正常情况下它会安静地等待 stdin，Ctrl+C 退出）。

**开了 Clash / V2Ray 等系统代理**：server 访问 Google News / Yahoo 会走系统代理（这是需要的）；agent 连接自己的 MCP server（HTTP 模式）不走代理，避免 `502 Bad Gateway`。确实要经代理访问远程 MCP server 时设置 `$env:MCP_HTTP_USE_PROXY="1"`。

**联网失败但仍想跑通**：加 `--offline`，所有数据都来自 fixtures / mock。

**LLM 配了但没生效**：确认 `.env` 在项目根目录、`LLM_API_KEY` 非空，且 `LLM_BASE_URL` 可访问；失败会自动回退离线模板，报告顶部会显示当前生成方式。

## 8. 已知限制

- Pilbara 锂项目多为澳交所公司，按 **JORC** 披露，不是加拿大 **NI 43-101**；解析器两者都支持并在结果里标出 `reporting_code`。
- LME 没有免费行情 API，锂的 LME 合约流动性也很低。价格 provider 为可插拔设计，默认免费源失败会降级到确定性的 `mock` 序列，并在报告中标注 `source=mock`。
- 锂价用 CME 氢氧化锂期货 `LTH=F`（Yahoo 报价单位 USD/kg，已换算为 USD/t）。Yahoo 免费接口对这个合约只返回最新结算价、没有历史，所以联网时锂的**现价是实时的、7/30 天涨跌幅留空**（不会用模拟数据冒充真实走势），报告「数据说明」里会写明原因。锂辉石没有免费数据源，始终为 `mock`。
- 样例 PDF 是示意数据，不是官方 ASX/JORC 披露；在 `fixtures/reports/index.json` 填入真实报告 PDF 链接即可解析真实数据。
