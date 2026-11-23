---
name: industrial-benchmark
description: 生成并运行 CNC 首件检验 Benchmark，查看题目、独立判分和工具验收，导出公开题包。
---

1. 先调用 `get_rules`，按需求选择固定题或指定 0 至 999999 的复现种子。初始场景为 `demo-cnc`。
2. 调用 `start_run`。默认选择 `offline`，以本机工具建立回归基线。用户明确要求真实模型评估并且 `get_catalog` 中可见 `live` 时，才提交该评估方式。
3. 用 `wait_run` 或 `get_run` 获取结果。分别核对题目数、有效判分、接口错误和工具测试，不确定的模型难度保留待复核。
4. 完成后调用 `export_benchmark`，交付运行 ID、`benchmark_path` 和核验摘要。题包的输入与结果校验范围按合成首件检验报告。
