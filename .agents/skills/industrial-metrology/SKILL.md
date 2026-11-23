---
name: industrial-metrology
description: 用规范化的首件检验 CSV 核对特征版本、单位、公差和缺测，给出可复查的逐项结果。
---

1. 调用 `get_rules` 确认实体关联、单位和公差的示例规则。读取规格 CSV 和测量 CSV，保留表头、版本、特征 ID、单位和值。
2. 把两份 CSV 原文分别传给 `inspect_measurements` 的 `requirements_csv`、`measurements_csv`。
3. 汇总整件状态、异常特征和原因码。缺测、版本不匹配或无效字段按工具结果登记为待复核。
4. 报告交付适用的文件格式和工作范围。设备实时数据或工厂验收任务需要另外确认实际工况的测量证据。
