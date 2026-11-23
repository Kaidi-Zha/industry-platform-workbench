---
name: industrial-source-discovery
description: 从 Forest 词表和登记的发布方发现并核验公开工业数据源，记录版本、许可和样本覆盖。
---

1. 用 `search_keywords` 查场景、工程问题、软件、格式和标准。用 `get_catalog` 匹配登记的 Zenodo 或 Hugging Face 路线。
2. 用 `start_run` 提交已登记的公开路线，设置 `cohort: offline`，把需求词用于本地筛选。公开采集会请求数据平台。
3. 用 `wait_run` 等待采集结果，仍在进行时保留运行 ID，必要时再查询。将 `needs_review` 当作可交付的候选核验结果。
4. 逐条交付原始来源、数据版本、许可出处，以及实际检查到的文件、字段和范围。标记哪些工程字段或用途证据待补。
