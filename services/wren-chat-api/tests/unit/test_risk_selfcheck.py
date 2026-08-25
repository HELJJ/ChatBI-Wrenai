"""Unit contracts for the risk self-check module."""

import asyncio

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from openai import APIConnectionError

from wren_chat_api.config import Settings
from wren_chat_api.contracts import RiskSelfCheckItem, RiskSelfCheckRequest
from wren_chat_api.errors import (
    InvalidRiskCheckResult,
    RequestTimedOut,
    UpstreamFailed,
)
from wren_chat_api.risk_selfcheck import (
    RiskSelfCheckService,
    _build_user_prompt,
    _parse_indices,
)

_DESCRIPTIONS = [
    "Apache Struts2 远程代码执行漏洞（CVE-2017-5638），受影响版本 Struts "
    "2.3.5 - 2.3.31、2.5 - 2.5.10。",
    "Spring Data Commons 反序列化漏洞，受影响版本 1.13 - 1.13.11。",
    "Apache Log4j2 JNDI 注入漏洞（CVE-2021-44228），受影响版本 2.0-beta9 "
    "至 2.14.1。",
]


def make_settings(tmp_path, **overrides) -> Settings:
    values = {
        "state_database_url": "postgresql://user:pass@localhost:5432/wren_test",
        "api_key": "test-key",
        "project_path": tmp_path,
        "model": "test-model",
    }
    values.update(overrides)
    return Settings(**values, _env_file=None)


def make_item(item_id: str = "2121", **overrides) -> RiskSelfCheckItem:
    values = {
        "id": item_id,
        "component": "struts2",
        "version": "2.3.31",
        "vulnerability_descriptions": list(_DESCRIPTIONS),
    }
    values.update(overrides)
    return RiskSelfCheckItem(**values)


def make_request(**overrides) -> RiskSelfCheckRequest:
    values = {
        "list": [
            make_item("2121"),
            make_item("1111", component="log4j", version="2.14.1"),
        ]
    }
    values.update(overrides)
    return RiskSelfCheckRequest(**values)


class FakeModel:
    def __init__(
        self,
        *,
        content: str = '{"matched_indices": []}',
        delay: float = 0,
        error=None,
        contents_by_call: list[str] | None = None,
    ):
        self.delay = delay
        self.error = error
        self.content = content
        # Per-call contents; the last entry repeats for further calls.
        self.contents_by_call = contents_by_call
        self.calls = []
        self.active = 0
        self.peak_concurrency = 0
        self.bound_kwargs: dict = {}

    def bind(self, **kwargs):
        self.bound_kwargs = kwargs
        return self

    async def ainvoke(self, messages):
        self.calls.append(messages)
        self.active += 1
        self.peak_concurrency = max(self.peak_concurrency, self.active)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            if self.error is not None:
                raise self.error
            if self.contents_by_call is not None:
                index = min(len(self.calls) - 1, len(self.contents_by_call) - 1)
                content = self.contents_by_call[index]
            else:
                content = self.content
            return AIMessage(content=content)
        finally:
            self.active -= 1


def make_service(tmp_path, model, **settings_overrides):
    return RiskSelfCheckService(
        model=model,
        settings=make_settings(tmp_path, **settings_overrides),
    )


# --- _build_user_prompt -----------------------------------------------------


def test_user_prompt_carries_component_version_and_numbered_descriptions():
    prompt = _build_user_prompt(make_item())

    assert "组件：struts2" in prompt
    assert "版本：2.3.31" in prompt
    assert "漏洞描述（共 3 条）" in prompt
    assert "[0] Apache Struts2" in prompt
    assert "[1] Spring Data Commons" in prompt
    assert "[2] Apache Log4j2" in prompt


def test_user_prompt_without_version_tells_model_to_skip_version_reasoning():
    prompt = _build_user_prompt(make_item(version=None))

    assert "组件：struts2" in prompt
    assert "版本：未提供（不推理受影响版本，仅比对组件）" in prompt
    assert "版本：2" not in prompt


@pytest.mark.parametrize("blank", [None, "", "   "])
def test_absent_or_blank_version_normalizes_to_none(blank):
    assert make_item(version=blank).version is None


# --- _parse_indices ----------------------------------------------------------


def test_parse_indices_accepts_valid_list():
    assert _parse_indices('{"matched_indices": [0, 2]}', 3) == {0, 2}


def test_parse_indices_tolerates_code_fences():
    text = '```json\n{"matched_indices": [1]}\n```'

    assert _parse_indices(text, 3) == {1}


def test_parse_indices_folds_duplicates():
    assert _parse_indices('{"matched_indices": [1, 1]}', 3) == {1}


@pytest.mark.parametrize(
    "text,count",
    [
        ("模型拒绝回答", 3),
        ('{"matched": true}', 3),
        ('{"matched_indices": "[0, 2]"}', 3),
        ('{"matched_indices": [0.5]}', 3),
        ('{"matched_indices": [true]}', 3),
        ('{"matched_indices": [3]}', 3),
        ('{"matched_indices": [-1]}', 3),
    ],
)
def test_parse_indices_rejects_malformed_output(text, count):
    with pytest.raises(ValueError):
        _parse_indices(text, count)


