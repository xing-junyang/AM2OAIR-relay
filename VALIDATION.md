# 实际验收记录

验收日期：2026-10-01（Asia/Shanghai）。密钥、完整提示词、模型输出和请求头未写入本记录。

| 检查 | 实际结果 |
| --- | --- |
| 独立 Poixe Anthropic Messages gate | `claude-opus-5-5`，HTTP 200，input=15 / output=4 token |
| pytest | **132 passed**；原有协议、持久化、隐私和费用测试通过；新增顶层 oneOf/allOf/anyOf、第 17 个工具、namespace、SSE 解包、历史重包、引用作用域、异常封装和凭据保护测试 |
| Ruff | All checks passed |
| TypeScript | `tsc --noEmit` 通过 |
| Vite production build | Vite 8.3.1 成功，本地和 Docker Node builder 均实际执行 |
| 最终运行环境 | Python **3.12.14**；运行镜像中无 Node.js 可执行文件 |
| 前端 | Dashboard HTML、实际 JS/CSS、SPA 路由刷新均通过；浏览器正常渲染，未发现 console error |
| 管理 API | status、logs、stats、hour/day 聚合正常 |
| Docker 中的 JSON + SSE 调用 | 三轮 JSON/SSE 验收共六次真实 Poixe 请求完成，累计 117 token；费用功能验收新增 39 token；SSE sequence_number 连续递增 |
| SQLite 直接读取 | 真实调用、token 和费用元数据已落库，schema version=3；升级前 8 条记录全部保留 |
| 容器 restart | 两次调用的 request_id、总量及 token 统计完全保留 |
| 容器 force-recreate | 相同数据保留，named volume 未删除 |
| 端口 | 镜像只 EXPOSE 8787/tcp；宿主机仅绑定 `127.0.0.1:8787` |
| 真实函数调用 | auto 策略下 function_call SSE 参数增量、done 事件和工具结果回传成功；两次请求累计 549 token |
| 真实 custom 调用 | `smoke_tools.py --kind custom` 通过；custom input delta/done、custom_tool_call 和 custom_tool_call_output 回传通过；2 次调用累计 622 token |
| 真实 namespace 调用 | `smoke_tools.py --kind namespace` 通过；函数名称与 namespace 还原、参数 SSE 与历史回传通过；2 次调用累计 644 token |
| 错误元数据 | 两次强制工具选择诊断返回 400，保留了 request_id 和 upstream_request_id，未保存正文 |
| 隐私 | 实际 Docker SQL dump、数据库/WAL 文件和容器日志未包含上游密钥；SQL 与日志未包含测试提示词；自动化测试另覆盖模型输出、Authorization 与 Cookie |
| UI 交互 | 刷新、HTTP 状态码筛选、近 7 天查询及自定义时间查询通过；12:00–13:00 只显示新增 2 次调用与 39 token，费用统计与日志同步筛选 |
| 生产镜像中的费用计算 | 使用临时数据库、MockTransport 和明确的虚构单价，普通输入、输出、缓存读取、5m/1h 写入合计 0.006400000；改变配置并重新初始化后历史费用不变；时间过滤和隐私检查通过 |
| 配置单价后的前端分支 | 隔离的临时本地验收服务中，示例 3 条记录合计 USD 0.0192；选择 10:30–11:30 后只显示 1 条、USD 0.0064，明细及费用图正常，无 console error；临时服务已停止 |
| 实际 Codex CLI 会话 | 隔离容器中的官方 CLI **0.159.2**，客户端与上游均使用 `claude-opus-5-5`；真实创建、读取并验证临时文件后正常完成；2 次请求均成功，累计 26,183 token；没有挂载或修改用户的 `~/.codex`，没有挂载 `.env` |
| namespace 修复时的 Docker smoke | 更新生产镜像后，前端与管理 API、JSON/SSE、SQL 写入、费用和时间查询通过；新增 2 次成功调用、39 token；完整统计对象在 restart 与 force-recreate 后一致 |
| namespace 修复时的数据库隐私 | SQL dump、DB/WAL/SHM 均未包含服务密钥；SQL 未包含 CLI、custom、namespace 测试提示词 |

此前 namespace 修复验收结束时数据库保留 **21 条记录：16 次成功、5 次失败，input=27,755 / output=399 / total=28,154 token**。失败包括此前 2 次上游强制工具选择诊断，以及修复前 3 次本地 `invalid_request`；本次 CLI/custom/namespace/Docker 的 8 次新增调用全部成功。

