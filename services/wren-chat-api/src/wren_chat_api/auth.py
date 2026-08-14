"""Constant-time bearer authentication for the service key."""

from __future__ import annotations

import secrets
from typing import Annotated

from fastapi import Header

from wren_chat_api.config import Settings
from wren_chat_api.errors import AuthenticationFailed

_BEARER_PREFIX = "Bearer "


class ServiceKeyAuth:
    """FastAPI dependency comparing bearer tokens in constant time.

    The supplied key is never logged and never echoed back; failures raise
    ``AuthenticationFailed`` with a generic public message.
    """

    def __init__(self, settings: Settings) -> None:
        self._key = settings.api_key.get_secret_value().encode()

    async def __call__(
        self,
        authorization: Annotated[str | None, Header()] = None,
    ) -> None:
        if not authorization or not authorization.startswith(_BEARER_PREFIX):
            raise AuthenticationFailed()
        candidate = authorization[len(_BEARER_PREFIX) :].encode()
        if not secrets.compare_digest(candidate, self._key):
            raise AuthenticationFailed()
