# 部署到服务器

这份文档把 AgentCode 从"在你电脑上跑"变成"别人能访问的服务"。全部基于 Docker，
不需要在服务器上装 Python。

## 0. 你需要先有的东西

| 东西 | 说明 |
| --- | --- |
| 一台 Linux 服务器 | 1 核 2G 起步就够。沙箱和模型调用都不吃本地 CPU |
| 一个域名 | 想用 HTTPS 就需要（Caddy 会自动申请证书）。只用 IP 也能跑 |
| 一个 LLM API key | DeepSeek / 通义 / 任何 OpenAI 兼容接口都行 |

## 1. 服务器上装 Docker

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER   # 重新登录一次生效
```

## 2. 拉代码、建沙箱镜像、配密钥

```bash
git clone https://github.com/Ling345/myagent.git
cd myagent

# 沙箱镜像要单独建——它是"被应用调用"的另一个镜像，不在 docker-compose.yml 里
docker build -t agentcode-sandbox:1.0 docker/sandbox

# 密钥放 deploy/，别放项目根目录（根目录的 .env 会遮住你本地的开发配置）
cp .env.example deploy/.env
nano deploy/.env
```

`deploy/.env` 里至少要填这几项：

```dotenv
LLM_API_KEY=你的密钥
LLM_BASE_URL=https://api.deepseek.com
LLM_MODEL_ID=deepseek-chat

# 登录态签名密钥。不填的话每次重启用户都要重新登录
AGENT_SECRET_KEY=随便一串足够长的随机字符

# 可选：告警推送地址（飞书/钉钉/Slack 机器人都能收）
AGENT_ALERT_WEBHOOK=

# 可选：抓指标用的令牌（见第 5 节）
AGENT_METRICS_TOKEN=
```

## 3. 起服务

```bash
# 数据目录：必须是**绝对路径**，而且宿主机上是什么路径、容器里就是什么路径
# （原因见下面的"为什么路径必须一致"）
export AGENTCODE_DATA=/srv/agentcode/data

docker compose up -d
docker compose logs -f app
```

第一次启动时，如果账号库是空的，它会在日志里打印一个初始管理员账号和密码，
**只打印这一次**：

```
========================================================
已创建初始账号　用户名：admin　密码：EP2UCbBCed8R
请立刻记下这个密码，它只显示这一次。
========================================================
```

记下来，登录后立刻用 `agentcode user passwd admin` 改掉。

现在访问 `http://服务器IP:8000` —— 注意 compose 里端口是绑在 `127.0.0.1` 上的，
直接开防火墙放 8000 是访问不到的。要对外访问走第 4 节的 Caddy，或者把
`docker-compose.yml` 里的 `127.0.0.1:8000:8000` 改成 `8000:8000`（不推荐，
那样就没有 HTTPS 了）。

在容器里执行管理命令：

```bash
docker compose exec app python -m agentcode user add alice
docker compose exec app python -m agentcode user plan alice basic --months 1
docker compose exec app python -m agentcode billing orders
```

## 4. 域名 + HTTPS（可选，但公网强烈建议）

把域名解析到服务器，然后：

```bash
export AGENTCODE_DOMAIN=agent.example.com
docker compose --profile public up -d
```

Caddy 会自动申请证书、自动续期、并把 HTTP 跳转到 HTTPS。`deploy/Caddyfile` 里
已经把 SSE 的 `flush_interval` 关掉了——不关的话流式输出会被代理攒着不发，
页面要等很久才出结果。

## 5. 抓指标

指标端点是 `/metrics`（Prometheus 文本格式）。

**本地**（服务器上直接 curl）不用登录：

```bash
curl -s http://127.0.0.1:8000/metrics | head
```

**容器里不一样**：容器必须绑 `0.0.0.0`，而"绑回环才免登录"这条就失效了。
所以配一个令牌给抓取端用：

```dotenv
# deploy/.env
AGENT_METRICS_TOKEN=一串随机字符
```

```yaml
# prometheus.yml
scrape_configs:
  - job_name: agentcode
    static_configs:
      - targets: ["127.0.0.1:8000"]
    authorization:
      credentials: 一串随机字符
```

## 6. 数据在哪、怎么备份

`$AGENTCODE_DATA` 目录下面是全部状态：

