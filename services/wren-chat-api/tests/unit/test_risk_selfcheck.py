"""Unit contracts for the risk self-check module."""

import asyncio

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from openai import APIConnectionError

from wren_chat_api.config import Settings
from wren_chat_api.contracts import RiskSelfCheckRequest
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


def make_request(**overrides) -> RiskSelfCheckRequest:
    values = {
        "component": "struts2",
        "version": "2.3.31",
        "vulnerability_descriptions": list(_DESCRIPTIONS),
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
    ):
        self.delay = delay
        self.error = error
        self.content = content
        self.calls = []
        self.bound_kwargs: dict = {}

    def bind(self, **kwargs):
        self.bound_kwargs = kwargs
        return self

    async def ainvoke(self, messages):
        self.calls.append(messages)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return AIMessage(content=self.content)


def make_service(tmp_path, model, **settings_overrides):
    return RiskSelfCheckService(
        model=model,
        settings=make_settings(tmp_path, **settings_overrides),
    )


# --- _build_user_prompt -----------------------------------------------------


def test_user_prompt_carries_component_version_and_numbered_descriptions():
    prompt = _build_user_prompt(make_request())

    assert "组件：struts2" in prompt
    assert "版本：2.3.31" in prompt
    assert "漏洞描述（共 3 条）" in prompt
    assert "[0] Apache Struts2" in prompt
    assert "[1] Spring Data Commons" in prompt
    assert "[2] Apache Log4j2" in prompt


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

    response = await service.check(make_request())

    assert response.component == "struts2"
    assert response.matched == 1


async def test_check_returns_0_when_nothing_matches(tmp_path):
    model = FakeModel(content='{"matched_indices": []}')
    service = make_service(tmp_path, model)

    response = await service.check(make_request())

    assert response.component == "struts2"
    assert response.matched == 0


async def test_check_sends_system_and_numbered_user_message(tmp_path):
    model = FakeModel(content='{"matched_indices": []}')
    service = make_service(tmp_path, model)

    await service.check(make_request())

    assert len(model.calls) == 1
    system_message, user_message = model.calls[0]
    assert isinstance(system_message, SystemMessage)
    assert isinstance(user_message, HumanMessage)
    assert "matched_indices" in system_message.content
    assert "组件：struts2" in user_message.content


async def test_check_binds_explicit_max_tokens(tmp_path):
    model = FakeModel(content='{"matched_indices": []}')

    make_service(tmp_path, model, risk_selfcheck_max_tokens=512)

    assert model.bound_kwargs == {"max_tokens": 512}


async def test_check_fails_closed_on_out_of_range_index(tmp_path):
    model = FakeModel(content='{"matched_indices": [7]}')
    service = make_service(tmp_path, model)

    with pytest.raises(InvalidRiskCheckResult):
        await service.check(make_request())


async def test_check_fails_closed_on_invalid_json(tmp_path):
    model = FakeModel(content="我认为该组件不受影响。")
    service = make_service(tmp_path, model)

    with pytest.raises(InvalidRiskCheckResult):
        await service.check(make_request())


async def test_check_maps_timeout_to_request_timed_out(tmp_path):
    model = FakeModel(delay=10)
    service = make_service(tmp_path, model, risk_selfcheck_timeout_seconds=1)

    with pytest.raises(RequestTimedOut):
        await service.check(make_request())


async def test_check_maps_openai_error_to_upstream_failed(tmp_path):
    model = FakeModel(
        error=APIConnectionError(
            request=httpx.Request("POST", "https://maas.example/v1/chat/completions")
        )
    )
    service = make_service(tmp_path, model)

    with pytest.raises(UpstreamFailed):
        await service.check(make_request())
