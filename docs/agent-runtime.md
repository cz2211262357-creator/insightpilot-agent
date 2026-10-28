# 分析状态图与报告证据

Agent 以原始任务问题和当前 topic 的已完成有效历史构造上下文，最多 20 条、16000 字符。每次模型请求使用 ChatOpenAI 对接兼容模式，关闭 SDK 自动重试；持久预算先预留，再发 HTTP。每次实际尝试独立记录供应商返回的 usage，缺失时为 unavailable/null。迟到请求只允许补记原尝试的消耗，不得修改任务或报告。原始模型响应、隐藏推理和解析异常原文不进入检查点。

状态图为目录 → 计划 →（需要口径澄清则 interrupt）→ 生成 SQL → 校验 →（超过真实 EXPLAIN 门槛则 interrupt）→ 只读执行 → 下一步骤或报告。可修复错误最多两次；危险 SQL、权限拒绝及永久供应商错误不尝试绕过。interrupt 与产生计划/查询票据的节点分离，恢复不会重复这些前置调用。

Worker 在状态图提交 interrupt 检查点之后，将任务设为 WAITING_INPUT 并释放租约。口径确认和高成本 SQL 确认分别绑定当前授权、任务修订、预算、检查点摘要与 24 小时期限；高成本确认还绑定确切 SQL hash。专用 resume 接口需要 Cookie、Origin、CSRF、令牌、幂等键和结构化决定。消息“确认”没有授权含义。消费确认、保存 resume_payload、原任务重新排队在同一事务完成。人工恢复不增加自动重试次数。

报告只从数据库重新读取当前 run/revision 下的不可变证据，重验数据版本、SQL 策略与哈希。模型叙述只允许证据单元格或服务器核算的聚合引用及 {value} 占位符。Decimal/Pandas 核对金额、占比和分组合计；截断或 SQL LIMIT 结果不能再声明为全量合计，NULL 保留为空。图表仅服务器构造的 Plotly bar/line/table，使用白名单字段和转义标签，无任意 HTML/脚本。图完成时任务仍为 RUNNING；Worker 在最终校验后原子发布正式报告和 SUCCEEDED。
