from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


def test_dm8_driver_is_available_as_an_optional_and_all_extra() -> None:
    pyproject = Path(__file__).parents[2] / "pyproject.toml"
    project = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]
    extras = project["optional-dependencies"]

    assert extras["dm8"] == ["dmPython>=2.5"]
    assert "dm8" in extras["all"][0].split("[")[1].split("]")[0].split(",")
