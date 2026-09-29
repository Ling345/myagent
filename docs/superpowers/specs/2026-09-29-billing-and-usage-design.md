# 套餐、用量账本与开通流程 设计文档

> 状态：已确认（方案：套餐目录 + 用量账本 + 手动开通）
> 日期：2026-09-29
> 位置：`D:\agent\agentcode-homework1`

## 1. 问题

现在的账号体系能做「限制」，但做不了「收费」：

- `plan` 只是一个自由字符串（`"free"` / `"owner"`），**没有套餐定义**，也就没有「买什么」这一层；
- 额度是账号上的一个裸数字 `daily_token_limit`，和套餐没有关系，换套餐要手工改数字；
- 用量只有 `usage(account_id, day, tokens, calls)` 这一张日计数器：查得到「今天用了多少」，
  查不到「这笔用量是哪次运行产生的」，也对不了月账；
- 没有订单、没有到期时间——**收钱这件事没有落脚点**；
- schema 只有 `CREATE TABLE IF NOT EXISTS`，**加表加列没有路径**。
  线上库里已经有真实账号，直接改结构会原地炸掉。

结论：瓶颈不在模型调用，而在「谁能用多少、用在哪、什么时候到期、钱怎么进来」这套账没记。

## 2. 范围

本次交付六件事：

1. `agentcode/plans.py`：套餐目录（额度、频率、并发、能否执行代码、月价）；
2. `agentcode/storage/migrations.py`：给账号库加版本号与顺序迁移；
3. `agentcode/accounts.py`：账本表、订单表、账号套餐字段；配额判断改走套餐；
4. `agentcode/billing.py`：`PaymentProvider` 接口 + `ManualProvider` + 开通/续期；
5. 接口与界面：`/api/plans`、`/api/billing`、`/api/billing/checkout`，网页加「套餐与用量」面板；
6. CLI：`agentcode user plan`、`agentcode billing orders|grant`。

**不在本次范围**：接真实支付渠道（微信/支付宝/Stripe）、发票、自动续费、多币种结算、
按量阶梯计价、团队子账号。这些都留在接口后面，以后实现同一个 `PaymentProvider` 即可。

## 3. 一处需要定死的歧义

立项时表述有歧义，这里定死：

- **额度按「日」重置**（次日 UTC 零点），数字就是确认过的 2 万 / 20 万 / 100 万 / 500 万；
- **套餐按「月」购买**，即有效期一个月，`plan_expires_at` = 购买日 + N 个月；
- **频率按「分钟」**（滑动窗口），用于防突发；
- 「本月累计用量」只作为**展示与对账**，不设上限。

这样「日额度」防的是单日烧穿，「月套餐」卖的是持续时间，两者不冲突。
如果实际想要的是「月总额度（比如基础版 600 万/月）」，那要改的是数据模型，不是数字。

## 4. 套餐目录

| 套餐 | 日 token | 每分钟请求 | 并发 | 代码执行 | 会话数 | 月价 |
| --- | --- | --- | --- | --- | --- | --- |
| `free` 免费 | 2 万 | 10 | 1 | 否 | 5 | 0 |
| `basic` 基础 | 20 万 | 30 | 2 | 是 | 20 | ¥29 |
| `pro` 专业 | 100 万 | 60 | 4 | 是 | 50 | ¥99 |
| `team` 团队 | 500 万 | 120 | 8 | 是 | 200 | ¥399 |
| `owner` 内部 | 不限 | 不限 | 不限 | 是 | 不限 | 不可售 |

定义成冻结数据类，放在代码里：

```python
@dataclass(frozen=True)
class Plan:
    name: str            # free / basic / pro / team / owner
    title: str           # 中文名，展示用
    daily_tokens: int    # <= 0 表示不限
    per_minute: int      # <= 0 表示不限
    max_concurrent: int  # <= 0 表示不限
    allow_code_tools: bool
    max_sessions: int    # <= 0 表示不限
    price_cents: int     # 月价（分），0 = 免费
    currency: str = "CNY"
    sellable: bool = True  # False = 内部套餐，不出现在对外列表
```

两条硬规则：

1. **未知套餐名一律回落 `free`**。坏字符串绝不能换来无限额度。
2. `owner` 的 `sellable=False`，对外列表里不出现，只能由管理员授予。

账号上的 `limit_override` 若不为空，则覆盖套餐的日额度（客服补偿、临时提额用）。

## 5. 数据模型与迁移

### 5.1 迁移机制

