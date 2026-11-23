# 工作台接口契约

[工作台原始重构规格](./workbench-spec.md)

[SQLite Schema](../industplatform/workbench.sql) 在 `RunStore` 的迁移事务中初始化。[设计令牌](../industplatform/static/tokens.css) 由新工作台直接加载。`GET /api/workbench?domain=<id>` 提供当前领域的注册信息、会话、工序、采集队列、文献和计数。`POST /api/workbench/domains` 接受 `{"name":"领域名称"}`，创建成功时返回 HTTP 201 和 `{id, name}`。旧计划和核验记录仍由原表管理，新工作台按领域读取。大文件使用原文链接或远程 URI。

## Codex App Server 与模型网关

`sh contracts/generate-app-server.sh` 用当前 Codex 二进制生成 TypeScript 类型。`CODEX_BIN` 可指定仓库测试使用的二进制，生成配置和产物位于仓库的 `.local`。 [接口类型](./workbench.ts) 直接引用生成内容。

- 启动后发送 `initialize`，收到成功响应后发送 `initialized`，再按动作发送 `thread/start`、`thread/resume` 或 `thread/fork`。单个会话记住原生线程 ID、领域、模型连接、工作目录和权限所需上下文。
- `turn/start` 开始执行。`turn/steer` 使用当前线程和预期执行轮 ID。`turn/interrupt` 中断指定执行轮。浏览器更换领域或导航时只更换订阅。
- App Server 发出的审批请求用连接 ID 和原生请求 ID 关联，由平台存储领域、线程、执行轮和界面审批标识。审批回复通过原 JSON-RPC 连接发送。断开后以原生状态重新查询，连接失效的请求不能直接重放。
- 新网页指令随 `POST /api/runs` 提交 `execution_mode` 与对应的模型标识。`codex_subscription` 传 `subscription_model_id`，通过本机 App Server 的 ChatGPT 登录运行。提交前读取账户及模型目录，执行前在同一进程复核账户和 MCP 配置，使用 `ephemeral: true` 开启线程。`api_gateway` 传 `model_profile_id`，通过现有 HTTP 适配器调用供应商。两个模式都使用相同的阶段契约、工具白名单与 SQLite 写保护。
- `GET /api/codex-local` 读取本机检测快照。`POST /api/codex-local` 的 `detect` 操作读取原生 `account/read` 和 `model/list`。`login` 操作在检测为未登录时启动 ChatGPT 官方 OAuth，并通过同一端点查询登录完成状态。工作台只保存模型选择与执行记录。
- 工作台按会话记录恢复阶段和采集进度。临时原生线程仅在执行时有效。仓库 CLI 和已保存的旧任务保留原生线程模式。

## 持久采集调度器

- 工序内按轮次和渠道登记检索式及链接。一次事务认领任务并签发租约。只有有效租约可提交结果。提交时原子登记后续入口，失败保留重试时间或阻塞原因。进程恢复后回收过期租约。
- 元数据和远程原文位置写入 `raw_sources`。核验时先核对来源版本和证据，再写入 `frontier_candidates`。同轮每个来源仅计一次。有效证据的实体指纹按工序去重，首次发现轮次在 `entity_fingerprints` 固定。SQL 视图从这些记录计算分母与分子。
- 新增率 = 本轮首次入库的有效实体数 ÷ 本轮已判定的去重来源数。分母为零时，只有本轮扩展检索均已成功结束才记录为零。单个渠道检索为空仍应留有已完成的检索记录。工具错误、被限流及无法核验的来源保留待处理状态。
- 允许结束的条件为七类渠道各有至少两轮已完成的同义词和长尾扩展，连续三轮关闭且各轮新增率严格小于 2%，同时无排队、执行、重试、待核验项或未处置的访问阻塞。证据不足时保留具体缺口。结束状态的含义是该计划范围内的可访问入口已达到停止条件。
- 工程任务定义关联权威文献。验证器能力按 L1–L4 独立记录，评测绑定确定的验证器版本、模型连接和用户确认时间。导出模板的全局与领域覆写以独立版本管理。
