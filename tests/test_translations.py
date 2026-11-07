"""Every translation has the same keys as strings.json."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

COMPONENT = Path(__file__).parent.parent / "custom_components" / "ajax_doorbell"


def _keys(data: dict, prefix: str = "") -> set[str]:
    keys = set()
    for key, value in data.items():
        path = f"{prefix}.{key}" if prefix else key
        keys |= _keys(value, path) if isinstance(value, dict) else {path}
    return keys


@pytest.mark.parametrize("language", ["en", "fr"])
def test_translation_keys(language: str) -> None:
    """No key is missing or extra."""
    source = _keys(json.loads((COMPONENT / "strings.json").read_text()))
    translated = _keys(json.loads((COMPONENT / "translations" / f"{language}.json").read_text()))
    assert translated == source
