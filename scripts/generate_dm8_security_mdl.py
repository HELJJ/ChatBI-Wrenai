#!/usr/bin/env python3
"""Generate the agreed Wren MDL package for nine DM8 security tables."""

from __future__ import annotations

import argparse
import csv
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import yaml


SCHEMA = "SAFETY_RISK_NEW"
EXPECTED_COLUMN_COUNT = 204
MISSING_DESCRIPTION = "原数据库未提供字段说明。"

TABLE_TO_MODEL = {
    "sec_security_architecture": "security_architecture",
    "sec_security_database": "security_database",
    "sec_security_dataexchange": "security_data_exchange",
    "sec_security_middleware": "security_middleware",
    "sec_security_os": "security_os",
    "sec_security_plugin": "security_plugin",
    "sec_security_port": "security_port",
    "sec_security_resource": "security_resource",
    "SEC_SYSTEM_INFO": "security_system",
}

RELATIONSHIPS = [
    ("architecture_system", "security_architecture", "F_SYSTEM_ID", "security_system", "F_ID"),
    ("database_system", "security_database", "F_SYSTEMID", "security_system", "F_ID"),
    (
        "data_exchange_system",
        "security_data_exchange",
        "F_BUSINESSSYSTEM_ID",
        "security_system",
        "F_ID",
    ),
    ("middleware_system", "security_middleware", "F_SYSTEM_ID", "security_system", "F_ID"),
    (
        "middleware_resource",
        "security_middleware",
        "F_RESOURCE_ID",
        "security_resource",
        "F_ID",
    ),
    ("os_system", "security_os", "F_SYSTEMID", "security_system", "F_ID"),
    ("os_resource", "security_os", "F_RESOURCE_ID", "security_resource", "F_ID"),
    ("plugin_system", "security_plugin", "F_SYSTEM_ID", "security_system", "F_ID"),
    ("port_system", "security_port", "F_BUSINESSSYSTEM_ID", "security_system", "F_ID"),
    ("resource_system", "security_resource", "F_SYSTEMID", "security_system", "F_ID"),
]


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def normalized_type(row: dict[str, str]) -> str:
    raw_type = row["DATA_TYPE"].strip().upper()
    if raw_type in {"VARCHAR", "VARCHAR2"}:
        length = row["DATA_LENGTH"].strip()
        if length:
            return f"VARCHAR({int(float(length))})"
        return "VARCHAR"
    supported = {"INT", "BIGINT", "TEXT", "TIMESTAMP", "DATETIME", "DATE"}
    if raw_type not in supported:
        raise ValueError(f"不支持的 DM8 类型: {raw_type} ({row['TABLE_NAME']}.{row['COLUMN_NAME']})")
    return raw_type


def write_yaml(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        yaml.safe_dump(
            value,
            handle,
            allow_unicode=True,
            sort_keys=False,
            width=1000,
            default_flow_style=False,
        )


def write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value.rstrip() + "\n", encoding="utf-8", newline="\n")


def validate_inputs(
    columns: list[dict[str, str]],
    constraints: list[dict[str, str]],
    comments: list[dict[str, str]],
) -> tuple[dict[tuple[str, str], str], dict[str, str]]:
    expected_tables = set(TABLE_TO_MODEL)
    actual_tables = {row["TABLE_NAME"] for row in columns}
    if actual_tables != expected_tables:
        raise ValueError(
            f"columns.csv 表集合不匹配; 缺少={sorted(expected_tables - actual_tables)}, "
            f"多出={sorted(actual_tables - expected_tables)}"
        )
    if len(columns) != EXPECTED_COLUMN_COUNT:
        raise ValueError(f"字段数应为 {EXPECTED_COLUMN_COUNT}，实际为 {len(columns)}")

    duplicate_columns = [
        key
        for key, count in Counter((row["TABLE_NAME"], row["COLUMN_NAME"]) for row in columns).items()
        if count != 1
    ]
    if duplicate_columns:
        raise ValueError(f"存在重复字段定义: {duplicate_columns}")

    relevant_constraints = [row for row in constraints if row["TABLE_NAME"] in expected_tables]
    primary_keys = [row for row in relevant_constraints if row["CONSTRAINT_TYPE"].upper() == "P"]
    expected_primary_keys = {(table, "F_ID") for table in expected_tables}
    actual_primary_keys = {(row["TABLE_NAME"], row["COLUMN_NAME"]) for row in primary_keys}
    if actual_primary_keys != expected_primary_keys or len(primary_keys) != len(expected_tables):
        raise ValueError(f"主键定义不匹配: {sorted(actual_primary_keys)}")

    foreign_keys = [row for row in relevant_constraints if row["CONSTRAINT_TYPE"].upper() == "R"]
    if foreign_keys:
        raise ValueError("输入包含数据库外键；本包预期仅使用已确认的逻辑关系")

    column_keys = {(row["TABLE_NAME"], row["COLUMN_NAME"]) for row in columns}
    comment_map: dict[tuple[str, str], str] = {}
    table_comment_map: dict[str, str] = {}
    for row in comments:
        table = row["TABLE_NAME"]
        if table not in expected_tables:
            continue
        column = row["COLUMN_NAME"]
        if column:
            comment_map[(table, column)] = row["COLUMN_COMMENT"].strip()
        table_comment = row["TABLE_COMMENT"].strip()
        if table_comment:
            previous = table_comment_map.setdefault(table, table_comment)
            if previous != table_comment:
                raise ValueError(f"同一表出现不同表注释: {table}")

    unknown_comments = set(comment_map) - column_keys
    if unknown_comments:
        raise ValueError(f"comments.csv 包含未知字段: {sorted(unknown_comments)}")
    if set(table_comment_map) != expected_tables:
        raise ValueError(f"缺少表注释: {sorted(expected_tables - set(table_comment_map))}")

    missing_comments = [key for key in column_keys if not comment_map.get(key)]
    if len(missing_comments) != 6:
        raise ValueError(f"预期 6 个字段无注释，实际为 {len(missing_comments)}: {sorted(missing_comments)}")
    return comment_map, table_comment_map