| 路径 | 内容 |
| --- | --- |
| `agentcode.db` | 账号、密码哈希、套餐、用量账本、订单 |
| `web-sessions/` | 每个账号的会话记录（按账号 id 分目录） |
| `sandbox/` | 每个账号的代码工作目录（上传的文件、agent 生成的文件都在这） |
| `traces/` | 运行轨迹 |

**备份就是备份这个目录**：

```bash
tar czf agentcode-backup-$(date +%F).tar.gz -C /srv/agentcode/data .
```

数据库是有版本化的迁移机制的，升级时会自动升到最新版本（只增不改、逐条事务、
可重复执行）。但**迁移是单向的**——回滚代码不会回滚表结构，所以升级前先备份。

## 7. 升级与回滚

```bash
# 升级
tar czf backup.tar.gz -C $AGENTCODE_DATA .   # 先备份
git pull
docker compose build
docker compose up -d

# 回滚代码（表结构不会跟着回退，所以有备份才敢回滚）
git checkout <上一个提交>
docker compose build && docker compose up -d
```

## 8. 安全上的取舍（请认真看这一节）

**应用容器挂了宿主机的 `/var/run/docker.sock`。**

为什么必须挂：AgentCode 自己**不执行**用户代码。它是在需要跑代码时，通过宿主
Docker 去起一个一次性的沙箱容器（断网、只读根、非 root、限资源）。要能起
"兄弟容器"，它就得能调宿主 Docker——这是 Docker-outside-of-Docker 的标准做法。

代价是什么：**能调 docker.sock 就等于能控制宿主 Docker**，也就等于能起一个特权
容器、挂宿主机根目录。也就是说，如果应用进程本身被攻破，宿主机就没了。

现在这套代码的暴露面有多大：HTTP 处理器、模型输出（代码字符串、路径）——路径有
`resolve_in_root` 兜着，代码只在沙箱里跑。所以主要风险是"应用本身有漏洞"，
不是"用户能直接利用"。

**要更安全的话，有三个方向：**

| 做法 | 说明 |
| --- | --- |
| 加一层 docker socket 代理 | 比如 `tecnativa/docker-socket-proxy`，只放行 `containers` 相关的读写，别的一律拒绝。改动最小，收益明显 |
| 应用跑在宿主机上，不容器化 | 直接 `python -m agentcode web --host 127.0.0.1` 配 systemd；沙箱仍然用宿主 Docker。**没有 socket 挂载，也就没有这个风险**，而且不用装 docker CLI 进镜像 |
| 沙箱放到另一台机器 | `AGENT_DOCKER_BINARY` 支持写成 `docker -H tcp://沙箱机:2376 --tlsverify ...`，把执行面和应用面彻底分开 |

如果只是自己用、或者给信任的人用，现在这样就行。真开放给公众收费之前，
建议至少做第一条（socket 代理）。

### 为什么挂载路径必须"两边一致"

`docker-compose.yml` 里写的是：

```yaml
- ${AGENTCODE_DATA}:${AGENTCODE_DATA}
```

宿主路径和容器路径写成同一个。这不是啰嗦，是必须的：应用调宿主 Docker 起沙箱时，
传的是**自己在容器里看到的路径**；而宿主 Docker 是按**宿主机的路径**去找这个目录的。
如果两边不一致（比如容器里是 `/data/sandbox`、宿主上是 `/srv/agentcode/data/sandbox`），
沙箱的挂载会指向一个不存在的目录——容器起得来，但里面看不到任何文件。

## 9. 常见问题

**代码执行报「Docker 不可用」**
沙箱镜像没建，或者宿主的 docker.sock 没挂上。检查：

```bash
docker images | grep agentcode-sandbox
docker compose exec app docker info --format '{{.ServerVersion}}'
```

**模型调用全部失败**
`deploy/.env` 里的 key / base_url / model 三项。`docker compose exec app python -m agentcode config`
可以看脱敏后的配置。

**上传或生成的文件写不进去**
工作目录的权限问题。应用容器里是 root，建出来的目录默认是 0755，而沙箱以
nobody(65534) 运行——所以代码里会在执行前把工作目录放开到 0777（`prepare_workspace`）。
如果你手动改过 `$AGENTCODE_DATA` 的权限，注意这一点。

**页面能用但流式输出很慢**
Caddy 的 `flush_interval -1` 是不是被改掉了。

**容器起了但外面访问不到**
compose 默认只把 8000 绑在 `127.0.0.1`。要么用 `--profile public` 起 Caddy，
要么自己配反向代理。
