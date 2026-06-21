import pytest
from rove.actions import (ACTIONS, ACTION_NAMES, requires_approval, apply_edits,
                          _name_selector)


def test_action_catalog_has_core_actions():
    assert "CONTINUE" in ACTION_NAMES
    assert "DISMISS_OVERLAY" in ACTION_NAMES
    assert "FILL_FORM" in ACTION_NAMES
    assert "ESCALATE_HUMAN" in ACTION_NAMES
    assert "STOP_CRAWL" in ACTION_NAMES


def test_read_only_actions_need_no_approval():
    assert requires_approval("CONTINUE") is False
    assert requires_approval("DEPRIORITIZE_PREFIX") is False


def test_write_actions_need_approval():
    assert requires_approval("FILL_FORM") is True
    assert requires_approval("ESCALATE_HUMAN") is True
    assert requires_approval("STOP_CRAWL") is True


def test_apply_edits_overrides_params():
    params = {"fields": {"q": "shoes"}, "submit": True}
    edited = apply_edits(params, "q=boots")
    assert edited["fields"]["q"] == "boots"


def test_apply_edits_submit_is_top_level_not_a_field():
    params = {"fields": {"q": "shoes"}, "submit": True}
    edited = apply_edits(params, "submit=false")
    assert edited["submit"] is False           # coerced to bool, not the string "false"
    assert "submit" not in edited["fields"]    # not misrouted into the form fields


def test_apply_edits_selector_for_non_form_action():
    params = {"selector": "#old"}
    edited = apply_edits(params, "selector=#new")
    assert edited["selector"] == "#new"
    assert "fields" not in edited


def test_name_selector_escapes_quotes():
    sel = _name_selector('a"]; input[type=password')
    assert sel.startswith('[name="') and sel.endswith('"]')
    # The injected closing quote is escaped, so the attribute value can't break out.
    assert '\\"' in sel
