# AM2OAIR Relay

一个通过 Docker 在本地运行的 Responses → Anthropic Messages 协议中继器。FastAPI 与 React Dashboard 在**同一个镜像、同一个 8787 端口**运行，最终镜像只有 Python 3.12，无常驻 Node.js 服务。

```text
Codex CLI → http://127.0.0.1:8787/v1/responses
          → https://api.poixe.com/v1/messages → claude-opus-5-5
Dashboard → http://127.0.0.1:8787/
```

## 上游验证与配置

首次部署，在项目目录执行（已有 `.env` 时不要覆盖）：

```bash
cp .env.example .env
chmod 600 .env
```

在编辑器中填写 `AM2OAIR_RELAY_API_KEY`，不要将密钥放进命令行参数、Git 或聊天记录。环境变量优先于 `.env`。

| 环境变量 | 缺省值 / 用途 |
| --- | --- |
| `AM2OAIR_RELAY_API_KEY` | 必填，上游密钥 |
| `AM2OAIR_RELAY_BASE_URL` | `https://api.poixe.com/v1`，会追加 `/messages` |
| `AM2OAIR_RELAY_MODEL` | 未设置时为 `aws-claude/claude-opus-5-5`；本项目 `.env.example` 与本地配置使用实际验证通过的 `claude-opus-5-5` |
| `AM2OAIR_RELAY_DATABASE_URL` | `sqlite:////data/relay.db` |
| `AM2OAIR_RELAY_TIMEOUT_SECONDS` | `180`，上游读取超时，连接超时为 15 秒 |
| `AM2OAIR_RELAY_DEFAULT_MAX_TOKENS` | `8192`，未提供 `max_output_tokens` 时使用 |

更换其他中转站只需修改 Base URL、模型与密钥。其接口必须兼容 Anthropic Messages，并接受 `x-api-key` 和 `anthropic-version: 2023-06-01`；Base URL 不允许包含用户名、密码或 query 密钥。客户端请求的 `model` 作为别名处理，实际模型始终由环境变量配置，`GET /v1/models` 返回这一模型。

开发前先运行独立上游 gate，它只依赖 Python 标准库，不启动服务、不写数据库：

```bash
python3.12 scripts/smoke_upstream.py
# 临时测试另一个模型，不改 .env：
AM2OAIR_RELAY_MODEL=claude-opus-5-5 python3.12 scripts/smoke_upstream.py
```

退出码 `0` 表示收到并验证了 Anthropic 文本消息；`1` 表示上游 HTTP 拒绝；`2` 表示配置缺失；`3` 表示连接失败；`4` 表示响应不符合预期。失败时输出 HTTP 状态和原始错误正文；如果上游回显密钥，密钥会被替换为 `[REDACTED]`。成功时只输出状态、request_id 和 token 数。

本次实测：默认 Python urllib 标识触发 Cloudflare `403 / 1010`；使用明确的 `AM2OAIR-relay/0.1` User-Agent 后，有前缀模型返回 Poixe `503 model_overloaded`，无前缀 `claude-opus-5-5` 返回 HTTP 200。因此 smoke test 和中继均显式设置应用 User-Agent。

## Docker 启动与 curl 测试

```bash
docker compose up -d --build
docker compose ps
curl --noproxy '*' -fsS http://127.0.0.1:8787/healthz
curl --noproxy '*' -fsS http://127.0.0.1:8787/v1/models
curl --noproxy '*' -fsS http://127.0.0.1:8787/api/admin/status
```

非流式请求：

```bash
curl --noproxy '*' -sS http://127.0.0.1:8787/v1/responses \
  -H 'Content-Type: application/json' \
  -d '{"model":"claude-opus-5-5","instructions":"Be concise.","input":"Reply with OK.","max_output_tokens":64,"stream":false}'
```

SSE 请求：

```bash
curl --noproxy '*' -N -sS http://127.0.0.1:8787/v1/responses \
  -H 'Content-Type: application/json' \
  -d '{"model":"claude-opus-5-5","input":"Reply with OK.","max_output_tokens":64,"stream":true}'
```

本机启用了 HTTP/HTTPS 代理时，请为 localhost 设置 NO_PROXY；验收脚本显式直连，curl 示例也指定 --noproxy。

调用会产生上游 token 用量。中继不自动重试上游请求，避免重复费用与重复工具调用。默认只有本机能够访问此端口；本地接口不要求客户端鉴权。请求中的 Authorization、Cookie 和客户端 API Key 不会转发给上游。

