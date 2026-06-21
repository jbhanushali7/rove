import pytest

from rove.resource_blocking import DEFAULT_BLOCKED_RESOURCES, resolve_blocked_types


def test_none_resolves_to_default():
    assert resolve_blocked_types(None) == DEFAULT_BLOCKED_RESOURCES


def test_empty_list_blocks_nothing():
    assert resolve_blocked_types([]) == frozenset()


def test_custom_list_resolves_to_frozenset():
    assert resolve_blocked_types(["image", "font"]) == frozenset({"image", "font"})


def test_bare_string_raises_value_error():
    with pytest.raises(ValueError):
        resolve_blocked_types("image")
