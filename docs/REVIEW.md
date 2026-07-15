# AgentBox 全面代码 Review

> 审查日期：2025-07-15
>
> 审查范围：全部源文件、测试、配置、文档、CI、Docker/K8s 资产
>
> 基准：SPEC.md 中列出的六个 JD 要点（durable execution、sandboxing/credential scoping、scaling/cold-start/cost、orchestration、Logfire+MCP observability、Pydantic AI 深度集成）

---

## 总体结论

这个项目的**方向和骨架非常对**：durable execution（checkpoint/replay）、租约+reaper 的 resume 机制、Docker/K8s 双后端抽象、conventional commits、uv/ruff/pre-commit/dependabot/mkdocs/codecov 这套工程化配置，都踩在了 Pydantic 那个 JD 的点上。commit 历史干净，文档结构也像样。

但目前它离"能被他们当真"还有明显距离。核心问题是：**README 里的每一个核心卖点，在当前代码里几乎都没有真正成立**。runner 在 pydantic-ai 2.9.1 下根本无法启动，这意味着 kill-and-resume 这个招牌演示从未在当前依赖下端到端跑通过。对招聘场景来说，最大的风险不是功能少，而是**宣称与实现不符**——面试官（尤其是 Logfire 的作者们）核查任何一条都会立刻发现。

> ⚠️ 说明：JD 页面本身无法直接抓取（pydantic.dev 对爬虫返回 403），本 review 以 SPEC.md 中转述的六个 JD 要点为基准。

---

## P0 — 致命：端到端流程当前跑不通（必须最先修）

### 1. Runner 与 pydantic-ai 2.9.1 不兼容，容器一启动就崩

**位置**：`src/agentbox/runner/main.py:141-161`

**问题**：
- `from pydantic_ai.models.openai import OpenAIModel` 在锁定版本里是 `ImportError`（已改名 `OpenAIChatModel`）
- 新版构造函数签名是 `(model_name, *, provider=..., profile=..., settings=...)`——不接受 `api_key=` 和 `base_url=`
- DeepSeek 需要 `OpenAIChatModel("deepseek-chat", provider=OpenAIProvider(base_url="https://api.deepseek.com", api_key=...))`
- `AnthropicModel(api_key=...)` 同样不成立

**影响**：单元测试全绿因为用 `FakeModel` 绕过了这条路径，但真实 runner 容器一启动就 crash。

**修复**：
```python
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIProvider

if api_key:
    provider = OpenAIProvider(
        base_url="https://api.deepseek.com/v1",
        api_key=api_key,
    )
    return OpenAIChatModel(
        settings.model_name,
        provider=provider,
    ), settings.model_name
```

### 2. 每个成功的 run 最终会被系统标记为 failed（reaper 逻辑 bug）

**位置**：`src/agentbox/runner/main.py`（成功完成后只 cancel 了 heartbeat task，**从不删除 lease**）

**位置**：`src/agentbox/launcher/worker.py:123-135`（`get_dead_leases` 查询**不过滤 run 状态**）

**问题**：
- Runner 成功结束后只 cancel 了 heartbeat task，但 lease 记录还留在 `leases` 表中
- `get_dead_leases` 的查询是：
  ```sql
  SELECT l.run_id, l.owner, l.heartbeat_at, r.attempt, r.max_attempts
  FROM leases l JOIN runs r ON r.id = l.run_id
  WHERE l.heartbeat_at < now() - make_interval(secs => $1)
  ```
  这里没有 `AND r.status = 'running'` 过滤
- 结果：run 成功 30 秒后 lease 过期 → reaper 把它 requeue → 重新跑一遍（replay 快进后再次 succeeded）→ 循环，直到 attempt 耗尽被 `fail_run` 永久标记为 **failed**

**修复**：
1. Runner 结束（成功或失败）时删除 lease
2. `get_dead_leases` 加 `AND r.status = 'running'`

