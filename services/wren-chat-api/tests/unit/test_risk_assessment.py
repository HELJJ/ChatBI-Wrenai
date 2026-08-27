"""Unit tests for the risk-assessment extraction pipeline."""

import io
import json
import zipfile
from types import SimpleNamespace

import pytest

from wren_chat_api import risk_assessment
from wren_chat_api.config import Settings
from wren_chat_api.errors import (
    InvalidRiskAssessmentResult,
    InvalidRiskFile,
    RiskDocConversionFailed,
)
from wren_chat_api.risk_assessment import (
    RiskAssessmentService,
    gate_section,
    parse_docx_blocks,
    parse_extraction,
    run_soffice,
    serialize_blocks,
    validate_extraction,
    validate_risk_upload,
)

# ------------------------------------------------------------ docx builder
_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

STATS_TABLE = [
    ("风险等级", "风险标识", "个数", "百分比"),
    ("5", "很高", "0", "0%"),
    ("4", "高", "1", "2%"),
    ("3", "中等", "5", "9%"),
    ("2", "低", "46", "87%"),
    ("1", "很低", "1", "2%"),
]
# Structurally identical to the stats table but with no count columns; it
# lives in an earlier section and must never reach the model.
DECOY_TABLE = [
    ("风险值", "风险等级", "等级标识", "描述"),
    ("81-125", "5", "很高", "一旦发生将产生非常严重的经济或社会影响"),
    ("55-80", "4", "高", "一旦发生将产生较大的经济或社会影响"),
]

EXPECTED_PAYLOAD = {
    "riskHigh": 1,
    "riskHighRate": 0.02,
    "riskMedium": 5,
    "riskMediumRate": 0.09,
    "riskLow": 46,
    "riskLowRate": 0.87,
    "finalEvaluationCode": "L",
    "finalEvaluationName": "低风险",
}


def para(
    text: str, *, style: str | None = None, ilvl: int | None = None, num_id: int = 14
) -> str:
    parts = []
    if style is not None:
        parts.append(f'<w:pStyle w:val="{style}"/>')
    if ilvl is not None:
        parts.append(
            f'<w:numPr><w:ilvl w:val="{ilvl}"/><w:numId w:val="{num_id}"/></w:numPr>'
        )
    p_pr = f"<w:pPr>{''.join(parts)}</w:pPr>" if parts else ""
    return f"<w:p>{p_pr}<w:r><w:t>{text}</w:t></w:r></w:p>"


def table(rows: list[tuple[str, ...]]) -> str:
    row_xml = "".join(
        "<w:tr>"
        + "".join(
            f"<w:tc><w:p><w:r><w:t>{cell}</w:t></w:r></w:p></w:tc>" for cell in row
        )
        + "</w:tr>"
        for row in rows
    )
    return f"<w:tbl>{row_xml}</w:tbl>"


def build_docx(body: str) -> bytes:
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document xmlns:w="{_W_NS}"><w:body>{body}</w:body></w:document>'
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("word/document.xml", document)
    return buffer.getvalue()


def report_body(*, with_heading: bool = True, with_prose: bool = True) -> str:
    blocks = [
        para("山东省市场监督管理局"),
        para(" 风险分析", ilvl=0),
        para(" 风险分析方法", ilvl=1),
        para("表 71 风险等级划分表"),
        table(DECOY_TABLE),
    ]
    if with_heading:
        blocks.append(para(" 风险等级统计", ilvl=1))
    if with_prose:
        blocks.append(
            para(
                "本次风险分析，共分析53个关键信息资产，共发现1个高风险，"
                "5个中风险，46个低风险。总体情况如下表："
            )
        )
    blocks.extend(
        [
            table(STATS_TABLE),
            para("本次风险评估最终判定为低风险。"),
            para(" 不可接受风险处置建议", ilvl=1),
            para("对本次评估过程识别的安全风险制定处置计划。"),
        ]
    )
    return "".join(blocks)


def serialize_report(**kwargs) -> str:
    blocks = parse_docx_blocks(build_docx(report_body(**kwargs)))
    section, _ = gate_section(blocks)
    return serialize_blocks(section)


# ------------------------------------------------------------ upload gate
def test_upload_rejects_wrong_suffix():
    with pytest.raises(InvalidRiskFile):
        validate_risk_upload("report.txt", b"PK\x03\x04data")


def test_upload_rejects_missing_magic():
    with pytest.raises(InvalidRiskFile):
        validate_risk_upload("report.docx", b"plain text, no magic")


def test_upload_accepts_docx_and_doc_magics():
    validate_risk_upload("报告.docx", b"PK\x03\x04" + b"x" * 20)
    validate_risk_upload("报告.doc", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"x" * 20)