在浏览器打开 **http://127.0.0.1:8787/**。页面每 30 秒刷新，也可以手动刷新；统计时间支持全部、今天、近 7 天、近 30 天与自定义起止时间，统一筛选请求、token、费用和日志。日志另支持分页和 HTTP 状态码筛选。刷新 `/dashboard/requests` 等前端路径也可加载 SPA。`/v1/*`、`/api/*`、`/healthz` 和 `/assets/*` 的未知路径保持 404。

```bash
# 查看运行日志（不含提示词、输出或鉴权头）
docker compose logs --tail=100 -f relay
# 停止服务，保留容器与数据库
docker compose stop
# 删除容器和网络，保留数据库 volume
docker compose down
# 重启 / 重新创建服务，历史仍保留
docker compose restart relay
docker compose up -d --force-recreate
```

## Responses 支持范围

| 能力 | MVP 行为 |
| --- | --- |
| `instructions`、字符串 input | 转换成 Anthropic system / user |
| user / assistant / system / developer 消息 | 支持文本 content 数组；system/developer 合并到 system，相邻相同角色合并 |
| function tools | 转换 name / description / parameters 为 input_schema；顶层 oneOf/allOf/anyOf 用对象封装保留原 schema，输出与历史自动解包/重包；不承诺 strict schema 约束 |
| function_call 历史、function_call_output | 保留 call_id，与 tool_use / tool_result 双向映射；连续多个工具调用/结果合并 |
| custom/freeform tools | 把原始字符串包装为 Anthropic `input_schema` 的 `input` 字段，再还原 `custom_tool_call`；支持 `custom_tool_call_output` 历史回传 |
| namespace tools | 展开 function/custom 子工具，使用稳定且无冲突的上游名称；返回时还原 name 和 namespace，支持历史与指定工具选择 |
| 原生 tool_use / tool_result | 支持 input item 以及消息内对应 content blocks；支持 is_error |
| tool_choice | 转换 auto / required / none / 指定 function 或 custom（含 namespace）；目标模型的强制选择限制见下文 |
| max_output_tokens、temperature、top_p | 转换；temperature/top_p 范围为 0–1 |
| stream=false | Responses 对象，包括 output_text、function_call、custom_tool_call、usage、status |
| stream=true | 文本与普通函数参数实时转换；custom 与顶层组合 schema 的工具在一个上游 JSON 参数块完成后发出解码/解包结果；sequence_number 从 0 连续递增 |
| token 用量 | 输入包含 Anthropic 普通输入、缓存创建和缓存读取；cached_tokens 单列缓存读取 |
| 正常结束 / 工具请求 | status=completed；工具执行由客户端负责 |
| max_tokens 截断 | status=incomplete，reason=max_output_tokens；SSE 发 response.incomplete |
| 上游 4xx / 5xx | 保留 HTTP 状态与错误 JSON，保留上游 request_id；过滤回显的凭据与敏感字段 |
| SSE 途中失败 | HTTP 已是 200，发 error 与 response.failed；数据库 success=false 并记录错误分类 |

SSE 文本事件：`response.created`、`response.in_progress`、`response.output_item.added`、`response.content_part.added`、`response.output_text.delta`、`response.output_text.done`、`response.content_part.done`、`response.output_item.done`、`response.completed`。函数额外使用 `response.function_call_arguments.delta` 与 `response.function_call_arguments.done`；custom 工具使用 `response.custom_tool_call_input.delta` 与 `response.custom_tool_call_input.done`，保留换行、引号和 Unicode 原文。失败使用 `error`、`response.failed`。

Custom 的 Lark/regex grammar 会加入上游工具说明，但 Anthropic 接口没有等价的约束解码参数，因此不保证语法约束；最终输入校验和工具执行仍由客户端负责。工具定义、参数和结果仅存在于当前请求内存中，不进入数据库、管理 API 或默认服务日志。

Anthropic 拒绝工具 `input_schema` 顶层的 `oneOf`、`allOf`、`anyOf`。中继将这类 schema 放入普通 object 的 `arguments` 属性中，不删除分支和约束；仅在与上游通信时使用封装，Codex 仍收到原始 function arguments。局部 JSON Pointer `$ref` 随嵌套位置调整，具有独立 `$id` 的资源及外部引用保留原作用域。原本就在属性内部的组合 schema 无需封装。组合 schema 中的 `$recursiveRef` 需要其所在资源有显式 `$id`，否则返回明确 400，避免改变递归引用语义。

`reasoning`、`include`、`store`、`parallel_tool_calls`、`prompt_cache_key`、`text.verbosity`、`text.format` 和 metadata 等可选字段安全忽略；历史 reasoning item 及上游 thinking block 丢弃。`store=true` 也不会保存模型内容；parallel_tool_calls 不限制上游并行调用。

