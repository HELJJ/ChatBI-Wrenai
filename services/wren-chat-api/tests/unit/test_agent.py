"""Unit contracts for the sequential LangGraph chat agent."""

import asyncio
from uuid import uuid4

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import Field

from wren_chat_api.agent import build_chat_graph, invoke_chat
from wren_chat_api.audited_query import RunContext
from wren_chat_api.config import Settings
from wren_chat_api.errors import InvalidFinalAnswer


def make_settings(tmp_path) -> Settings:
    return Settings(
        state_database_url="postgresql://user:pass@localhost:5432/wren_test",
        api_key="integration-test-key",
        project_path=tmp_path,
        model="test-model",
    )


class ScriptedModel(BaseChatModel):
    """Deterministic chat model replaying queued responses."""

    responses: list
    cursor: int = 0
    received: list = Field(default_factory=list)

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.received.append(list(messages))
        response = self.responses[self.cursor]
        self.cursor += 1
        return ChatResult(generations=[ChatGeneration(message=response)])


class FakeAuditedQuery:
    def __init__(self, delay: float = 0.05) -> None:
        self.calls: list[str] = []
        self.in_flight = 0
        self.max_in_flight = 0
        self.delay = delay

    async def execute(self, context, sql, limit=100):
        self.calls.append(sql)
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        await asyncio.sleep(self.delay)
        self.in_flight -= 1
        return {
            "ok": True,
            "content": f"rows for {sql}",
            "content_truncated": False,
            "returned_row_count": 1,
            "result_truncated": False,
        }


class FakeToolkit:
    def __init__(self, tools) -> None:
        self._tools = list(tools)

    def get_tools(self, *, include_memory_write: bool = True, **kwargs):
        return list(self._tools)

    def system_prompt(self, *, tools=None) -> str:
        return "wren system prompt"


def _wren_query_call(sql: str, call_id: str) -> dict:
    return {"name": "wren_query", "args": {"sql": sql}, "id": call_id}


async def test_multiple_tool_calls_run_sequentially_in_model_order(tmp_path):
    audited = FakeAuditedQuery()
    settings = make_settings(tmp_path)
    model = ScriptedModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    _wren_query_call("SELECT 1", "call-1"),
                    _wren_query_call("SELECT 2", "call-2"),
                ],
            ),
            AIMessage(content="both answered"),
        ]
    )
    graph = build_chat_graph(
        toolkit=FakeToolkit([]),
        model=model,
        summarizer=lambda history, previous: previous,
        checkpointer=InMemorySaver(),
        settings=settings,
    )
    context = RunContext(
        request_id=uuid4(),
        session_id="s-1",
        audited_query=audited,
    )

    answer = await invoke_chat(graph, "thread-1", "run two", context, settings)

    assert answer == "both answered"
    assert audited.calls == ["SELECT 1", "SELECT 2"]
    assert audited.max_in_flight == 1


async def test_non_wren_tools_are_invoked_and_returned(tmp_path):
    @tool("wren_list_models")
    def list_models() -> str:
        return "model-list"

    settings = make_settings(tmp_path)
    model = ScriptedModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "wren_list_models",
                        "args": {},
                        "id": "call-1",
                    }
                ],
            ),
            AIMessage(content="listed"),
        ]
    )
    graph = build_chat_graph(
        toolkit=FakeToolkit([list_models]),
        model=model,
        summarizer=lambda history, previous: previous,
        checkpointer=InMemorySaver(),
        settings=settings,
    )
    context = RunContext(
        request_id=uuid4(),
        session_id="s-1",
        audited_query=FakeAuditedQuery(),
    )

    answer = await invoke_chat(graph, "thread-1", "list models", context, settings)
    state = await graph.aget_state({"configurable": {"thread_id": "thread-1"}})

    assert answer == "listed"
    tool_messages = [
        message
        for message in state.values["messages"]
        if isinstance(message, ToolMessage)
    ]
    assert len(tool_messages) == 1
    assert "model-list" in tool_messages[0].content


async def test_state_keeps_six_turns_and_strips_tool_traffic(tmp_path):
    settings = make_settings(tmp_path)
    audited = FakeAuditedQuery()
    responses = [AIMessage(content=f"answer {index}") for index in range(1, 7)]
    responses.append(
        AIMessage(
            content="",
            tool_calls=[_wren_query_call("SELECT 7", "call-7")],
        )
    )
    responses.append(AIMessage(content="answer 7"))
    responses.append(AIMessage(content="比较完成"))
    model = ScriptedModel(responses=responses)
    graph = build_chat_graph(
        toolkit=FakeToolkit([]),
        model=model,
        summarizer=lambda history, previous: "第一期营收为100",
        checkpointer=InMemorySaver(),
        settings=settings,
    )
    context = RunContext(
        request_id=uuid4(),
        session_id="s-1",
        audited_query=audited,
    )
    config = {"configurable": {"thread_id": "thread-1"}}

    for turn in range(1, 8):
        question = f"问题{turn}" if turn < 7 else "带工具的问题"
        await invoke_chat(graph, "thread-1", question, context, settings)

    state = await graph.aget_state(config)
    messages = state.values["messages"]

    assert state.values["summary"] == "第一期营收为100"
    assert len([m for m in messages if isinstance(m, HumanMessage)]) == 6
    assert not any(isinstance(m, ToolMessage) for m in messages)
    assert not any(
        isinstance(m, AIMessage) and m.tool_calls for m in messages
    )

    await invoke_chat(graph, "thread-1", "和第一期比较", context, settings)

    last_input = model.received[-1]
    assert any(
        "第一期营收为100" in str(message) for message in last_input
    )
    assert not any(
        isinstance(message, ToolMessage) for message in last_input
    )


async def test_missing_final_answer_raises_invalid_final_answer(tmp_path):
    settings = make_settings(tmp_path)
    model = ScriptedModel(responses=[AIMessage(content="")])
    graph = build_chat_graph(
        toolkit=FakeToolkit([]),
        model=model,
        summarizer=lambda history, previous: previous,
        checkpointer=InMemorySaver(),
        settings=settings,
    )
    context = RunContext(
        request_id=uuid4(),
        session_id="s-1",
        audited_query=FakeAuditedQuery(),
    )

    with pytest.raises(InvalidFinalAnswer):
        await invoke_chat(graph, "thread-1", "empty answer", context, settings)
