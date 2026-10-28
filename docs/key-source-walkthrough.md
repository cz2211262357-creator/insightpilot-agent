# 源码导读与面试切入点

| 问题 | 主要代码 | 可验证的行为 |
| --- | --- | --- |
| Agent 为什么会规划和暂停 | `src/insightpilot/agent/{graph,nodes,state}.py` | LangGraph 路由、公开计划、interrupt、受限 SQL 修复 |
| 模型调用如何计费和限时 | `agent/model.py`、`runs/repository.py` | 先持久预留预算，再实际 HTTP；真实 usage 缺失保留空值 |
| 重发消息与双页面竞争 | `runs/service.py`、`frontend/src/workbench` | 唯一键幂等、expected_epoch CAS、旧流过滤 |
| 切换话题后长任务继续 | `worker.py`、`runs/repository.py` | BACKGROUND 不依赖当前前台 epoch，仍检查 run/授权/租约 |
| 旧 Worker 为什么不能写 | `checkpoints.py`、`runs/repository.py` | 同事务权限行锁、execution_version、租约、官方 saver 写入 |
| 如何让模型只读数据库 | `query/{policy,parameters,gateway}.py`、`migrations/004_rls.sql` | AST 白名单、绑定参数、独立只读角色、FORCE RLS |
| MCP 如何防伪造租户 | `auth/tool_tokens.py`、`mcp_server.py`、`mcp_client.py` | 短时令牌绑定当前 run，模型参数不能指定租户或连接 |
| “同意”为什么不会误批准 | `runs/confirmations.py`、`query/confirmations.py` | 原任务 token/SQL/预算/checkpoint 绑定，专用按钮恢复 |
| 防止后台占满交互资源 | `admission.py`、`worker.py` | Redis Lua 原子准入，执行/模型分别保留前台容量 |
| 防止报告数字编造 | `reports/{builder,repository,history}.py` | Decimal 重算、证据哈希、当前权限复核、受控图表 |
| 如何证明恢复有效 | `tests/system/test_execution_recovery.py` | 真进程暂停/终止，恢复后旧写提交为零 |
| 准确率来自哪里 | `evals/{gold_cases.json,engine.py,run.py}` | 独立参考 SQL、实际完整链路、匹配规则和失败逐题记录 |

阅读建议：先运行一次含澄清的分析，再从 API receipt 找到 run，沿 Worker → graph → MCP → query_evidence → report 查数据库状态。解释系统时讲具体竞争时序和失败结果，不能把“用了 Redis/框架”当成一致性保证。

真实约束：检查点和数据库提交可防旧写，外部模型调用在进程崩溃时仍可能重复收费；只读查询允许恢复重试，但逻辑证据按 query_id 去重。这里没有宣称模型 exactly-once，也没有把后台队列当作无限执行时间。
