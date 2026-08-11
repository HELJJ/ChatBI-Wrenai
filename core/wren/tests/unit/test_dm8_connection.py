from __future__ import annotations

import pytest

from wren.model.data_source import DataSource
from wren.model.field_registry import get_fields, get_selectable_datasources

pytestmark = pytest.mark.unit


def test_dm8_connection_info_defaults_and_masks_password() -> None:
    data_source = DataSource("dm8")

    info = data_source.get_connection_info(
        {
            "host": "dm.internal",
            "user": "app",
            "password": "secret",
            "schema": "APP",
        }
    )

    assert info.__class__.__name__ == "DM8ConnectionInfo"
    assert info.host == "dm.internal"
    assert info.port == "5236"
    assert info.dm_schema == "APP"
    assert info.password.get_secret_value() == "secret"


def test_dm8_fields_are_available_to_profile_clients() -> None:
    assert "dm8" in get_selectable_datasources()

    fields = {field.name: field for field in get_fields("dm8")}

    assert set(fields) == {"host", "port", "user", "password", "dm_schema"}
    assert fields["port"].default == "5236"
    assert fields["password"].sensitive is True
    assert fields["dm_schema"].alias == "schema"
    assert fields["dm_schema"].label == "Schema"
