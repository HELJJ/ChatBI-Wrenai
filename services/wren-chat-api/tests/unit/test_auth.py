"""Unit contracts for constant-time service key authentication."""

import pytest

from wren_chat_api.auth import ServiceKeyAuth
from wren_chat_api.config import Settings
from wren_chat_api.errors import AuthenticationFailed


def make_auth() -> ServiceKeyAuth:
    settings = Settings(
        state_database_url="postgresql://user:pass@localhost:5432/wren_test",
        api_key="test-key",
        project_path="/tmp/wren-project",
        model="test-model",
    )
    return ServiceKeyAuth(settings)


@pytest.mark.asyncio
async def test_valid_key_passes() -> None:
    auth = make_auth()

    await auth(authorization="Bearer test-key")


@pytest.mark.asyncio
async def test_missing_or_malformed_header_is_rejected() -> None:
    auth = make_auth()

    with pytest.raises(AuthenticationFailed):
        await auth(authorization=None)
    with pytest.raises(AuthenticationFailed):
        await auth(authorization="test-key")
    with pytest.raises(AuthenticationFailed):
        await auth(authorization="Basic dGVzdC1rZXk=")


@pytest.mark.asyncio
async def test_wrong_key_is_rejected_without_echoing_it() -> None:
    auth = make_auth()

    with pytest.raises(AuthenticationFailed) as excinfo:
        await auth(authorization="Bearer wrong-key")

    assert "wrong-key" not in str(excinfo.value)


@pytest.mark.asyncio
async def test_non_ascii_candidate_does_not_crash_comparison() -> None:
    auth = make_auth()

    with pytest.raises(AuthenticationFailed):
        await auth(authorization="Bearer 你好")