### 3. Docker compose 全栈下 runner 连不上数据库

**位置**：`src/agentbox/launcher/backend_docker.py:59`（runner 容器只挂在 `agentbox-internal` 网络）

**位置**：`docker-compose.yml`（Postgres 只在 `agentbox-control` 网络）

**问题**：
- Runner 容器只挂在 `agentbox-internal`（`internal: true`）网络
- Postgres 只在 `agentbox-control` 网络
- Runner 拿到的 `DATABASE_URL` 是 `postgres:5432`，在 internal 网络里既解析不到也路由不到
- Checkpoint 一条都写不进去

**修复**：给 Postgres 也挂 `agentbox-internal` 网络，或经由 egress-proxy 中转，并把连通性写进 e2e 验证。

### 4. 出口控制（egress default-deny）实际不存在——tinyproxy 配置语义用错了

**位置**：`docker/tinyproxy.conf`

**问题**：
- tinyproxy 的 `Allow`/`Deny` 控制的是**哪些客户端 IP 可以使用代理**，不是可以访问哪些目标域名
- 当前配置：(a) 只 `Allow 127.0.0.1` → sandbox 容器作为客户端会被直接拒绝，连 LLM API 都调不通
- (b) 目标域名 allowlist（需要 `Filter` + `FilterDefaultDeny Yes` + filter 文件）完全没有配置，`api.deepseek.com`/`api.anthropic.com` 没出现在任何地方
- (c) 每 run 的 `egress_allow` 字段存进了数据库但没有任何消费方
- README 的 "default-deny egress" 卖点当前为假
- SPEC 1.6 要求的"e2e 测试证明 example.com 被拒、LLM API 可通"也不存在

---

## P1 — 核心卖点与实现不符（面试官一查即穿）

### 5. Credential scoping 是空壳：所谓 scoped credential 就是 master key 本身

**位置**：`src/agentbox/secrets/scoper.py:23-38`

**问题**：
- `mint_scoped_credentials` 原样返回 master API key，只是包了层 JSON 信封
- README/docs 宣称 "master API keys never enter the sandbox"，实际进入沙箱的就是 master key 的值
- TTL 只是存了个字段，**没有任何代码检查 `expires_at`**

**建议修复**：让 egress proxy 做凭证注入——沙箱只拿到一个无意义的 per-run token，proxy 转发到 LLM API 时才替换成真 key。这样 key 只存在于控制面/proxy，才配得上 README 的宣称。

### 6. 沙箱内不受信代码可以完全控制控制面数据库

**问题**：
- Runner 拿到完整的 `DATABASE_URL`（agentbox 超级用户）
- 沙箱里的不受信 agent 代码可以：读取 `scoped_credentials` 表里**所有 run 的明文凭证**、篡改任意 run 的状态/结果/checkpoint、删表
- 对一个以"运行不受信 workload"为主题的平台，这是最大的架构漏洞

**建议修复**（任选其一，需在 docs 里论证）：
- Per-run 最小权限 DB role（只能读写自己 run_id 的行，用 RLS）
- 或 runner 完全不接触 DB，改为通过控制面提供的 checkpoint HTTP API 读写（凭 per-run token 认证）
- Credential 在 Postgres 里明文存储，至少要加密（envelope encryption）并在文档里写清威胁模型

### 7. Cost tracking 恒为 0——定义了费率表但从没人调用

**位置**：
- `src/agentbox/runner/durable_model.py:163-167`：调 `context.step()` 时不传 `token_count`/`cost`
- `src/agentbox/runner/durable_model.py` 的 `_estimate_cost` 和 `MODEL_COST_RATES` 是死代码
- `src/agentbox/settings.py`：`cost_per_1k_input_tokens: 0.15`（DeepSeek 实际是 0.00027，差 500 倍）
- `src/agentbox/cost/tracker.py:105-108`：用拍脑袋的 70/30 拆 input/output