```python
@dataclass(frozen=True)
class Migration:
    version: int
    description: str
    apply: Callable[[sqlite3.Connection], None]


def run_migrations(conn: sqlite3.Connection) -> int:
    """把库升到最新版本，返回最终版本号。"""
```

版本号存在 `schema_meta(key TEXT PRIMARY KEY, value TEXT)` 里。规则：

- **只增不改**：加列用 `ALTER TABLE ADD COLUMN`，不删列、不改类型；
- 每条迁移**单独一个事务**，失败就回滚，版本停在上一条，库仍可用；
- **幂等**：以版本号为准，跑过的不再跑；
- v1 用 `CREATE TABLE IF NOT EXISTS` 描述现状：老库跑它是 no-op，只是把版本从 0 抬到 1。

### 5.2 `accounts` 新增列

| 列 | 类型 | 说明 |
| --- | --- | --- |
| `plan_expires_at` | TEXT NULL | 到期时间（UTC ISO8601）；NULL = 不过期 |
| `plan_started_at` | TEXT NULL | 本次套餐生效时间 |
| `limit_override` | INTEGER NULL | 账号级日额度覆盖；NULL = 跟随套餐 |

原 `daily_token_limit` **保留但不再读写**（留着回滚保命）。

> **行为变化，需要写进 README**：升级后额度以套餐为准，账号上原先手工设的那个数字不再生效。

### 5.3 新表 `usage_ledger`（一行 = 一次运行）

```sql
CREATE TABLE IF NOT EXISTS usage_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT NOT NULL,
    day TEXT NOT NULL,          -- UTC 日期，汇总用
    created_at TEXT NOT NULL,   -- 精确到秒
    tokens INTEGER NOT NULL,
    calls INTEGER NOT NULL,
    run_id TEXT,                -- 关联一次运行
    note TEXT
);
CREATE INDEX IF NOT EXISTS idx_ledger_account_day ON usage_ledger(account_id, day);
```

### 5.4 新表 `orders`

```sql
CREATE TABLE IF NOT EXISTS orders (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    plan TEXT NOT NULL,
    months INTEGER NOT NULL,
    amount_cents INTEGER NOT NULL,
    currency TEXT NOT NULL DEFAULT 'CNY',
    status TEXT NOT NULL,       -- pending / paid / cancelled / refunded
    provider TEXT NOT NULL,     -- manual（以后会有 wechat / stripe ...）
    provider_ref TEXT,          -- 第三方支付单号
    created_at TEXT NOT NULL,
    paid_at TEXT,
    note TEXT
);
CREATE INDEX IF NOT EXISTS idx_orders_account ON orders(account_id, created_at DESC);
```

### 5.5 老数据搬迁

- `usage` 的历史行全部灌进账本：`created_at` 取当天 `00:00:00`，`run_id` 留空，`note='历史迁移'`；
- 此后 `usage` 表**只读**，不再写入（保留是为了回滚，不是为了查询）。

## 6. 用量账本

一次运行结束 → 写**一条**账本记录（`run_id` 就是运行注册表里的编号，串得起来）。

查询都从账本算，靠 `(account_id, day)` 索引：

| 方法 | 用途 |
| --- | --- |
| `usage_today(account_id)` | 今日 (tokens, calls) —— 配额判断用 |
| `usage_between(account_id, start, end)` | 任意区间汇总 —— 月账、报表 |
| `usage_history(account_id, limit)` | 近 N 天，画曲线 |
| `export_usage_csv(account_id)` | 导出，给用户对账 |

**单一事实来源**：账本是唯一被写入的用量表。`usage` 只是历史遗留。

## 7. 订单与开通

```python
class PaymentProvider(Protocol):
    name: str

    def create_order(self, account: Account, plan: Plan, months: int) -> Order: ...
    def confirm(self, order_id: str, reference: str | None = None) -> Order: ...
```

- `ManualProvider`：`create_order` 只落一条 `pending` 订单；`confirm` 把订单标成 `paid`
  并**触发开通**。管理员收到钱之后用 CLI 确认。
- 以后接微信/支付宝：实现同一个协议，`create_order` 返回支付二维码/链接，
  `confirm` 由支付回调调用。**上层业务代码一行都不用改。**

`BillingService` 对外提供：

| 方法 | 说明 |
| --- | --- |
| `plans()` | 对外可售套餐列表 |
| `checkout(account, plan_name, months)` | 下单（校验套餐可售、月数 1–12） |
| `confirm(order_id, reference)` | 确认到账 → 延长套餐（**幂等**，重复确认不重复开通） |
| `grant(account, plan_name, months)` | 管理员直接开通/续期，不经过订单 |
| `effective_plan(account)` | 有效套餐：到期就回落 `free` |
| `my_billing(account)` | 套餐、到期、今日/本月用量、订单历史 |

