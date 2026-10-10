# 项目接续说明（给下一个对话看的第一份文件）

> **怎么用**：新开一个对话，第一句就说"先读 `docs/state.md`，然后我们继续做 X"。
> 这份文件就是项目状态的事实来源，读完它不需要再通读代码或翻长对话历史。
>
> **谁维护**：AI 在每个阶段收尾时更新（对应"每完成一个阶段就更新它"的惯例）。
> 内容要短、要是事实。**不要**往这里写"大概""可能"——写不准的就标 TODO 并说明去哪查。

## 1. 这是什么项目

AgentCode：一个可扩展的 Python 智能体框架（ReAct / Plan-and-Solve / Reflection / Coding），
带一个只给结果的本地网页。**《软件工程》作业的方向是"测试生成 Agent"**
（为源码生成 pytest 用例并真的跑通），但代码在按"能收费的产品"这条线往下推。

| 事实 | 值 |
| --- | --- |
| 仓库 | https://github.com/Ling345/myagent（`main` 是唯一长期分支） |
| 本地路径 | `D:\agent\agentcode-homework1` |
| Python | `D:\Anaconda\python.exe`（3.13）；CI 还跑 3.10 / Windows 3.13 |
| 数据库 schema | **v8**（代码里的最新迁移；老库在下次打开时自动升上来。迁移只增不改、逐条事务、幂等，见 `agentcode/storage/migrations.py`） |
| 代码量 | `agentcode/` 约 1.2 万行 Python；70 个测试文件 |
| 测试 | **873 项全部离线**（不打真实模型、不联网、不发行邮件；其中 1 项"POSIX 权限位"只在 Linux 上跑） |
| 提交 | 88 次；PR #1–#17 已合并（#2 是重复提交，已关闭未合并） |
| 远程状态 | 只保留 `main`；功能分支合并后即删 |

## 2. 模块地图（`agentcode/`）

| 文件 | 职责 | 什么时候改它 |
| --- | --- | --- |
| `cli.py` | 全部命令入口：`run/web/open/user/billing/backup/audit/trash/notify/token/api/list/config` | 加命令 |
| `config.py` | `Settings`（所有 `AGENT_*` / `LLM_*` 环境变量的唯一出口）+ `masked()` 脱敏展示 | 加配置项 |
| `accounts.py` | 账号库：账号、用量账本、订单、审计、通知台账、API 令牌、API 任务台账 | 加表/加字段（**先加迁移**） |
| `storage/migrations.py` | 版本化迁移（当前 v8） | 任何 schema 变化 |
| `lifecycle.py` | 数据生命周期：导出、注销、留存清理、**定期杂活**（回收站/审计/到期提醒/API 任务） | 定期任务 |
| `plans.py` / `billing.py` / `pricing.py` | 套餐定义、订单与开通、成本换算 | 计费 |
| `metrics.py` / `alerts.py` | Prometheus 指标（`/metrics`）与阈值告警（webhook） | 观测 |
| `audit.py` | 审计策略层：动作名、敏感字段抹除、绝不阻断主流程 | 新动作留痕 |
| `trash.py` | 回收站：会话与代码目录的软删除、恢复、彻底删、过期清理 | 删除相关 |
| `notify.py` | 用户通知：webhook / SMTP 两个渠道、五类事件、去重 | 通知相关 |
| `tokens.py` | API 令牌：生成、认证、吊销（只存哈希） | 对外认证 |
| `backup.py` | 备份与恢复演练（一致性快照 + 真打开库校验） | 备份相关 |
| `testgen.py` | 批量补测试（`agentcode test-gen`，作业方向） | 作业相关 |
| `web/server.py` | HTTP 服务：网页 API、静态资源、`/v1/*` 对外 API、鉴权与限流 | 接口 |
| `callbacks.py` | 任务完成回调：地址校验（防 SSRF）、HMAC 签名、投递与退避重试 | 回调相关 |
| `web/runner.py` | 把一次 agent 运行变成事件流（status/step/answer/error），sync 与 async 共用 | 运行链路 |
| `web/sessions.py` | 会话仓库（按账号分目录、磁盘持久化、回收站软删除） | 会话 |
| `web/static/` | `index.html` / `app.js` / `style.css`（浅蓝流动背景、面板式侧栏） | 界面 |

## 3. 已完成（按 PR）