**影响**：所有 checkpoint 的 token_count/cost 都是 NULL，`/runs/{id}/cost` 和 `cost_estimate` 永远是 0

**修复**：从 `ModelResponse.usage` 提取真实 input/output tokens 入库

### 8. 工具调用实际没有被 checkpoint——"every tool call is checkpointed" 为假

**位置**：
- `src/agentbox/runner/durable_tool.py`：`durable_tool` 装饰器写了、测了
- `src/agentbox/runner/main.py:222`：构建 agent 时用的是 `agents.py` 里**未包装的裸工具**
- `src/agentbox/runner/agents.py:140`：`open_github_issue` 用 Python `hash()`（每进程随机化）生成 issue_id

**问题**：
- 10 秒的 `analyze_logs` 在 resume 时会原样重跑
- `hash()` 每进程随机化 → replay 跨进程结果不同，直接破坏确定性、触发 fingerprint mismatch

**修复**：
- 在 runner 里把工具用 `durable_tool(context)` 包装后传入 agent
- `hash()` 换成基于内容的稳定哈希（如 hashlib）

### 9. MCP server 是壳，且查询方式对 Logfire 是错的

**位置**：`src/agentbox/mcp_server/server.py:44-80`

**问题**：
- `_fetch_logfire_traces` 向 `localhost:4318/v1/traces` POST 一个自造的查询体
- 这是 OTLP 的**写入（ingest）**端点，不是查询接口
- Logfire 真正的查询 API 是 `https://logfire-api.pydantic.dev/v1/query`（read token + SQL over records）
- 当前所有 MCP 工具实际只会返回 "No Logfire trace data available" 的 fallback 文案
- 文案里还自夸 "The MCP server is functioning correctly"——这种话放在给 Logfire 作者看的项目里非常危险

**修复建议**：
- 接真实的 Logfire query API（SQL 查 `records` 表，按 `attributes->>'run_id'` 过滤）
- 顺带建议改用 FastMCP 风格或直接演示 pydantic-ai 的 MCP toolset 集成
- 补上 SPEC 2.5 要求的"ops agent 通过 MCP 回答『为什么 run X 这么慢』"的脚本化演示

### 10. Warm pool / cold-start 优化是死代码，且设计不可行

**位置**：`src/agentbox/launcher/warm_pool.py`

**问题**：
- 整个类从未被 `worker.py` 实例化
- 设计上也走不通——Docker 无法给运行中的容器修改 env（`RUN_ID`）
- `sleep infinity` 的容器无法接管 run
- `cold_start_ms` 没有在任何地方记录为 span attribute
- README/SPEC 承诺的 benchmark 数字不存在

**修复建议**：要么删掉、要么改成可行方案（如：warm 容器启动后自己 poll 数据库领任务，launcher 只负责补池），并给出真实的冷启动对比数字。

### 11. 招牌测试 kill-and-resume 的核心断言被掏空，且 CI 中从不运行

**位置**：`tests/e2e/test_kill_and_resume.py:187-209`

**问题**：
- SPEC 验收标准是"证明没有 step 被重复执行"
- 现在的断言退化成 `assert final_attempt is not None`（永真）
- "model calls 不增加"的断言只剩注释
- 该测试需要真实 API key，CI 里直接 skip
- **项目的核心卖点在 CI 中零验证**

**修复建议**：
- 用 pydantic-ai 的 `TestModel`/`FunctionModel`（他们官方推荐的测试方式）做一个不需要外部 key 的 kill-resume 场景
- 在 CI 里以 docker compose 起全栈跑通
- 恢复严格断言（kill 后 model_call checkpoint 数量不增加）

---

## P2 — 正确性与健壮性问题

### 12. Replay 反序列化丢信息

**位置**：`src/agentbox/runner/durable_model.py:86-110`