**续期算法**：若当前套餐未过期且是同一个套餐，从原 `plan_expires_at` 往后加；
否则从今天起算。过期后购买 = 重新开始，不追溯。

## 8. 配额与限流的判断顺序

每次接任务时按顺序查，先便宜的先查：

1. 账号是否被停用 → 拒绝；
2. 解析**有效套餐**（含到期回落）；
3. 频率 + 并发（数值来自套餐，`UsageGuard` 增加按次覆盖参数）；
4. 今日额度（`limit_override` 优先，否则套餐的 `daily_tokens`；<=0 表示不限）→ 拒绝；
5. 执行时：套餐不允许代码执行 → 不下发代码工具（与现有 `AGENT_ALLOW_CODE_TOOLS` 取「与」）。

拒绝时给中文原因，并**告诉用户怎么办**（「今日额度已用完（…），明天 UTC 零点重置；升级套餐可提高额度」）。

## 9. 接口与界面

| 接口 | 说明 |
| --- | --- |
| `GET /api/plans` | 可售套餐列表（价格、额度、是否允许代码执行） |
| `GET /api/billing` | 我的套餐、到期时间、今日/本月用量、订单历史 |
| `POST /api/billing/checkout` | 下单，返回订单号与「待支付」说明 |

管理动作（确认到账、直接开通、改套餐）**只走 CLI，不开 Web 接口**——
否则等于给自己留了一条自助提权的路。

网页加一个「套餐与用量」面板，从账号区域点进去：

- 当前套餐名 + 到期时间（免费套餐显示「永久」）；
- 今日用量进度条（已用 / 额度），超额时变红；
- 近 7 天用量小柱状图（纯 CSS，不加图表库）；
- 套餐卡片：价格、额度、升级按钮；手动模式下点完提示「订单已创建，请联系管理员完成支付」，并显示订单号；
- 订单历史（时间、套餐、金额、状态）。

面板保持克制，不抢主界面的位置——主界面只给结果，这是既有约定。

## 10. 边界与错误处理

| 情况 | 行为 |
| --- | --- |
| 套餐名未知（数据被改坏） | 回落 `free`，并记一条告警日志 |
| 套餐已过期 | 回落 `free`，`my_billing` 里明确显示「已于 X 过期」 |
| 重复确认同一订单 | 幂等：第二次直接返回，不再延长套餐 |
| 已取消/已退款订单再确认 | 报错，不改变套餐 |
| 下单不存在的套餐 / 内部套餐 | 报错「该套餐不可购买」 |
| 月数越界 | 只允许 1–12 |
| 迁移失败 | 回滚到上一条，库仍可打开；启动时报错并提示先备份 |

## 11. 测试策略

全部离线，不联网、不调真实模型。

**迁移（最重要的一组）**

1. 造一个 v1 老库（旧结构 + 两个账号 + 若干日用量）→ 跑迁移 → 账号还在、密码还能验、
   原有用量进了账本、`schema_meta` 版本正确；
2. **再跑一次迁移**：幂等，数据不变；
3. 全新空库跑迁移 → 结构完整；
4. 拿本项目真实的 `traces/agentcode.db` 拷贝跑一遍，确认那两个账号升级后能登录。

**套餐与配额**：目录取值、未知回落、`limit_override` 生效、`owner` 不限、
过期回落、停用拒绝、日额度用尽拒绝。

**账本**：记录、今日汇总、区间汇总、CSV 导出。

**订单**：下单 → 确认 → 开通 → 到期时间正确；重复确认幂等；取消后确认报错；
续期在同一套餐上叠加、跨套餐从今天起算。

**HTTP**：`/api/plans`、`/api/billing`、`/api/billing/checkout` 的鉴权与正常路径。

**端到端**：免费账号把日额度跑穿 → 被拒 → 管理员 `grant basic` → 同一账号立刻能继续跑。

## 12. 兼容性

- `AGENT_DB_PATH` 不变，老库原地升级，不需要手工操作；
- 现有 `owner` 账号：不限量、不过期，行为不变；
- 现有 `usage_history` / `remaining_tokens` 等公开方法的返回结构保持不变，内部改为读账本；
- CLI 现有子命令（`user add/list/limit/passwd/disable/enable`）全部保留；
- 新配置项一律有默认值，`.env` 不填也能跑。