尚未实现：多模态输入输出、OpenAI 内置 web/file search、服务端 MCP、computer use、Structured Outputs/grammar 严格保证、推理内容、服务端对话记忆、previous_response_id、background、响应检索/删除、WebSocket、上下文压缩接口和 tool_search 延迟工具加载。客户端把 MCP 工具声明为普通 function/custom 时，可使用已有转换。对于会丢失关键语义的 unsupported input/tool 类型、previous_response_id、conversation 和 background=true，返回明确 400。本项目支持所列文本、function/custom 和 namespace 转换，不代表完整 Responses API。

Poixe 的 `claude-opus-5-5` 实测拒绝 Anthropic `tool_choice.type=any/tool`，返回 HTTP 400：`tool_choice: type "tool" and "any" are not supported for this model.` 因此当前目标模型应使用 `auto`（或 `none`）；强制策略不会被偷偷改成 auto，中继会保留该上游错误。

## 数据库、统计和管理 API

数据库默认在容器 `/data/relay.db`，挂载 named volume **`am2oair-relay_relay_data`**。compose 固定项目名；删除或重建容器不会删除 volume。`docker compose down -v` 会删除数据库，不要用于普通停止。

使用 `sqlite:////data/other.db` 可更换 volume 内的文件。主机开发可使用 `sqlite:///./data/relay.db`。若改变为 `/data` 以外的目录，需要同时配置持久化挂载和 UID 10001 的写权限。

每次数据库操作创建独立短连接并放在 FastAPI 线程池执行，启用 WAL、30 秒 busy timeout、foreign_keys 和 synchronous=NORMAL。启动时在事务中按版本执行 `relay/database.py` 的迁移，记录 schema_migrations 并更新 PRAGMA user_version；不会清空旧记录，拒绝高于当前应用的 schema。

数据库只保存请求时间、固定接口名、服务配置的模型、是否流式、实际 HTTP 状态、耗时、随机本地 request_id、上游 request_id、input/output/total token、success 与固定错误分类，以及数值缓存用量、币种、请求时的单价快照、费用明细和费用状态。统计只计 `/v1/responses`；Dashboard、healthz、models 请求不计费也不混入统计。失败/截断计入失败次数；中断的用量仅依据已收到的 usage，不推算未收到的 token。

| 路径 | 查询参数 / 结果 |
| --- | --- |
| `GET /api/admin/status` | 运行状态、运行时间、公开配置、数据库状态与 pricing 单价 |
| `GET /api/admin/logs` | page、page_size（1–100）、status_code、start、end；每条记录含 cost 费用明细 |
| `GET /api/admin/stats` | bucket=hour/day、start、end；返回 totals、series 与 costs；series 每个时间桶也含 costs |

时间过滤使用带时区的 ISO 8601，例如 `2026-10-01T00:00:00%2B08:00`，起止时间均包含边界。聚合按 UTC 整小时/天，前端显示浏览器本地时间。趋势覆盖所选时间范围，默认总量展示全部历史；长时间范围建议按天聚合。输入 header `x-request-id` 不直接保存，防止将客户端任意内容作为元数据记录。客户端响应的 `x-request-id` 与管理日志的 request_id 一致，`x-upstream-request-id` 保留经过校验的上游 ID。

## Cost 配置与按时间查询

Dashboard 展示所选时间的**估算费用**、普通输入/输出/缓存费用明细、费用趋势及日志中的单次费用。费用根据上游返回的 usage 与你配置的单价计算，不能代替中转站账单；不自动套用 Anthropic 官方价格，不包含折扣、税费、长上下文阶梯价格或额外服务费。

在现有 `.env` 中补充下列配置，填入该中转站对当前模型的实际单价，单位统一为 **每 100 万 token**。`.env.example` 已包含这些字段。

| 环境变量 | 用途 |
| --- | --- |
| `AM2OAIR_RELAY_COST_CURRENCY` | 三位大写币种代码，默认 `USD`，可设置 `CNY` 等；不会自动换汇 |
| `AM2OAIR_RELAY_COST_INPUT_PER_MILLION` | 普通、未缓存输入单价 |
| `AM2OAIR_RELAY_COST_OUTPUT_PER_MILLION` | 输出单价 |
| `AM2OAIR_RELAY_COST_CACHE_READ_PER_MILLION` | 缓存读取单价 |
| `AM2OAIR_RELAY_COST_CACHE_WRITE_PER_MILLION` | 上游只提供缓存写入总数、没有 TTL 明细时使用的通用单价 |
| `AM2OAIR_RELAY_COST_CACHE_WRITE_5M_PER_MILLION` | 上游报告 5 分钟缓存写入明细时使用的单价 |
| `AM2OAIR_RELAY_COST_CACHE_WRITE_1H_PER_MILLION` | 上游报告 1 小时缓存写入明细时使用的单价 |

