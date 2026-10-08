# 对外开放 API

网页是给**人**用的门；这套接口是给**程序**用的门：脚本、CI、内部工具、机器人
都能带着令牌调用同一个智能体，拿回结果，用量记在同一个账本上。

## 1. 先拿一把令牌

两种方式，二选一：

| 谁 | 怎么做 |
| --- | --- |
| 用户自己 | 网页左下角「我的数据」→「API 令牌」→ 填个名字 → 新建 |
| 运营方 | `agentcode token create <账号> --name CI --days 90` |

**明文只显示这一次**。库里只存哈希（sha256），忘了就吊销重建——
这样即使备份、日志、误提交里漏了库，别人也拿不到能用的凭据。

建议**按用途分开建**：`我的脚本`、`公司 CI`、`那个插件` 各一把。
哪天某一把泄露了，只吊销那一把，别的照常用。

## 2. 认证

每个请求都要带上：

```
Authorization: Bearer agk_xxxxxxxxxxxxxxxxxxxxxxxx
```

令牌无效/过期/被吊销/账号被停用，都会回 `401`：

```json
{"error": "令牌无效或已失效。请在网页「我的数据」里生成 API 令牌，然后用请求头 Authorization: Bearer <令牌> 调用。"}
```

## 3. 接口

### `POST /v1/run` —— 跑一个任务

请求体：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `task` | 是 | 要交给智能体的任务描述 |
| `agent` | 否 | `react`（默认）/ `plan_and_solve` / `reflection` / `coding` / `test_gen` / `echo` |
| `session_id` | 否 | 带上它就**延续同一个会话**（上下文记忆会在两次调用之间保留） |
| `max_steps` | 否 | 本轮最多跑几步；不填按服务端配置 |
| `async` | 否 | `true` 时立刻返回 `202` + `run_id`，任务在后台跑（见第 4 节） |

请求头还可以带一个 **`Idempotency-Key`**（见第 5 节）：网络抖动重发时，
同一个键只会跑一次、只扣一次费。

```bash
curl -sS -X POST "$AGENTCODE_URL/v1/run" \
  -H "Authorization: Bearer $AGENTCODE_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"agent": "coding", "task": "给 utils.py 补 pytest 测试", "session_id": "ci-42"}'
```

返回（`200`）：

```json
{
  "run_id": "9f2c1d4a7b30",
  "agent": "coding",
  "llm_mode": "openai",
  "session_id": "ci-42",
  "success": true,
  "answer": "已生成 tests/test_utils.py，5 项测试全过。",
  "artifacts": [{"path": "tests/test_utils.py", "bytes": 812}],
  "usage": {"total_tokens": 1834, "prompt_tokens": 1502, "completion_tokens": 332},
  "elapsed_ms": 8421
}
```

智能体没跑成功时返回 `502`，格式一样，只是 `success=false` 并多一个 `error` 字段
——**该扣的额度照扣**（模型确实调用过），这一点和网页一致。

### `GET /v1/me` —— 查自己的额度和套餐

```bash
curl -sS "$AGENTCODE_URL/v1/me" -H "Authorization: Bearer $AGENTCODE_TOKEN"
```

```json
{
  "account": "alice",
  "plan": "basic",
  "plan_title": "基础版",
  "plan_expires_at": "2026-11-08T00:00:00+00:00",
  "daily_token_limit": 200000,
  "used_today": 15320,
  "calls_today": 12,
  "remaining": 184680,
  "unlimited": false,
  "limits": {"per_minute": 30, "max_concurrent": 2},
  "token": {"name": "我的脚本", "prefix": "agk_ab12cd", "last_used_at": "2026-10-08T06:20:11+00:00"}
}
```

建议脚本开跑前先看一眼 `remaining`，省得跑到一半拿到 `402`。

## 4. 异步：长任务不要干等

写代码这类任务动辄几十秒，同步接口很容易超过客户端超时。加 `"async": true`：

```bash
curl -sS -X POST "$AGENTCODE_URL/v1/run" \
  -H "Authorization: Bearer $AGENTCODE_TOKEN" -H "Content-Type: application/json" \
  -d '{"agent": "coding", "task": "给 utils.py 补测试", "async": true}'
# 202 {"run_id":"9f2c…","status":"running","poll":"/v1/runs/9f2c…"}

curl -sS "$AGENTCODE_URL/v1/runs/9f2c…" -H "Authorization: Bearer $AGENTCODE_TOKEN"
# 跑完：{"status":"succeeded","success":true,"answer":"…","usage":{…},"finished_at":"…"}
```

- `GET /v1/runs/<run_id>`：查状态与结果。`status` 是 `running` / `succeeded` / `failed`；
- `GET /v1/runs?limit=20`：列最近的任务（不带结果正文，适合做后台列表）；
- **结果存在数据库里**，所以服务重启之后照样取得到（不是内存里的临时状态）；
- 保留 `AGENT_API_RUNS_DAYS` 天（默认 7），过期就清掉；
- 服务重启或线程意外中断留下的 `running` 记录，会被定期清理标成 `failed`
  并写明原因——不会让你一直轮询一个死任务。