**问题**：
- `_deserialize_model_response` 只重建 text/tool-call part
- 丢 usage/timestamp/thinking parts
- pydantic-ai 自带 `ModelMessagesTypeAdapter` 官方序列化工具（SPEC 1.3 也点名要用），应该换掉手写的

### 13. 连接池被占死

**位置**：`src/agentbox/runner/durable.py:140-191`

**问题**：
- 在持有 pool 连接的同时 await LLM 调用（几十秒）——占死连接池
- fn 成功后、INSERT 前被 kill 会重复一次 model call（这属于可接受的语义，但应写进 docs 的 failure-modes 章节）

### 14. `agent_name` 参数没有任何作用

**问题**：无论传什么，`runner/main.py:222` 都硬编码跑 incident-investigator。至少做一个 agent registry dict。

### 15. API 面缺口

- 没有 `GET /runs`（列表/分页）
- 没有 cancel 端点（schema 里定义了 `canceled` 状态却无入口）
- 没有 `/healthz`（compose 健康检查靠 /docs）
- 非法 UUID 会让 asyncpg 抛异常返回 500 而不是 422

### 16. CORS 配置是反模式

**位置**：`src/agentbox/api/main.py`

**问题**：`allow_origins=["*"]` + `allow_credentials=True`。对纯 API 控制面直接删掉即可。

### 17. K8s 一堆细节没接线

**位置**：`k8s/network-policy.yaml`、`k8s/job-template.yaml`、`k8s/rbac.yaml`、`k8s/runtimeclass-gvisor.yaml`

**问题**：
- NetworkPolicy 用 `matchLabels: agentbox.run_id: "*"`——matchLabels 不支持通配符，这个 policy 匹配不到任何 pod（要用 `matchExpressions: {key, operator: Exists}`）
- 成功完成的 Job 的 Secret 永远不被清理（只有 kill_run 删）
- Job 模板把含密码的 DATABASE_URL 和 LOGFIRE_TOKEN 放明文 env，与"凭证走 Secret"自相矛盾
- Job 没设 `serviceAccountName: agentbox-launcher`（RBAC 白写）
- `AGENTBOX_RUNTIME_CLASS` 在 docs/SPEC 里出现但 settings.py 里根本没有这个配置项，gVisor 只有 manifest 没有 wiring

### 18. Worker 忽略配置

**位置**：`src/agentbox/launcher/worker.py`

**问题**：
- 用模块常量 `MAX_CONCURRENT_RUNS=3`，`settings.max_concurrent_runs` 和 compose 传入的 env 都无效
- `tenants.max_concurrent` 列完全未使用——SPEC/DoD 承诺的 per-tenant 并发限制没有实现（round-robin 倒是有）

### 19. Logfire 埋点远未达到"全链路"

**问题**：
- 只有 FastAPI instrument + runner 一个手动 span
- Launcher 的 claim/start/reap 没有 span
- SPEC 1.7 要求的 `replayed=true` span attribute 没实现
- Trace context 没有传播进容器（API→scheduler→container 的 trace 是断的）
- 没用 `logfire.instrument_asyncpg()`/`instrument_httpx()`
- `logfire.info("...", extra={...})` 用法不对（logfire 用 kwargs）
- 对 Pydantic 而言 observability 深度是重头戏

### 20. 迁移执行器用正则拆 SQL 不安全

**位置**：`src/agentbox/db/migrate.py`

**问题**：用 `re.split(r";\s*\n", sql)` 拆 SQL——遇到函数体或字符串里的分号会炸。asyncpg 的 `execute` 本身支持整段多语句脚本，直接整文件执行即可。

---

## P3 — CI / 工程化 gap

### 21. 没有类型检查

**问题**：
- CI 无 mypy/pyright，pyproject 也没配
- Pydantic 全家桶是 strict-typing 文化（pydantic-ai 自己跑 pyright strict + mypy）
- 建议加 `pyright`（basic 起步，核心模块 strict）进 CI

### 22. Ruff 规则太窄