| PR | 内容 |
| --- | --- |
| #1 | 套餐、用量账本与开通流程（能收费的地基） |
| #3 | 指标与阈值告警（`/metrics` + webhook） |
| #4 | 文件上传 + 代码目录按用户隔离 |
| #5 | 部署：应用镜像 + `docker-compose` + HTTPS + `docs/deploy.md` |
| #6 | 修复：登出时没读完请求体导致连接被中止 |
| #7 | 隐私政策 + 数据导出与注销 |
| #8 | `agentcode test-gen` + GitHub Action（作业方向落地） |
| #9 | 账本分开记输入/输出 token + `billing costs` 毛利表 |
| #10 | 备份与恢复演练（`agentcode backup`） |
| #11 | 回收站（软删除）+ 操作审计日志（迁移 v4） |
| #12 | 通知邮箱 + 通知（webhook / SMTP，迁移 v5） |
| #13 | 对外 API：API 令牌 + `POST /v1/run` / `GET /v1/me`（迁移 v6） |
| #14 | 对外 API 的异步任务与幂等键（任务台账落库，迁移 v7） |
| #15 | 接续说明 `docs/state.md` + 防过期测试（本文件） |
| #16 | 更新接续说明里的快照数字（827 项用例 / 85 次提交 / PR 到 #15） |
| #17 | 任务完成回调：签名 + 失败重试 + 防 SSRF（迁移 v8，`docs/api.md` 第 5 节） |

更早的阶段（框架本体、网页、上下文记忆、会话管理、账号体系、容器化执行、限流与
多 key 熔断、coding 智能体）在 PR #1 之前直接推到 main，没有单独 PR。

## 4. 对外 API 现状（`docs/api.md` 是用户文档）

- 认证：`Authorization: Bearer agk_...`，令牌只存 sha256，明文只在创建时出现一次；
- `POST /v1/run`：同步返回结果；`{"async": true}` 则 `202` + `run_id`；
- `GET /v1/runs/<run_id>`、`GET /v1/runs`：查状态与结果（**结果在库里，重启不丢**）；
- 幂等：请求头 `Idempotency-Key`，同键不重跑、不重复扣费；
- 回调：异步任务多给一个 `callback_url`，跑完主动 POST 结果（HMAC-SHA256 签名、
  按退避重试且跨重启补发、地址逐条查内网）；投递状态在 `GET /v1/runs/<id>` 的
  `callback` 块里，收不到时看 `agentcode api callbacks <账号>`；
- `GET /v1/me`：套餐、今日用量、剩余额度；
- 计费：与网页/CLI **同一本账**（`usage_ledger`），按输入/输出分开记；
- 开关：`AGENT_API_ENABLED=false` 可整体关掉（`/v1/*` 回 404）。

## 5. 还没做的（下一步候选）

| 事 | 为什么值得做 | 备注 |
| --- | --- | --- |
| 流式接口 | 网页有 SSE，对外没开 | 适合聊天式集成 |
| 细分权限 | 现在一把令牌能调全部接口 | 例如只读令牌 |
| 邮箱"点链接验证" | 现在只做格式校验，填错也存得下 | 需要稳定发信能力 |
| 真支付接入 | 订单现在是人工确认（`billing confirm`） | 与定价策略相关，用户之前说先放一放 |
| 团队协作 | `team` 套餐目前只是额度更大，没有成员管理 | 卖团队客户时迟早要有 |
| ICP 备案 / 数据出境评估 | 合规前置条件 | 非代码事项，见 `docs/data-and-privacy.md` |

## 6. 每次改动的标准动作（别省）

1. **先写失败测试**（`pytest tests/xxx.py -q` 看到红），再写实现；
2. 全量：`D:\Anaconda\python.exe -m pytest -q`（看**退出码**，汇总行在本机终端不显示）；
3. Linux 一路：`wsl -d Ubuntu -u wyh -- bash /mnt/d/Docker-setup/wsl_ci_check.sh`；
4. 干净检出：`git archive --format=zip -o x.zip HEAD` → 解压 → 在里面跑 pytest；
5. **真机演练**：起真实服务，用真 `curl` / 真浏览器（CDP）走一遍，而不是"应该能跑"；
6. 提交（信息写进文件用 `-F`，不要塞 `-m`）→ 开 PR → 等 CI 绿 → `merge_pr.ps1 <号> rebase` → 删分支；
7. **更新这份文件**（版本号、PR、下一步、坑）。

