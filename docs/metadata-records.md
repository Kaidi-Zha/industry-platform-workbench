# 共享工业元数据目录

每条资料保存至官方 L2 行业下对应的类型目录。首次导出合格资料时才创建目录。信源及文献按 L2、类型和规范化 URL 生成文件名。企业关系、工序及题目还包含工程对象标识，便于区分不同对象上的同名工序和题目。

    data/domains/<l2_code>/{sources,literature,entities,processes,tasks}/<canonical_id>.json
    data/_index/manifest.json

每条 JSON 包含 l2_code、object_cluster 和 lifecycle_stages。l2_code 与路径和兼容字段 domain_id 一致，例如 D01.2。lifecycle_stages 是 S0–S8 的非空无重复子集。object_cluster 在同一 L2 内复用已有的同义标识，使用小写字母、数字和短横线命名。L3/L4 提示清单提供覆盖线索。

同一任务的条目共享一个 task_id。新资料从 active v1 开始。纠错增版并填写理由，废止保留原记录及其证据。自动索引由主分支工作流按 L2 生成。规范见 [共享元数据 Schema](../schemas/metadata-record.schema.json)。