单价默认留空，表示**未知**；显式 `0` 才表示免费。单次请求中 token 数为 0 的类别无需配置单价；任何有用量的类别缺少单价，则该请求显示“单价未配置”，不以 0 元参与费用合计。支持非负十进制数，最多 9 位小数；范围为 0–1,000,000。未细分的缓存写入不会猜测 TTL；已细分时分别用 5m / 1h 单价。[Anthropic 缓存用量字段说明](https://platform.claude.com/docs/en/build-with-claude/prompt-caching)。

费用 =（普通输入 token × 输入单价 + 输出 token × 输出单价 + 缓存读取 token × 读取单价 + 缓存写入 token × 对应写入单价）/ 1,000,000。Responses 的 input_tokens 含缓存，但普通输入费用只用原始 Anthropic input_tokens，避免重复收费。计算使用 Decimal，SQLite 保存币种单位的十亿分之一整数；各项四舍五入到这一精度后相加，API 返回十进制字符串。

修改配置后执行，容器会重新创建并保留 volume：

```bash
docker compose up -d
```

每条请求固定保存请求时的单价和费用。修改单价或模型不会重新计算历史费用，不同币种分开汇总，前端可切换币种。Schema 3 迁移保留已有历史日志；迁移前记录缺少单价与缓存明细，标记为“历史费用未知”，不倒填猜测价格。未收到 usage 的失败显示“未收到用量”；流式中断则按已经收到的用量计算并标记“仅含已收到的用量”，可能小于上游最终账单。合计同时展示已估算、未知和部分用量的请求数。

前端输入本地起止时间后点击“查询统计”，或选择快捷时间范围；结束时间留空表示到现在。按天查询费用与用量 API 的示例：

```bash
curl --noproxy '*' -fsS --get http://127.0.0.1:8787/api/admin/stats \
  --data-urlencode 'bucket=day' \
  --data-urlencode 'start=2026-10-01T00:00:00+08:00' \
  --data-urlencode 'end=2026-10-01T23:59:59+08:00'
```

`costs.currencies` 返回各币种的 input_cost、output_cost、cache_read_cost、cache_write_cost、total_cost，以及 priced_requests / partial_requests。`costs.unpriced_requests` 表示未知费用的记录数；没有已估算记录时 currencies 为空。日志 `cost.total_cost=null` 表示未知，不是零费用。空桶填零，含未知费用而无已估算金额的时间桶在费用图中留空。

## 备份、恢复与清理

在线备份使用 SQLite backup API，包含 WAL 中已提交的数据。输出不会夹杂提示词、密钥或状态文本：

```bash
mkdir -p backups
docker compose exec -T relay python scripts/db_admin.py backup --stdout > backups/relay.db
```

恢复必须先停止服务，避免活跃连接继续写旧数据库。备份先执行 quick_check 与表结构校验，再替换数据库：

```bash
docker compose stop relay
docker compose run --rm --no-deps -T relay \
  python scripts/db_admin.py restore --stdin --service-stopped < backups/relay.db
docker compose up -d relay
```

按时间清理或清空元数据（删除会同步改变统计）：

```bash
docker compose exec -T relay python scripts/db_admin.py clear --before '2026-10-01T00:00:00+08:00'
# 确认需要清空全部历史时：
docker compose exec -T relay python scripts/db_admin.py clear --all
```

## 隐私验证、开发与测试

默认关闭 uvicorn access log、HTTPX/HTTPCore INFO 日志，不记录请求正文、模型正文、Authorization、Cookie 或完整错误正文。API Key 不进入静态构建；`.env` 在 `.gitignore` 和 `.dockerignore` 中。不要执行 `docker compose config` 或完整 `docker inspect` 并将输出分享，它们会显示 Docker 环境变量里的密钥。

数据库 schema 无正文或 header 字段，管理 API 使用固定公开字段。`tests/test_app.py` 用独特的提示词、模型输出、Key、Authorization 与 Cookie 标记验证数据库 SQL dump、数据库/WAL 文件、管理 API 和捕获日志都不含这些内容；错误回显也单独验证。可以再次运行隐私测试确认：

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
.venv/bin/python -m pytest -q -k 'sensitive or secret'
.venv/bin/ruff check relay tests scripts
```

本地后端开发（保留 `.env` 中的 Key，覆盖 Docker 内的数据路径）：

```bash
AM2OAIR_RELAY_DATABASE_URL=sqlite:///./data/relay.db \
  .venv/bin/uvicorn relay.app:app --host 127.0.0.1 --port 8787 --no-access-log --reload
```

前端使用 React + TypeScript + Vite 8 + Tailwind 4，组件由官方 shadcn/ui CLI 安装。需要支持 Vite 8 的 Node.js；建议 Node 24 LTS：

```bash
cd frontend
npm ci
npm run dev       # 仅用于开发，/api 代理到本地 8787
npm run typecheck # 实际 TypeScript 检查
npm run build     # 类型检查 + Vite production build，写入 frontend/dist
```

生产环境 Docker 的 Node builder 会执行 `npm ci` 和 `npm run build`，随后只将 dist 拷入 Python 镜像。主机直接启动 FastAPI 前请先 build；资产 mount 在启动时注册，因此主机构建后需重启后端。

启动容器后执行真实验收脚本：

```bash
python3.12 scripts/smoke_docker.py
# 另测真实工具参数 SSE 与工具结果回传（再产生两次请求）：
python3.12 scripts/smoke_tools.py
# 分别验证 custom/freeform 和 namespace，均会再产生两次请求：
python3.12 scripts/smoke_tools.py --kind custom
python3.12 scripts/smoke_tools.py --kind namespace
# 验证顶层组合 schema 的真实工具调用和历史回传，每种两次请求：
python3.12 scripts/smoke_tools.py --kind oneOf
python3.12 scripts/smoke_tools.py --kind allOf
python3.12 scripts/smoke_tools.py --kind anyOf
```

脚本验证 Dashboard HTML 和实际 JS/CSS、SPA 刷新、health/models/管理 API、真实 JSON 与 SSE 请求、数据库直接查询、用量统计、唯一暴露端口、容器 restart 与 force-recreate 后历史保留。会消耗少量上游 token，也会重启/重建本项目 relay 容器，保留 volume。

`scripts/codex_smoke.mjs` 用实际 Codex CLI 创建、读取并验证临时文件，只输出工具执行状态和用量元数据。应在一次性 CLI 测试容器中执行，只读挂载这一份脚本；不要挂载项目 `.env`、用户主目录或 `~/.codex`。测试容器加入 Compose 网络，使用 `http://relay:8787`，无需发布任何额外端口。该脚本不属于生产镜像，也不启动常驻 Node 服务。

```bash
docker build -t am2oair-codex-qa:0.159.2 -f scripts/codex-smoke.Dockerfile scripts
docker run --rm --network am2oair-relay_default \
  -e AM2OAIR_QA_SANDBOX=danger-full-access \
  --mount "type=bind,source=$(pwd)/scripts/codex_smoke.mjs,target=/qa/codex_smoke.mjs,readonly" \
  am2oair-codex-qa:0.159.2 node /qa/codex_smoke.mjs
```

上述 sandbox 参数仅用于这个没有主机可写挂载的一次性容器；Docker 提供文件系统隔离。CLI 仍使用 `claude-opus-5-5`，密钥只存在于 relay 服务。此验收会发送完整 CLI 工具描述和系统指令，消耗的 token 明显多于小型 curl 测试。自定义 Compose 项目名时，需要相应调整测试容器的 network 名称。

## Codex profile 建议

完整片段见 `examples/codex-profile.toml`。本项目不写入或修改 `~/.codex`；验收后由你自行合并：

```toml
[model_providers.am2oair]
name = "AM2OAIR local relay"
base_url = "http://127.0.0.1:8787/v1"
wire_api = "responses"
supports_websockets = false
requires_openai_auth = false

[profiles.am2oair]
model_provider = "am2oair"
model = "claude-opus-5-5"
model_supports_reasoning_summaries = false
model_reasoning_summary = "none"
web_search = "disabled"
```

```bash
docker compose up -d --build
NO_PROXY=127.0.0.1,localhost no_proxy=127.0.0.1,localhost codex --profile am2oair
```

上游密钥只配置给服务，不需要提供给 Codex。配置参考 [Codex 官方配置文档](https://learn.chatgpt.com/docs/config-file/config-reference)；协议依据 [Poixe Anthropic Messages](https://docs.poixe.com/cn/api-reference/text-api/anthropic-messages/overview)、[OpenAI Streaming Responses](https://developers.openai.com/api/docs/guides/streaming-responses) 与 [Function Calling](https://developers.openai.com/api/docs/guides/function-calling)。