## 7. 命令速查

```powershell
# 测试与服务
D:\Anaconda\python.exe -m pytest -q
D:\Anaconda\python.exe -m agentcode open                    # 起网页（本机 8000）
D:\Anaconda\python.exe -m agentcode config                  # 看配置（密钥脱敏）

# 运营
D:\Anaconda\python.exe -m agentcode user list
D:\Anaconda\python.exe -m agentcode token create <账号> --name CI --days 90
D:\Anaconda\python.exe -m agentcode api list <账号>
D:\Anaconda\python.exe -m agentcode api callbacks <账号>     # 回调投递到了哪一步
D:\Anaconda\python.exe -m agentcode audit list --action-prefix api.
D:\Anaconda\python.exe -m agentcode trash list <账号>
D:\Anaconda\python.exe -m agentcode notify list
D:\Anaconda\python.exe -m agentcode backup create
D:\Anaconda\python.exe -m agentcode backup verify <包>
D:\Anaconda\python.exe -m agentcode billing costs

# 辅助脚本（仓库外，D:\Docker-setup\）
open_pr.ps1 / merge_pr.ps1 / check_pr.ps1 / check_ci.ps1
launch_chrome_cdp.ps1 + verify_*.mjs（真浏览器验证）
```

## 8. 环境与踩过的坑（省时间用）

- **Windows 控制台是 GBK**：输出符号只能用 `￥ √ × – →` 这类，**不要**用 `✓ ✗`；源码里也别写。
- **`.ps1` 必须纯 ASCII**（PowerShell 5.1 按 ANSI 读，中文注释会破坏解析）；
  中文的 PR 标题/正文单独放 UTF-8 文件。
- **提交信息别塞 `-m`**：多行中文会被 shell 拆坏（我踩过一次，生成过 `name=x` 的提交，
  用 `git commit --amend -F <文件>` 修的）。
- **到 GitHub 的网络不稳**：push/pull/fetch 一律写重试循环（`for` + `Start-Sleep`）。
- **`pytest -q` 的汇总行在本机终端不显示**：看退出码，或先 `--collect-only` 数用例。
- **静态资源路径是 `/static/app.js`**，不是 `/app.js`（写错会 404，别误判成"缓存"）。
- **浏览器验证要"登录后重新加载"**：登录是 fetch 调的，界面不会自己切到主界面。
- **清理临时目录用 Python**（`shutil.rmtree`）：`Remove-Item -Recurse -Force` 会被策略拦。
- **面板尺寸会随内容变化**：改完 UI 一定用 CDP 量 `getBoundingClientRect`，
  确认元素真的落在视口内（我靠这个抓到过"令牌区被挤到可视区外"）。
- **回调不能用 `urllib` 发**：它会读 `http_proxy` 环境变量，等于绕开我们的地址检查；
  也不跟随重定向（跳过去的目标没校验过）。所以投递走 `http.client` 直连。
- **回调是"至少一次"**：重试、或两个进程同时捞到同一条，都可能重复投递。
  收方要按 `run_id` 幂等——`docs/api.md` 里写明了这一点。
- **回调的审计只记主机名**：webhook 地址的密钥常藏在路径（Slack）或 query 里，
  完整地址只留在库里，不进审计、不进接口响应。

## 9. 这个项目的约定（别破坏）

- 所有注释、提示、错误信息、终端输出**用中文**；页面**只给结果**，不显示推理过程与来源。
- 密钥只从 `.env` 读（真实 `.env` 在 `D:\agent\.env`，不进仓库）；`traces/` 也不进仓库。
- **每用户隔离**：会话目录、代码目录、配额、令牌、回收站都按账号分。
- **失败不静默**：审计/通知/落盘这类旁路出错要打日志或记台账，绝不明说"成功"
  （例如没配通知渠道就记 `queued`，不显示"已发送"）。
- 迁移只增不改；新表/新列一律走 `agentcode/storage/migrations.py`。

## 10. TODO（写不准、需要核实的地方）

- PR #1–#14 的合入时间见 GitHub；本文件只记内容，不记时间。
- 官方用量/计费口径没写进这里（网络受限抓不到官方页面）：要看准确数字请查官方
  Codex 用量与定价页，别引用本文件的估算。