## 5. 幂等键：重发不等于重跑

客户端超时之后最常见的做法是"原样重发一次"。带 `Idempotency-Key` 就不会变成
"跑两次、扣两次"：

```bash
curl -sS -X POST "$AGENTCODE_URL/v1/run" \
  -H "Authorization: Bearer $AGENTCODE_TOKEN" \
  -H "Idempotency-Key: ci-42-$GITHUB_RUN_ID" \
  -H "Content-Type: application/json" \
  -d '{"agent": "coding", "task": "给 utils.py 补测试"}'
```

同一个账号 + 同一个键：

| 那一单的状态 | 你会收到 |
| --- | --- |
| 已经跑完 | 原来的结果，多一个 `"idempotent_replay": true` |
| 还在跑 | `202` + 同一个 `run_id` + `poll`，**不会再开一单** |

键由你自己定，建议带上业务上下文（比如 CI 的 run id）。**不要**用随机数——
那就等于没带。同一个键最多保留 `AGENT_API_RUNS_DAYS` 天。

## 6. 错误码

| 状态码 | 什么意思 | 该怎么办 |
| --- | --- | --- |
| `400` | 请求不合法（`task` 空、`agent` 不存在） | 改请求；错误信息会写清楚 |
| `401` | 令牌无效/过期/被吊销 | 换一把；账号被停用也会是 401 |
| `402` | 今天的额度用完了 | 等 UTC 零点重置，或升级套餐 |
| `404` | 服务端关掉了对外 API（`AGENT_API_ENABLED=false`） | 找运营方 |
| `429` | 请求太频 / 并发太多 | 退避重试；响应里有 `Retry-After` |
| `502` | 任务没跑成功（模型侧错误、超出单次预算等） | 看 `error` 字段；可以重试 |

## 7. 限流与计费

- **计费**：按这次运行真实消耗的 token 记账，输入/输出分开记（两者单价差好几倍）。
  和网页、CLI 走的是**同一本账**——`agentcode billing costs` 里能一起看到。
- **限流**：按账号算，不是按 IP。每把令牌背后是同一个账号，所以多建几把不会
  "多出额度"。
- **上限**：每日 token 额度、每分钟请求数、并发数、单次运行 token 预算，
  都在服务端 `.env` 里配（见 `README.md` 的「配置说明」）。

## 8. 脚本示例

### Python

```python
import os, requests

URL = os.environ["AGENTCODE_URL"]
TOKEN = os.environ["AGENTCODE_TOKEN"]          # 别写死在代码里
headers = {"Authorization": f"Bearer {TOKEN}"}

me = requests.get(f"{URL}/v1/me", headers=headers, timeout=10).json()
if me["remaining"] != -1 and me["remaining"] < 5000:
    raise SystemExit(f"额度快没了：还剩 {me['remaining']} token")

resp = requests.post(
    f"{URL}/v1/run",
    headers=headers,
    json={"agent": "coding", "task": "给 utils.py 补 pytest 测试"},
    timeout=300,                                # 写代码类任务会跑几十秒
)
data = resp.json()
if resp.status_code != 200:
    raise SystemExit(f"调用失败（{resp.status_code}）：{data.get('error')}")
print(data["answer"], data["usage"])
```

### GitHub Actions（在别人的仓库里用你的服务）

```yaml
- name: 让 AgentCode 补测试
  env:
    AGENTCODE_URL: ${{ secrets.AGENTCODE_URL }}
    AGENTCODE_TOKEN: ${{ secrets.AGENTCODE_TOKEN }}
  run: |
    curl -sS -X POST "$AGENTCODE_URL/v1/run" \
      -H "Authorization: Bearer $AGENTCODE_TOKEN" \
      -H "Content-Type: application/json" \
      -d '{"agent": "test_gen", "task": "给 src/ 下的模块补 pytest 测试"}' \
      | tee result.json
```

令牌放进仓库的 **Secrets**，不要写进 workflow 文件。

## 9. 安全

- 令牌等同密码：**只放环境变量或 secret**，不要提交进仓库、不要贴进聊天记录。
- 泄露了就**立刻吊销**（网页「我的数据」或 `agentcode token revoke`），
  重建一把新的即可，不影响历史记录。
- 想限制寿命就建带 `--days` 的令牌（比如给外包同学一把 30 天的）。
- 每一次调用都会写审计：**哪个令牌、在什么时候、调了哪个智能体、花了多少 token**
  （`agentcode audit list --action-prefix api.`）。审计里永远没有令牌明文。
- 对外提供服务时请务必用 **HTTPS**：明文 HTTP 会让令牌在网络里裸奔。

## 10. 现在还没有的（诚实列一下）

| 缺什么 | 影响 |
| --- | --- |
| 任务完成的回调 | 异步任务要自己轮询（`GET /v1/runs/<id>`）；我们不会主动通知你的服务 |
| 流式接口 | 网页有 SSE，对外还没开；需要边跑边看的话请提需求 |
| 按接口细分的权限 | 一把令牌能调全部接口；要"只读令牌"之类的还得再做 |

这些都在路线图上，但**没做就是没做**——上面这些坑请先自己绕过去。