该次 namespace 验收中，生产服务读取 `.env` 中配置的单价，8 次新增调用都有费用快照；其余 13 条记录的费用保持未知，不追溯估算。USD 费用聚合显示 **0.027198250**，属于配置单价下的估算，并非上游账单确认金额。本次未修改用户的密钥、模型或单价配置，也没有采用虚构示例价格。

升级前在线备份保存在 `backups/before-cost-schema3.db`。此前 namespace 修复时的生产镜像实际再次执行 TypeScript 与 Vite 构建，保留相同 named volume；重启与重建后 21 条记录、28,154 token 以及完整费用统计保持一致。测试 CLI 镜像仅用于一次性验收，不加入生产 Compose，不发布额外端口；最终生产运行环境仍只有 Python。

实际页面截图：`artifacts/cost-dashboard.jpg`、`artifacts/cost-dashboard-preview.jpg`。隔离示例截图：`artifacts/cost-qa-fixtures.jpg`，其中数据和价格仅供验收。

## 顶层组合 schema 修复验收

用户报告的 `tools.16.custom.input_schema` 错误来自上游对工具 schema 顶层组合关键字的限制。中继保留完整原 schema，并在上游使用普通 object 的 `arguments` 字段封装；Responses JSON 和 SSE 解包为原始参数，后续 function_call 历史按当前工具定义重包。调整局部引用时保留独立资源作用域及字面量数据；没有显式资源 `$id` 的 `$recursiveRef` 组合 schema 返回本地 400。

- pytest：**132 passed**；Ruff lint 和 format check 均通过。
- Docker 中真实 `oneOf`：SSE 与工具结果回传通过，2 次调用、799 token。
- Docker 中真实 `allOf`：SSE 与工具结果回传通过，2 次调用、851 token。
- Docker 中真实 `anyOf`：SSE 与工具结果回传通过，2 次调用、1,016 token。
- 三种组合共 **6 次成功调用、2,666 token**；客户端收到原来的参数对象，没有上游封装字段。
- 首次 oneOf 调用已成功；首次后续回复被 64 token 上限截断，HTTP 200、success=false、error_category=max_output_tokens。将 smoke test 的后续输出额度提高到 512 后完整往返通过，没有改变生产输出额度或重试策略。
- 实际 SQL dump、DB/WAL/SHM 中均未出现服务密钥；工具 smoke 的提示词没有进入 SQL。
- 本轮最终 Docker smoke：前端、管理 API、JSON/SSE、SQL、费用及时间查询均通过；新增 2 次成功调用、38 token。restart 与 force-recreate 后完整统计及 request_id 均保留；只暴露 8787。
- 本轮验收结束时数据库：32 条记录、25 次成功、7 次失败；input=30,301 / output=1,308 / total=31,609 token。保留了修复前的失败及首次额度截断记录。
- 生产容器已部署新转换代码；前端未修改，本轮 Docker 构建复用此前实际完成 TypeScript/Vite 构建的资源层。

## 已确认的上游限制

- 默认 urllib User-Agent 得到 Cloudflare HTTP 403 / 1010；显式应用 User-Agent 已用于 gate 和中继。
- 有前缀 `aws-claude/claude-opus-5-5` 曾返回 HTTP 503 `model_overloaded`；无前缀型号实际调用通过，当前 `.env` 使用无前缀型号。
- 当前无前缀型号对 Anthropic `tool_choice.type=any/tool` 返回 HTTP 400，错误消息为 `tool_choice: type "tool" and "any" are not supported for this model.` auto 策略的真实函数调用已验证成功。
- 原来的 `MVP supports function tools only` 来自中继的本地校验；0.159.2 实际请求含 `multi_agent_v1` namespace。已补齐 namespace 与 custom 转换；未让测试 CLI 调用或创建任何子代理。
- Anthropic 没有与 OpenAI custom grammar 等价的约束解码参数；grammar 仅作为工具说明传给模型。Custom SSE 在一个 JSON 参数块完成后发送解码后的原始字符串。内置搜索、tool_search、多模态和其他边界见 README。

构建有一个单个 JS chunk 超过 500 kB 的体积提示；pytest 有一个 Starlette 对 HTTPX TestClient 的弃用提示。两者均未导致验证失败，运行和测试依赖已固定版本。

重跑命令见 README。`scripts/smoke_docker.py` 会重启和重建 relay 容器并保留 volume；`scripts/smoke_tools.py` 支持 function/custom/namespace/oneOf/allOf/anyOf；`scripts/codex_smoke.mjs` 配合 `scripts/codex-smoke.Dockerfile` 验证真实 CLI 会话，控制台仅输出元数据。
