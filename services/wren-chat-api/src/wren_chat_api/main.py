"""Uvicorn entrypoint: ``uvicorn wren_chat_api.main:app``."""

from wren_chat_api.app import create_app

app = create_app()