# ---------------------------------------------------------------- parsing
def test_blocks_preserve_document_order_and_table_cells():
    blocks = parse_docx_blocks(build_docx(report_body()))
    kinds = [block.kind for block in blocks]
    assert kinds[:6] == ["p", "p", "p", "p", "tbl", "p"]
    tables = [block for block in blocks if block.kind == "tbl"]
    assert tables[0].rows[0] == DECOY_TABLE[0]
    assert tables[1].rows[2] == ("4", "高", "1", "2%")


def test_gated_section_excludes_decoy_table_and_next_section():
    text = serialize_report()
    assert "风险等级统计" in text
    assert "| 4 | 高 | 1 | 2% |" in text
    assert "最终判定为低风险" in text
    assert "81-125" not in text  # decoy table stays out
    assert "处置计划" not in text  # next section stays out


def test_gate_mode_is_section_when_heading_found():
    blocks = parse_docx_blocks(build_docx(report_body()))
    _, mode = gate_section(blocks)
    assert mode == "章节定位"


def test_gate_falls_back_to_whole_document_without_heading():
    blocks = parse_docx_blocks(build_docx(report_body(with_heading=False)))
    section, mode = gate_section(blocks)
    assert mode == "全文档"
    assert len(section) == len(blocks)


def test_gate_ends_at_higher_level_heading():
    body = (
        para(" 风险等级统计", ilvl=1)
        + table(STATS_TABLE)
        + para("本次风险评估最终判定为低风险。")
        + para(" 第二章", ilvl=0)  # parent chapter heading ends the section
        + para("后继章节正文。")
    )
    blocks = parse_docx_blocks(build_docx(body))
    section, mode = gate_section(blocks)
    assert mode == "章节定位"
    text = serialize_blocks(section)
    assert "后继章节正文" not in text
    assert "低风险" in text


def test_serialize_renders_markdown_table_shape():
    text = serialize_report()
    lines = text.splitlines()
    assert "| 风险等级 | 风险标识 | 个数 | 百分比 |" in lines
    header_index = lines.index("| 风险等级 | 风险标识 | 个数 | 百分比 |")
    assert lines[header_index + 1].startswith("| --- ")
    assert lines[header_index + 1].endswith("|")


# --------------------------------------------------------- model parsing
def test_parse_extraction_tolerates_code_fences():
    raw = "```json\n" + json.dumps(EXPECTED_PAYLOAD) + "\n```"
    assert parse_extraction(raw) == EXPECTED_PAYLOAD


def test_parse_extraction_rejects_non_json():
    with pytest.raises(ValueError):
        parse_extraction("抱歉，我无法提取。")


# ------------------------------------------------------------ validation
def test_validation_passes_on_real_document_values():
    text = serialize_report()
    stats = validate_extraction(EXPECTED_PAYLOAD, text)
    assert stats["riskHigh"] == 1
    assert stats["riskLowRate"] == 0.87
    assert stats["finalEvaluationName"] == "低风险"


def test_validation_rejects_hallucinated_count():
    payload = EXPECTED_PAYLOAD | {"riskHigh": 3}
    with pytest.raises(InvalidRiskAssessmentResult) as excinfo:
        validate_extraction(payload, serialize_report())
    assert "疑似幻觉" in excinfo.value.internal_message


def test_validation_rejects_rate_without_percent_in_text():
    payload = EXPECTED_PAYLOAD | {"riskHighRate": 0.03}
    with pytest.raises(InvalidRiskAssessmentResult) as excinfo:
        validate_extraction(payload, serialize_report())
    assert "未同现" in excinfo.value.internal_message


def test_validation_accepts_prose_only_section_without_table():
    # Template drift with no markdown table: the loose containment path
    # applies (counts and percents anywhere in the text channel).
    text = (
        "共分析53个关键信息资产，共发现1个高风险，5个中风险，46个低风险，"
        "分别占比2%、9%、87%。本次风险评估最终判定为低风险。"
    )
    stats = validate_extraction(EXPECTED_PAYLOAD, text)
    assert stats["riskHigh"] == 1
    assert stats["riskLowRate"] == 0.87


def test_validation_prose_only_rejects_wrong_percent():
    text = (
        "共分析53个关键信息资产，共发现1个高风险，5个中风险，46个低风险，"
        "分别占比2%、9%、87%。本次风险评估最终判定为低风险。"
    )
    payload = EXPECTED_PAYLOAD | {"riskHighRate": 0.04}
    with pytest.raises(InvalidRiskAssessmentResult):
        validate_extraction(payload, text)


def test_validation_rejects_prose_count_mismatch():
    # With a table present, a bucket swap is caught even earlier: the
    # (count, percent) pair no longer co-occurs in one row.
    payload = EXPECTED_PAYLOAD | {"riskMedium": 46, "riskLow": 5}
    with pytest.raises(InvalidRiskAssessmentResult) as excinfo:
        validate_extraction(payload, serialize_report())
    assert "未同现" in excinfo.value.internal_message


