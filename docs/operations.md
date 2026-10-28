# 本地部署与操作

## 环境与启动

需要 Docker Engine/Compose、可用的百炼 API Key。开发环境使用 Python 3.12 与 uv 0.11.16；前端锁定 npm 依赖，CI 使用 Node 26。Windows 的 `scripts/bootstrap.ps1` 将 uv、Python、虚拟环境和缓存放在指定工作区，不设置全局 PATH、注册表或系统 Python。

Windows 参照 README 的 bootstrap/up 命令。Linux/macOS 在仓库目录执行（先安装 uv 0.11.16、Node 和 Docker）：

```bash
source scripts/env.sh "$PWD"
uv python install 3.12.13 --no-bin
uv sync --all-groups --frozen
uv run python scripts/bootstrap.py --workspace-root "$PWD"
# 编辑 .env.insightpilot，填入真实模型 Key，然后重新 source 加载。
source scripts/env.sh "$PWD"
docker compose --env-file .env.insightpilot up -d --wait postgres redis
uv run python -m scripts.manage --workspace-root "$PWD" --create-accounts
docker compose --env-file .env.insightpilot --profile api --profile worker --profile mcp --profile web up -d --build --wait
```

如果源码目录位于工作区 R 之下，传入 R 的绝对路径，Compose 的 `--env-file` 也指向 R/.env.insightpilot。最终服务应从长期保留的源码目录启动。镜像与 Docker 引擎缓存由 Docker Desktop 管理；项目不会迁移或修改全局引擎配置。

| 服务 | 主机地址 |
| --- | --- |
| Web | 127.0.0.1:8090 |
| API | 127.0.0.1:8010 |
| MCP | 127.0.0.1:8011 |
| PostgreSQL | 127.0.0.1:5433 |
| Redis | 127.0.0.1:6382 |
| Worker 探针 | 仅容器内 8012，不发布 |

POSIX 系统中，新建本地配置、账号凭据和数据库备份使用 0600 权限；已有私有配置/账号文件会移除组和其他用户权限。Windows 的访问控制依赖目录继承的 ACL，POSIX 模式位不等同于 Windows ACL。请根据本机账号配置工作区权限。

Linux 主机的绑定目录会覆盖镜像中的目录权限。容器以 UID/GID 10001 运行，需确保工作区内 `.data/insightpilot/runtime`、`.cache/insightpilot-tmp` 与 `.cache/insightpilot` 对容器映射后的用户可写；可以对这些项目目录单独配置 ACL，并通过容器内 `test -w` 验证。不同 rootless Docker 的 UID 映射可能不同，不要直接给所有用户开放写权限。本项目未在所有 Linux 宿主配置上完成端到端部署验证。

所有发布端口都绑定 loopback。浏览器访问 localhost:8090 或 127.0.0.1:8090。若部署到公网，需要部署方配置 HTTPS 入口、生产 Origin/Secure Cookie、密钥管理和备份策略；默认本地配置没有作为公网部署配置验收。

## 账号和数据

首次初始化创建合成商户 A 的管理员/分析师和商户 B 的分析师，密码随机生成，只保存于 `R/.data/insightpilot/accounts.json`。重复初始化不重置账号。管理员可在工作台停用或调整本租户成员；停用或角色变化立即使旧授权失效。新增账号使用 `uv run python -m scripts.accounts --workspace-root R --tenant-id UUID --email EMAIL --name NAME --role analyst`。

内置数据固定种子 20261025，两个商户 96/60 笔订单，包含取消、零元、跨月退款和空月份。每个快照有不可变版本与 SHA256；新增数据应创建新版本。当前项目没有连接任意生产数据库的功能。对话和导出结果仍要遵守账号所属租户与当前数据版本可见性。

## 检查与恢复

`/api/v1/health/live` 只说明进程可响应；`/api/v1/health/ready` 检查三种数据库角色可访问的运行表、schema 版本和 Redis。Worker 还检查任务循环仍在运行。探针不调用模型，不输出 DSN。供应商认证错误可从任务错误码判断；修复本地配置后重建对应容器，不能用固定回复替代真实模型成功。

停止：`docker compose --env-file R/.env.insightpilot --profile api --profile worker --profile mcp --profile web stop`。再次 up 会恢复持久队列，过期租约由新 Worker 接管，已经取消的任务保持终态。不要用清空数据库处理任务失败。

每轮执行/模型分别最多并发 2，后台最多 1；前台排队 60 秒、执行 120 秒；后台排队 600 秒、累计执行 300 秒。人工等待最多 24 小时；8 次模型、4 次查询、2 次修复是跨恢复累计上限。查询 3 秒/1000 行/1 MiB，批准高成本查询也不改变这些上限。

## 备份与恢复演练

加载 env 后，在项目目录执行：

```powershell
uv run python -m scripts.backup create
uv run python -m scripts.backup verify --path "R/.data/insightpilot/backups/实际文件名.dump"
```

备份包含用户密码哈希、会话与检查点，必须留在私有目录。命令输出文件路径及校验摘要，不输出数据内容。`verify` 校验 SHA256，并新建带所有权标签的临时 PostgreSQL/Redis 容器执行真实 pg_restore；核对 schema、数据版本、订单计数和恢复后的 RLS。它不覆盖运行中的数据库，结束后只清理本次创建的容器。

真实灾备恢复应先保留原数据目录和当前备份，在独立 PostgreSQL 实例恢复并验证，再切换应用连接；项目不提供无确认覆盖生产库的脚本。模型供应商已发生的调用费用不能通过取消回滚，用量缺失记为未知而不是零。

## 验收

`scripts/verify.ps1` 运行后端、前端和现有浏览器测试，`-BuildImages` 加入镜像构建。测试自行创建只绑定 127.0.0.1 的容器，不使用本地业务库。GitHub CI 不需要模型 Key，使用明确标识的控制供应商；真实模型验收使用 `evals.run --mode online`，结果与控制模式分别保存。