**问题**：
- 只有 `E,F,I,N,W,UP` 五个规则组
- CI 不跑 `ruff format --check`
- Pre-commit 配了但 CI 不执行 pre-commit
- 建议对齐 pydantic-ai 的 ruff 配置（含 B、C4、PL、RUF 等）

### 23. CI 覆盖面严重不足

**问题**：
- 不 build Docker 镜像
- 无 kind/compose 集成测试
- 无 `uv lock --check`
- 无 concurrency cancel-in-progress
- 覆盖率实测 **15%**：scoper、worker、两个 backend、cost、credentials、warm_pool、settings 全是 0%
- worker 的 SQL 函数、scoper、cost、credentials 都是纯逻辑，很容易测
- 挂着 codecov badge 显示 15% 对求职是减分项

### 24. 测试债务

- `test_placeholder.py` 该删
- SPEC 承诺的 `test_credential_scoping.py`、`test_cost_tracking.py`、egress e2e 不存在
- SPEC 的 Definition of Done 一半以上没达成，而 SPEC.md 还留在仓库根目录——面试官对照会发现大量空头支票
- 要么补齐，要么把 SPEC 改成诚实的 status/roadmap 文档

### 25. Dockerfile 安全与质量

**问题**：
- Runner 以 root 运行（对沙箱项目 `USER nonroot` 是基本盘）
- 没有 `.dockerignore`
- 无 multi-stage 瘦身
- 控制面无 HEALTHCHECK

### 26. 仓库门面

- 无 SECURITY.md（一个安全主题的项目没有安全政策很讽刺）
- 无 issue templates
- 无 CHANGELOG/release 流程
- CONTRIBUTING 只有 38 行
- README 没有 demo GIF/asciinema——kill-and-resume 的 30 秒终端录屏是 SPEC 2.6 明确要求的、也是对招聘最有效的一张牌
- 早期 commit 说加过 "JD mapping" 章节，现在没了；建议以中性的形式放回（如 docs 里一页 "Design goals ↔ production concerns"，不点名 JD，两边都体面）

---

## 与 JD 的 gap 总结（按六个要点打分）

| JD 要点 | 现状 | 主要 gap |
|---|---|---|
| Durable execution | ★★★☆☆ 概念正确，是最强部分 | #7 #8 #12；另外 pydantic-ai 官方已有 Temporal/DBOS/Prefect durable 集成，docs 必须正面回答"为什么自研 checkpoint 层而不用 DBOS"——面试必问 |
| Sandboxing / credential scoping | ★☆☆☆☆ | #4 #5 #6 修复前基本不成立；加分项：non-root、no-new-privileges、seccomp profile、真正的 proxy 凭证注入 |
| Scaling / cold-start / cost | ★☆☆☆☆ | #7 cost 恒 0；#10 warm pool 死代码；没有任何 benchmark 数字 |
| Orchestration (K8s) | ★★☆☆☆ 骨架齐 | #17；没有 kind 上的 CI 验证 |
| Logfire + MCP observability | ★☆☆☆☆ | #9 #19——对 Pydantic 最重要、目前最薄弱 |
| Pydantic AI 深度集成 | ★★☆☆☆ | #1 直接崩；没用 TestModel/FunctionModel、官方消息序列化、MCP toolset；durable wrapper "可贡献回上游"的说法需要真的按上游 API 设计 |

---

## 建议修复顺序

1. **P0 (1→2→3→4)**：让端到端流程跑通
2. **#11**：让核心演示在 CI 可验证
3. **#5/#6**：安全故事成立
4. **#7/#8**：durable/cost 名副其实
5. **#9/#19**：Logfire/MCP 做真
6. **P2/P3 清尾**：正确性、工程化、门面

每修一项都把 README/docs 里对应的宣称核对一遍，做不到的改成 roadmap——对这份求职来说，"宣称的每一句都经得起验证"比多一个功能重要得多。