def test_validation_prose_only_rejects_swapped_counts():
    # Without a table the loose path lets the swap through; only the
    # summary-sentence cross-check catches it.
    text = (
        "共分析53个关键信息资产，共发现1个高风险，5个中风险，46个低风险，"
        "分别占比2%、9%、87%。本次风险评估最终判定为低风险。"
    )
    payload = EXPECTED_PAYLOAD | {"riskMedium": 46, "riskLow": 5}
    with pytest.raises(InvalidRiskAssessmentResult) as excinfo:
        validate_extraction(payload, text)
    assert "总述句" in excinfo.value.internal_message


def test_validation_skips_crosscheck_when_prose_absent():
    text = serialize_report(with_prose=False)
    validate_extraction(EXPECTED_PAYLOAD, text)


def test_validation_rejects_code_name_mismatch():
    payload = EXPECTED_PAYLOAD | {
        "finalEvaluationCode": "H",
        "finalEvaluationName": "低风险",
    }
    with pytest.raises(InvalidRiskAssessmentResult) as excinfo:
        validate_extraction(payload, serialize_report())
    assert "不对应" in excinfo.value.internal_message


def test_validation_rejects_missing_field_as_null():
    payload = EXPECTED_PAYLOAD | {"riskLow": None}
    with pytest.raises(InvalidRiskAssessmentResult) as excinfo:
        validate_extraction(payload, serialize_report())
    assert "缺少字段 riskLow" in excinfo.value.internal_message


# --------------------------------------------------------- doc conversion
def test_run_soffice_failure_maps_to_typed_error(monkeypatch):
    def fake_run(*args, **kwargs):
        return SimpleNamespace(returncode=1, stderr=b"Error: source corrupt")

    monkeypatch.setattr(risk_assessment.subprocess, "run", fake_run)
    with pytest.raises(RiskDocConversionFailed):
        run_soffice(b"\xd0\xcf\x11\xe0 junk", soffice_bin="soffice", timeout_seconds=5)


def test_run_soffice_missing_binary_maps_to_typed_error(monkeypatch):
    def fake_run(*args, **kwargs):
        raise FileNotFoundError("no soffice")

    monkeypatch.setattr(risk_assessment.subprocess, "run", fake_run)
    with pytest.raises(RiskDocConversionFailed):
        run_soffice(b"\xd0\xcf\x11\xe0 junk", soffice_bin="soffice", timeout_seconds=5)


# ---------------------------------------------------------------- service
class FakeModel:
    def __init__(self, content: str):
        self._content = content
        self.calls: list[list] = []

    def bind(self, **kwargs):
        return self

    async def ainvoke(self, messages):
        self.calls.append(messages)
        return SimpleNamespace(content=self._content)


def make_settings(tmp_path) -> Settings:
    return Settings(
        state_database_url="postgresql://user:pass@localhost:5432/wren_test",
        api_key="test-key",
        project_path=tmp_path,
        model="test-model",
        _env_file=None,
    )


def make_service(tmp_path, content: str) -> tuple[RiskAssessmentService, FakeModel]:
    model = FakeModel(content)
    return RiskAssessmentService(model=model, settings=make_settings(tmp_path)), model


async def test_service_extracts_from_docx_end_to_end(tmp_path):
    service, model = make_service(tmp_path, json.dumps(EXPECTED_PAYLOAD))
    response = await service.extract("报告.docx", build_docx(report_body()))

    assert response.model_dump() == {
        "filename": "报告.docx",
        **EXPECTED_PAYLOAD,
    }
    assert len(model.calls) == 1
    user_prompt = str(model.calls[0][1].content)
    assert "| 4 | 高 | 1 | 2% |" in user_prompt
    assert "81-125" not in user_prompt


async def test_service_converts_doc_before_extracting(tmp_path, monkeypatch):
    docx_bytes = build_docx(report_body())
    monkeypatch.setattr(risk_assessment, "run_soffice", lambda *a, **k: docx_bytes)
    service, _ = make_service(tmp_path, json.dumps(EXPECTED_PAYLOAD))
    ole = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"binary blob"
    response = await service.extract("报告.doc", ole)

    assert response.filename == "报告.doc"
    assert response.riskLow == 46


async def test_service_raises_typed_error_on_model_garbage(tmp_path):
    service, _ = make_service(tmp_path, "not json at all")
    with pytest.raises(InvalidRiskAssessmentResult):
        await service.extract("报告.docx", build_docx(report_body()))


async def test_service_raises_typed_error_on_validation_failure(tmp_path):
    payload = json.dumps(EXPECTED_PAYLOAD | {"riskHigh": 9})
    service, _ = make_service(tmp_path, payload)
    with pytest.raises(InvalidRiskAssessmentResult):
        await service.extract("报告.docx", build_docx(report_body()))
