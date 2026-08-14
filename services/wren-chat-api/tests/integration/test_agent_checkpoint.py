"""Checkpoint isolation and restart recovery for the chat agent."""

from uuid import uuid4

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from pydantic import Field

from psycopg_pool import AsyncConnectionPool

from wren_chat_api.agent import build_chat_graph, invoke_chat
from wren_chat_api.audited_query import RunContext

FIRST_PERIOD_FACT = "第一期营收为100"


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


class FakeToolkit:
    def get_tools(self, *, include_memory_write: bool = True, **kwargs):
        return []

    def system_prompt(self, *, tools=None) -> str:
        return "wren system prompt"


class FakeAuditedQuery:
    async def execute(self, context, sql, limit=100):
        return {
            "ok": True,
            "content": "rows",
            "content_truncated": False,
            "returned_row_count": 1,
            "result_truncated": False,
        }


def _make_graph(settings, model, checkpointer):
    return build_chat_graph(
        toolkit=FakeToolkit(),
        model=model,
        summarizer=lambda history, previous: previous,
        checkpointer=checkpointer,
        settings=settings,
    )


def _make_context(session_id: str) -> RunContext:
    return RunContext(
        request_id=uuid4(),
        session_id=session_id,
        audited_query=FakeAuditedQuery(),
    )


@pytest.mark.asyncio
async def test_thread_survives_restart_and_other_threads_are_isolated(
    postgres_url, settings
):
    def make_checkpoint_pool() -> AsyncConnectionPool:
        # Default row factory (tuples): the checkpointer indexes columns
        # positionally, so dict_row pools are incompatible with it.
        return AsyncConnectionPool(
            conninfo=postgres_url,
            kwargs={"autocommit": True, "prepare_threshold": 0},
            open=False,
            name="wren-chat-checkpoint-test",
        )

    pool = make_checkpoint_pool()
    await pool.open()
    await pool.wait()
    try:
        checkpointer = AsyncPostgresSaver(pool)
        await checkpointer.setup()

        thread_a = f"thread-a-{uuid4()}"
        thread_b = f"thread-b-{uuid4()}"

        first_model = ScriptedModel(responses=[AIMessage(content=FIRST_PERIOD_FACT)])
        context_a = _make_context("s-a")
        answer = await invoke_chat(
            _make_graph(settings, first_model, checkpointer),
            thread_a,
            "第一期营收是多少",
            context_a,
            settings,
        )
        assert answer == FIRST_PERIOD_FACT

        # Simulate a restart: a brand-new checkpointer instance reading the
        # same database, a new graph, and a new model.
        await pool.close()
        pool = make_checkpoint_pool()
        await pool.open()
        await pool.wait()
        restarted_checkpointer = AsyncPostgresSaver(pool)
        await restarted_checkpointer.setup()

        followup_model = ScriptedModel(
            responses=[AIMessage(content="第二期为120")]
        )
        answer = await invoke_chat(
            _make_graph(settings, followup_model, restarted_checkpointer),
            thread_a,
            "那第二期呢",
            context_a,
            settings,
        )
        assert answer == "第二期为120"
        assert any(
            FIRST_PERIOD_FACT in str(message)
            for message in followup_model.received[-1]
        )

        isolated_model = ScriptedModel(responses=[AIMessage(content="没有上下文")])
        answer = await invoke_chat(
            _make_graph(settings, isolated_model, restarted_checkpointer),
            thread_b,
            "那第二期呢",
            _make_context("s-b"),
            settings,
        )
        assert answer == "没有上下文"
        assert not any(
            FIRST_PERIOD_FACT in str(message)
            for message in isolated_model.received[-1]
        )
    finally:
        await pool.close()
