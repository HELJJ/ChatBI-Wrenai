# wren-chat-api 部署与运维手册（含 docling 解析链路）

覆盖阶段一~五的全部部署形态：渗透接口 docling detail、等保 PDF 任务化、
监控与灰度验收。适用于当前单机（nohup）形态，并给出 systemd 升级路径。

## 一、组件与启动顺序

```
PostgreSQL (5432)  →  docling 解析服务 (127.0.0.1:5001)  →  wren-chat-api (127.0.0.1:8300)
                            │                                      │
                     /mnt/sdb/workspace/docling_service     /mnt/sdb/workspace/ChatBI-Wrenai/services/wren-chat-api
                     （docling 源码 venv, Py3.10）            （micromamba wrenai env, Py3.11）
```

另有系统依赖：`soffice`（LibreOffice，风险评估 .doc 转换，已有）。

```bash
# 1. PostgreSQL（现有，略）
# 2. docling 解析服务（模型常驻，预热约 1 分钟）
/mnt/sdb/workspace/docling_service/start.sh
# 3. wren-chat-api（deploy/start.sh 默认打开两个 docling 开关）
cd /mnt/sdb/workspace/ChatBI-Wrenai/services/wren-chat-api
./deploy/start.sh
# 4. 灰度验收
./deploy/smoke.sh
```

## 二、配置总表（wren-chat-api，环境变量前缀 WREN_CHAT_）

| 变量 | 默认 | 说明 |
|---|---|---|
| `PENTEST_DOCLING_ENABLED` | start.sh 中 true（代码默认 false） | 渗透接口附加 detail 通道；关=响应与旧契约逐字节一致 |
| `RISK_DENGBAO_ENABLED` | start.sh 中 true（代码默认 false） | 风险评估接口接受等保 PDF（异步任务）；关=仅 .doc/.docx |
| `DOCLING_SERVICE_URL` | http://127.0.0.1:5001 | 解析服务地址 |
| `PENTEST_DOCLING_TIMEOUT_SECONDS` | 300 | 渗透链路解析超时（17 页实测 22s） |
| `DENGBAO_DOCLING_TIMEOUT_SECONDS` | 1800 | 等保任务解析超时（522 页实测 820s） |
| `REPORT_TASK_POLL_SECONDS` | 5 | 任务 worker 轮询间隔 |

解析服务配置见 `/mnt/sdb/workspace/docling_service/README.md`（离线模型说明在同一文档）。

## 三、接口行为速查

| 接口 | 输入 | 行为 |
|---|---|---|
| `POST /v1/pentest-report/extract` | 渗透记录单 PDF | 同步；`risk_items`（VLM 3 字段）+ 可选 `detail`（docling 结构化：测试基本信息/安全风险项含测试内容·风险分析·加固建议/通过项/不适用项），~30-60s |
| `POST /v1/risk-assessment/extract` | 风险评估 .doc/.docx | 同步 8 统计字段（原路径，未改动） |
| 同上 | 等保测评 PDF | **异步**：秒回 `{code:200,data:{taskId,status}}`；同哈希去重（处理中共用任务、已完成直接命中缓存） |
| `GET /v1/report-tasks/{taskId}` | — | `processing` / `succeeded`(含 `result.detail` 14 键) / `failed`(含公开错误)；404=任务不存在 |

## 四、监控（GET /metrics，需鉴权）

| 指标 | 含义与告警建议 |
|---|---|
| `wren_chat_report_tasks_total{outcome}` | 等保任务终态计数（worker 实跑，不含缓存命中）。`failed` 持续增长 → 查日志 `report task ... failed` |
| `wren_chat_report_tasks_pending` | 任务积压。常驻 >0 属正常（排队）；持续增长 > 数小时 → 解析服务异常或积压过多 |
| `wren_chat_pentest_docling_total{outcome}` | 渗透 detail 通道。`degraded` 占比升高 → 解析服务不可用（主字段不受影响） |
| `wren_chat_requests_total{route,status}` | 新增 route 标签值：`/v1/report-tasks` |

## 五、运维注意

- **任务表清理**：`report_tasks` 行含上传原件字节（15MB/份），长期运行需定期清理。
  已交付报告保留 30 天的示例：
  ```sql
  DELETE FROM report_tasks
  WHERE status = 'succeeded' AND completed_at < now() - interval '30 days';
  ```
  （删除后同文件再上传会重新解析；如需永久缓存，把 result 备份后再删。）
- **停机语义**：wren-chat-api 停止会取消在途等保任务（不等待 14 分钟转换）；
  对应行留 `running`，下次启动自动重排队（启动日志 `report task recovery`）。
- **解析服务重启**：预热 1 分钟内 `warmed:false`，期间渗透 detail 降级为
  detail=null、等保任务失败可重传；无需重启 wren-chat-api。
- **换机迁移**：HF 模型缓存（/root/.cache/huggingface，约 500MB）+
  docling venv + micromamba env + 两个 workspace 目录 + .env；详见
  docling_service/README.md 离线说明。

## 六、生产切换步骤（本机已完成）

1. 解析服务 start.sh 常驻（已完成，PID 记录在其 service.pid）；
2. `deploy/start.sh` 以双开关开启启动 wren-chat-api（默认 true）；
3. `deploy/smoke.sh` 全矩阵验收（2026-09-22 首跑记录见 logs/）；
4. 回退方式：stop.sh 后以两个 `=false` 环境变量重启——渗透响应回到旧契约，
   等保 PDF 回到 422 拒绝，.doc/.docx 路径从未受影响。

生产加固建议：systemd 托管两个服务（`deploy/wren-chat-api.service` 与
docling-service 的单元文件都已备好），开机自启 + 崩溃自动拉起。

## 七、遗留清单（按优先级）

| # | 事项 | 影响 | 建议 |
|---|---|---|---|
| 1 | 等保管线为当前报告模板精调（章节锚点/表 7-1 修复） | 换机构/模板的报告可能解析质量下降 | 管线自带体检报告暴露问题；新模板需在 report_file 调锚点后重跑对拍 |
| 2 | `detail` 含测试账号/内网 IP 等敏感字段 | 渗透 detail 原样出网 | 若网关要求脱敏，在 PentestDoclingChannel 加字段裁剪配置（小时级工作量） |
| 3 | 非等保 PDF 的拒绝文案复用通用 INVALID_RISK_FILE 消息 | 提示略误导（"需 .doc/.docx"） | 加专用错误子类改善文案（分钟级） |
| 4 | GPU 加速未启用（3×L40 闲置，torch CUDA 与驱动不匹配） | 522 页 14 分钟；GPU 可缩短数倍 | 评估 torch 版本与驱动适配后，解析服务 `DOCLING_DEVICE=cuda` |
| 5 | GLM-OCR 勾选框/印章直读未启用（无密钥） | 附录 B 勾选状态走规则推断 | 配 `GLM_OCR_API_KEY` 即启用，结果交叉校验后回填 |
| 6 | 代码未提交 git（阶段三~五全部改动） | 交付物仅在工作区 | 确认后一次性提交 |
| 7 | `report_postproc` 依赖 editable 安装于本机 | 换机需重装 | 迁移清单已含；或后续打成内部 wheel |