# --- RiskSelfCheckService.check ----------------------------------------------


async def test_check_returns_1_when_any_description_matches(tmp_path):
    model = FakeModel(content='{"matched_indices": [0, 2]}')
    service = make_service(tmp_path, model)

    assert await service.check(make_item()) == 1


async def test_check_returns_0_when_nothing_matches(tmp_path):
    model = FakeModel(content='{"matched_indices": []}')
    service = make_service(tmp_path, model)

    assert await service.check(make_item()) == 0


async def test_check_sends_system_and_numbered_user_message(tmp_path):
    model = FakeModel(content='{"matched_indices": []}')
    service = make_service(tmp_path, model)

    await service.check(make_item())

    assert len(model.calls) == 1
    system_message, user_message = model.calls[0]
    assert isinstance(system_message, SystemMessage)
    assert isinstance(user_message, HumanMessage)
    assert "matched_indices" in system_message.content
    assert "组件：struts2" in user_message.content


async def test_check_without_version_marks_it_in_the_user_message(tmp_path):
    model = FakeModel(content='{"matched_indices": [0]}')
    service = make_service(tmp_path, model)

    assert await service.check(make_item(version=None)) == 1

    user_message = model.calls[0][1]
    assert "版本：未提供（不推理受影响版本，仅比对组件）" in user_message.content


async def test_check_binds_explicit_max_tokens(tmp_path):
    model = FakeModel(content='{"matched_indices": []}')

    make_service(tmp_path, model, risk_selfcheck_max_tokens=512)

    assert model.bound_kwargs == {"max_tokens": 512}


async def test_check_fails_closed_on_out_of_range_index(tmp_path):
    model = FakeModel(content='{"matched_indices": [7]}')
    service = make_service(tmp_path, model)

    with pytest.raises(InvalidRiskCheckResult):
        await service.check(make_item())


async def test_check_fails_closed_on_invalid_json(tmp_path):
    model = FakeModel(content="我认为该组件不受影响。")
    service = make_service(tmp_path, model)

    with pytest.raises(InvalidRiskCheckResult):
        await service.check(make_item())


async def test_check_maps_timeout_to_request_timed_out(tmp_path):
    model = FakeModel(delay=10)
    service = make_service(tmp_path, model, risk_selfcheck_timeout_seconds=1)

    with pytest.raises(RequestTimedOut):
        await service.check(make_item())


async def test_check_maps_openai_error_to_upstream_failed(tmp_path):
    model = FakeModel(
        error=APIConnectionError(
            request=httpx.Request("POST", "https://maas.example/v1/chat/completions")
        )
    )
    service = make_service(tmp_path, model)

    with pytest.raises(UpstreamFailed):
        await service.check(make_item())


# --- RiskSelfCheckService.check_batch ----------------------------------------


async def test_batch_returns_verdict_per_item_in_request_order(tmp_path):
    model = FakeModel(
        contents_by_call=['{"matched_indices": [0]}', '{"matched_indices": []}']
    )
    service = make_service(tmp_path, model)

    response = await service.check_batch(make_request())

    assert len(response.data) == 2
    first, second = response.data
    assert first.id == "2121"
    assert first.component == "struts2"
    assert first.matched == 1
    assert second.id == "1111"
    assert second.component == "log4j"
    assert second.matched == 0


async def test_batch_failed_item_becomes_error_item_and_rest_survive(tmp_path):
    model = FakeModel(
        contents_by_call=["这不是 JSON", '{"matched_indices": [0]}']
    )
    service = make_service(tmp_path, model)

    response = await service.check_batch(make_request())

    assert len(response.data) == 2
    first, second = response.data
    # A failed judgment is an error item, never a masked matched=0.
    assert first.id == "2121"
    assert first.error == InvalidRiskCheckResult().public_message
    assert second.id == "1111"
    assert second.matched == 1


async def test_batch_timeout_items_become_error_items(tmp_path):
    model = FakeModel(delay=10)
    service = make_service(tmp_path, model, risk_selfcheck_timeout_seconds=1)

    response = await service.check_batch(make_request())

    for entry in response.data:
        assert entry.error == RequestTimedOut().public_message


async def test_batch_upstream_errors_become_error_items(tmp_path):
    model = FakeModel(
        error=APIConnectionError(
            request=httpx.Request("POST", "https://maas.example/v1/chat/completions")
        )
    )
    service = make_service(tmp_path, model)

    response = await service.check_batch(make_request())

    for entry in response.data:
        assert entry.error == UpstreamFailed().public_message


async def test_batch_runs_items_under_the_concurrency_ceiling(tmp_path):
    model = FakeModel(content='{"matched_indices": []}', delay=0.05)
    service = make_service(tmp_path, model, risk_selfcheck_concurrency=2)
    request = make_request(
        list=[make_item(str(i)) for i in range(6)]
    )

    await service.check_batch(request)

    assert model.peak_concurrency <= 2


async def test_batch_empty_list_is_rejected_by_the_contract():
    with pytest.raises(ValueError):
        RiskSelfCheckRequest(list=[])
