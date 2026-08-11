# Wren DM8 安全资产项目

本项目只覆盖 `SAFETY_RISK_NEW` 模式中的九张安全资产表。

执行问数任务时：

1. 先读取 `knowledge/rules/security_query_rules.md`。
2. 先运行 `wren dry-plan --sql '<Wren SQL>'` 检查生成的 DM8 SQL。
3. 再运行 `wren dry-run --sql '<Wren SQL>'` 验证数据库可执行性。
4. 仅在 SQL 只读且结果范围合理时运行 `wren query --sql '<Wren SQL>'`。
