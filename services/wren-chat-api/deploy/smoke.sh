#!/usr/bin/env bash
# 灰度验收冒烟：对运行中的 wren-chat-api 跑全矩阵检查（真实样张、真实链路）。
#
# 前置：PostgreSQL 可达；docling 解析服务(5001)已预热；wren-chat-api 已以
#       两个开关开启的状态启动（deploy/start.sh 默认即如此）；.env 提供 API key。
#
# 耗时：渗透链路约 30-60s；等保任务在库中已有同哈希成功结果时秒级命中缓存，
#       全新环境（空库）首次跑约 15 分钟。默认等 25 分钟，可用 SMOKE_TIMEOUT 覆盖。
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BASE="${WREN_CHAT_BASE:-http://127.0.0.1:8300}"
SAMPLES=/mnt/sdb/workspace/report_file
PENTEST_PDF="$SAMPLES/非接触式特种设备数字化监管平台（监管端）渗透测试结果记录单.pdf"
DENGBAO_PDF="$SAMPLES/1-装订版“智慧市场监管”一体化平台系统-网络安全等级保护测评报告（S3A3）电子版.pdf"
RISK_DOCX="$SAMPLES/山东省市场监督管理局风险评估报告.docx"
KEY="$(grep WREN_CHAT_API_KEY "$DIR/.env" | cut -d= -f2)"
AUTH=(-H "Authorization: Bearer $KEY")
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

pass=0; fail=0
check() {  # check 名称 条件(0/非0)
    if [[ "$2" == "0" ]]; then echo "  [PASS] $1"; pass=$((pass+1));
    else echo "  [FAIL] $1"; fail=$((fail+1)); fi
}

echo "== 0. 前置健康 =="
curl -sf http://127.0.0.1:5001/health | grep -q '"warmed": *true'
check "docling 解析服务已预热" $?
curl -sf "$BASE/health/ready" >/dev/null
check "wren-chat-api 就绪" $?

echo "== 1. 渗透接口（VLM 主链路 + docling detail） =="
t0=$SECONDS
curl -sf --max-time 300 -X POST "$BASE/v1/pentest-report/extract" \
    "${AUTH[@]}" -F "file=@$PENTEST_PDF" -o "$TMP/pentest.json"
check "上传渗透样张返回 200" $?
python3 - "$TMP/pentest.json" <<'EOF'
import json, sys
d = json.load(open(sys.argv[1]))
assert "detail" in d and "risk_items" in d and "filename" in d
assert len(d["risk_items"]) == len(d["detail"]["安全风险项"]) > 0
base = json.load(open("/mnt/sdb/workspace/report_file/docling_fixed/非接触式特种设备数字化监管平台（监管端）渗透测试结果记录单_structured.json"))
assert d["detail"] == base, "detail 与基准不一致"
EOF
check "detail 与基准逐字段一致；两条链路条数互检相等" $?
echo "     （耗时 $((SECONDS-t0))s）"

echo "== 2. 风险评估旧路径（.docx → 8 字段，不受新分支影响） =="
curl -sf --max-time 120 -X POST "$BASE/v1/risk-assessment/extract" \
    "${AUTH[@]}" -F "file=@$RISK_DOCX" -o "$TMP/risk.json"
check "上传风险评估 docx 返回 200" $?
python3 - "$TMP/risk.json" <<'EOF'
import json, sys
d = json.load(open(sys.argv[1]))
assert d["code"] == 200
data = d["data"]
for k in ("riskHigh","riskHighRate","riskMedium","riskMediumRate",
          "riskLow","riskLowRate","finalEvaluationCode","finalEvaluationName"):
    assert data.get(k) is not None, k
EOF
check "8 个统计字段齐全" $?

echo "== 3. 等保任务（上传→taskId→轮询→结果） =="
t0=$SECONDS
curl -sf --max-time 60 -X POST "$BASE/v1/risk-assessment/extract" \
    "${AUTH[@]}" -F "file=@$DENGBAO_PDF" -o "$TMP/accept.json"
check "上传等保样张受理（业务码 200）" $?
TASK="$(python3 -c "import json;print(json.load(open('$TMP/accept.json'))['data']['taskId'])")"
deadline=$((SECONDS + ${SMOKE_TIMEOUT:-1500}))
while :; do
    curl -sf "${AUTH[@]}" "$BASE/v1/report-tasks/$TASK" -o "$TMP/task.json"
    status="$(python3 -c "import json;print(json.load(open('$TMP/task.json'))['status'])")"
    [[ "$status" != "processing" ]] && break
    [[ $SECONDS -ge $deadline ]] && { echo "  [FAIL] 等保任务超时"; fail=$((fail+1)); break; }
    sleep 10
done
python3 - "$TMP/task.json" <<'EOF'
import json, sys
view = json.load(open(sys.argv[1]))
assert view["status"] == "succeeded", view.get("error")
assert view["result"]["reportType"] == "dengbao"
base = json.load(open("/mnt/sdb/workspace/report_file/dengbao_parse/pipeline_out/报告提取结果.json"))
assert view["result"]["detail"] == base, "detail 与基准不一致"
EOF
check "任务 succeeded 且结果与基准逐字段一致" $?
echo "     （taskId=$TASK，耗时 $((SECONDS-t0))s，含缓存命中情形）"

echo "== 4. 错误路径（非等保 PDF → 业务失败；无鉴权 → 401） =="
if curl -s --max-time 60 -X POST "$BASE/v1/risk-assessment/extract" \
    "${AUTH[@]}" -F "file=@$PENTEST_PDF" -o "$TMP/reject.json" \
    && python3 -c "
import json; d = json.load(open('$TMP/reject.json'))
assert d['code'] == 422 and d['data'] is None"; then ok=0; else ok=1; fi
check "非等保 PDF 以业务码 422 拒绝" $ok
code="$(curl -s -o /dev/null -w '%{http_code}' "$BASE/v1/report-tasks/00000000-0000-0000-0000-000000000000" -H 'Authorization: Bearer wrong')"
if [[ "$code" == "401" ]]; then ok=0; else ok=1; fi
check "查询接口错误密钥返回 401" $ok

echo "== 5. 监控指标 =="
if curl -sf "${AUTH[@]}" "$BASE/metrics" -o "$TMP/metrics.txt"; then ok=0; else ok=1; fi
check "GET /metrics 可访问" $ok
for m in wren_chat_report_tasks_total wren_chat_report_tasks_pending wren_chat_pentest_docling_total wren_chat_requests_total; do
    if grep -q "^$m" "$TMP/metrics.txt" 2>/dev/null; then ok=0; else ok=1; fi
    check "指标 $m 已暴露" $ok
done

echo
echo "灰度验收结果: $pass 通过 / $fail 失败"
[[ $fail -eq 0 ]]
