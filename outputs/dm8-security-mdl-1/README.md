# DM8 安全资产 Wren MDL

该项目包含 `SAFETY_RISK_NEW` 模式下 9 张安全资产表的完整 Wren MDL：204 个物理字段、9 个主键和 10 条已确认的逻辑关系。包中不含数据库密码或连接配置。

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
| `security_architecture` | `sec_security_architecture` |
| `security_database` | `sec_security_database` |
| `security_data_exchange` | `sec_security_dataexchange` |
| `security_middleware` | `sec_security_middleware` |
| `security_os` | `sec_security_os` |
| `security_plugin` | `sec_security_plugin` |
| `security_port` | `sec_security_port` |
| `security_resource` | `sec_security_resource` |
| `security_system` | `SEC_SYSTEM_INFO` |
