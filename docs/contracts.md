# 浏览器 API 与事件契约

`docs/openapi.json` 由 `scripts/export_openapi.py` 使用不连接数据库、不读取本地凭据的文档配置生成；`frontend/src/api/generated/schema.d.ts` 由锁定的 openapi-typescript 生成。验证时重新生成并比较，不能手改生成类型。

认证使用 HttpOnly 的 ip_session Cookie 和独立 ip_csrf；所有浏览器写入校验 Origin 与 CSRF。登录、me 返回公开身份；账号切换会废止旧 Cookie，登出删除会话。前端切换身份时中止请求/事件流并清空缓存，JSON 解析之后也复核当前登录代际，旧 401 不得注销新账号。

主要端点：

| 范围 | HTTP |
| --- | --- |
| 会话与话题 | POST/GET conversations；GET conversations/{id}；POST topics、topics/{id}/activate |
| 提交与变更 | POST conversations/{id}/messages；POST runs/{id}/cancel、revise、resume |
| 状态与记录 | GET runs/{id}、runs（before 游标、limit 最多100）、topics/{id}/history（最近200条消息、100条任务） |
| 报告与证据 | GET runs/{id}/report、runs/{id}/evidence/{evidence_id} |
| 事件 | GET runs/{id}/events（SSE）；GET event-batch（同一持久事件的 JSON 恢复接口） |
| 指标与成员 | GET datasets、metrics、system、members；GET/PATCH members/{id} |

消息的 client_message_id 在一次用户提交及全部网络重试中保持不变；正文、话题、运行方式与 expected_epoch 捕获为一个不可变请求。新消息带 expected_epoch 做 CAS；已有消息先返回原回执，不检查新的前台版本，也不再次推进 epoch。409 后只能由用户明确重新提交产生新消息键。话题切换独立 CAS；取消和恢复精确指定 run，不从“同意”等普通文本推断授权。

RunSnapshot 的状态、snapshot_seq 与公开计划在同一 PostgreSQL SELECT 快照中读取。前端按 run/revision/execution_version 过滤旧数据；调用计数在同一 immutable run 内不回退。前台结果还校验 topic 与 epoch，后台结果按原话题归档。HTTP 快照不会推进 SSE cursor；仅收到事件推进对应 run 的游标。事件必带 seq/run_id/topic_id/task_revision/execution_version/scope/epoch，版本在发事件时持久保存，不从当前任务状态反推。

SSE 只提供阶段、公开步骤和安全查询编号；每批重新检查 Cookie、当前授权与数据可见性，数据库事务在 yield 前结束。连接最多25秒后由客户端携游标重连，状态轮询用于恢复；旧账号、旧执行、旧前台版本事件不能追加当前回答。服务端不得返回隐藏推理、原始供应商响应或访问令牌。

状态列表只带 has_report 与必要元信息，完整报告走独立授权读取，避免批量携带大结果。数据停用后隐藏报告、证据及助手历史中的报告内容；模型上下文只使用当前 topic、当前 immutable 数据版本的已完成有效消息。报告打开时重新核验，导出前再次读取授权报告；导出 JSON 包含证据、查询摘要、口径版本、数据版本和图表配置。React 直接渲染文本，不使用 dangerouslySetInnerHTML；图表仅受控 Plotly，表格分页可访问。

浏览器本地验收：显式运行 tests/browser/check_workbench.py，传入 IP_BROWSER_NODE、IP_BROWSER_PLAYWRIGHT、IP_BROWSER_EXECUTABLE（现有 Chromium/Edge）路径。夹具使用独立临时服务、合成数据与确定性 HTTP 模型；不访问用户浏览器会话。登录、HITL、两页409、A/B确认隔离、实际柱状图、证据导出、记录/目录/成员、390px布局及登出均有真实浏览器断言。在线供应商效果单独由评测阶段报告，不能把此夹具冒充真实模型效果。