def build_project(
    columns_path: Path,
    constraints_path: Path,
    comments_path: Path,
    output: Path,
) -> None:
    columns = read_csv(columns_path)
    constraints = read_csv(constraints_path)
    comments = read_csv(comments_path)
    comment_map, table_comment_map = validate_inputs(columns, constraints, comments)

    if output.exists():
        raise FileExistsError(f"输出目录已存在，请先移走或指定新目录: {output}")
    output.mkdir(parents=True)

    columns_by_table: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in columns:
        columns_by_table[row["TABLE_NAME"]].append(row)
    for rows in columns_by_table.values():
        rows.sort(key=lambda row: int(row["COLUMN_ID"]))

    for table, model_name in TABLE_TO_MODEL.items():
        model_columns = []
        for row in columns_by_table[table]:
            column_name = row["COLUMN_NAME"]
            model_columns.append(
                {
                    "name": column_name,
                    "type": normalized_type(row),
                    "is_calculated": False,
                    "not_null": row["NULLABLE"].strip().upper() == "N",
                    "is_primary_key": column_name == "F_ID",
                    "properties": {
                        "description": comment_map.get((table, column_name)) or MISSING_DESCRIPTION
                    },
                }
            )
        model = {
            "name": model_name,
            "properties": {"description": table_comment_map[table]},
            "table_reference": {"catalog": "", "schema": SCHEMA, "table": table},
            "columns": model_columns,
            "primary_key": "F_ID",
            "cached": False,
        }
        write_yaml(output / "models" / model_name / "metadata.yml", model)

    relationship_document = {
        "relationships": [
            {
                "name": name,
                "models": [source_model, target_model],
                "join_type": "MANY_TO_ONE",
                "condition": f"{source_model}.{source_column} = {target_model}.{target_column}",
            }
            for name, source_model, source_column, target_model, target_column in RELATIONSHIPS
        ]
    }
    write_yaml(output / "relationships.yml", relationship_document)
    write_yaml(
        output / "wren_project.yml",
        {
            "schema_version": 5,
            "name": "dm8_security",
            "catalog": "wren",
            "schema": SCHEMA,
            "data_source": "dm8",
            "profile": "dm8-prod",
        },
    )
    write_yaml(output / "knowledge" / "knowledge.yml", {"schema_version": 1})

    write_text(
        output / "knowledge" / "rules" / "security_query_rules.md",
        """# 安全资产问数规则

- 默认只使用本项目中的九个安全资产模型；除非用户明确要求，不访问 DM8 中的其他表。
- 各模型默认过滤 `F_DELETEMARK = 0`，排除已删除记录。
- 用户询问“当前、有效、启用、在用”的资产时，同时过滤 `F_ENABLEDMARK = 1`。
- 模型关联必须使用已定义的 ID 字段关系，不使用冗余名称字段做连接。
- 只生成只读查询；禁止生成 INSERT、UPDATE、DELETE、TRUNCATE、DROP、ALTER 等写操作或 DDL。
- 探索性明细查询默认最多返回 100 行；聚合结果不强制该限制。
- 涉及系统维度时，优先通过 `security_system.F_ID` 关联其他模型的系统 ID 字段。
- 涉及资源维度时，操作系统和中间件通过 `security_resource.F_ID` 关联资源 ID 字段。
""",
    )
    write_text(
        output / "AGENTS.md",
        """# Wren DM8 安全资产项目

本项目只覆盖 `SAFETY_RISK_NEW` 模式中的九张安全资产表。

执行问数任务时：

1. 先读取 `knowledge/rules/security_query_rules.md`。
2. 先运行 `wren dry-plan --sql '<Wren SQL>'` 检查生成的 DM8 SQL。
3. 再运行 `wren dry-run --sql '<Wren SQL>'` 验证数据库可执行性。
4. 仅在 SQL 只读且结果范围合理时运行 `wren query --sql '<Wren SQL>'`。
""",
    )
    write_text(
        output / ".gitignore",
        """.env
.wren/
target/
__pycache__/
""",
    )
    write_text(
        output / "README.md",
        f"""# DM8 安全资产 Wren MDL

该项目包含 `{SCHEMA}` 模式下 9 张安全资产表的完整 Wren MDL：204 个物理字段、9 个主键和 10 条已确认的逻辑关系。包中不含数据库密码或连接配置。

## 部署到 Linux

```bash
cd /mnt/sdb/workspace
unzip dm8-security-mdl.zip -d wren-dm-demo-new
cd wren-dm-demo-new

# 服务器上需已存在 dm8-prod profile；此命令把项目绑定到该 profile
wren context set-profile dm8-prod
wren context validate
wren context build
test -f target/mdl.json && echo "MDL OK"
```

如需覆盖现有 `/mnt/sdb/workspace/wren-dm-demo`，请先备份原目录，再把压缩包内文件复制进去；不要把本机连接密码写入项目文件。

## 连接与最小验证

```bash
wren profile debug dm8-prod

wren dry-plan --sql 'SELECT COUNT(*) AS system_count FROM security_system WHERE F_DELETEMARK = 0'
wren dry-run  --sql 'SELECT COUNT(*) AS system_count FROM security_system WHERE F_DELETEMARK = 0'
wren query    --sql 'SELECT COUNT(*) AS system_count FROM security_system WHERE F_DELETEMARK = 0'
```

再验证一条逻辑关联：

```bash
wren dry-run --sql 'SELECT s.F_ID, COUNT(r.F_ID) AS resource_count FROM security_system s LEFT JOIN security_resource r ON r.F_SYSTEMID = s.F_ID AND r.F_DELETEMARK = 0 WHERE s.F_DELETEMARK = 0 GROUP BY s.F_ID LIMIT 10'
```

## 在本机 Codex 中问数

由于 Wren 和 DM8 均位于 Linux 服务器，本机 Codex 不应直接读取数据库密码。请在 Linux 项目目录启动 Wren MCP HTTP 服务，并只向可信网络开放端口：

```bash
cd /mnt/sdb/workspace/wren-dm-demo-new
wren serve mcp --transport http --host 127.0.0.1 --port 8000
```

在本机建立 SSH 隧道（将 `<linux-host>` 替换为服务器地址）：

```bash
ssh -N -L 8000:127.0.0.1:8000 root@<linux-host>
```

另开一个本机终端，把 Wren 注册到 Codex：

```bash
codex mcp add --url http://127.0.0.1:8000/mcp wren-dm8
codex mcp list
```

连接成功后，可先问：

- “统计未删除的业务系统数量。”
- “按系统统计有效服务器资源数量，返回前 10 名。”
- “列出当前启用且开放高风险端口的系统、IP 和端口号。”

## 模型清单

| Wren 模型 | DM8 物理表 |
|---|---|
"""
        + "\n".join(f"| `{model}` | `{table}` |" for table, model in TABLE_TO_MODEL.items()),
    )

    generated_model_files = list((output / "models").glob("*/metadata.yml"))
    if len(generated_model_files) != 9:
        raise AssertionError(f"生成模型数错误: {len(generated_model_files)}")
    generated_column_count = sum(
        len(yaml.safe_load(path.read_text(encoding="utf-8"))["columns"])
        for path in generated_model_files
    )
    if generated_column_count != EXPECTED_COLUMN_COUNT:
        raise AssertionError(f"生成字段数错误: {generated_column_count}")
    if len(RELATIONSHIPS) != 10:
        raise AssertionError(f"生成关系数错误: {len(RELATIONSHIPS)}")

    print(f"Generated project: {output.resolve()}")
    print("Models: 9")
    print(f"Columns: {generated_column_count}")
    print("Primary keys: 9")
    print("Relationships: 10")
    print("Missing source column comments represented explicitly: 6")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--columns", type=Path, required=True)
    parser.add_argument("--constraints", type=Path, required=True)
    parser.add_argument("--comments", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    build_project(args.columns, args.constraints, args.comments, args.output)


if __name__ == "__main__":
    main()
