"""Tests for ``common/gates.py`` post-flight gate helpers.

Loads the module by file path (importlib) so the test runs without importing
the plugin package (whose ``__init__.py`` pulls in ``odev``).
"""

import importlib.util
from pathlib import Path

import pytest


def _plugin_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "__manifest__.py").exists():
            return parent
    raise RuntimeError("Could not locate the plugin root (no __manifest__.py found).")


def _load_gates_module():
    path = _plugin_root() / "common" / "gates.py"
    spec = importlib.util.spec_from_file_location("upg_gates_under_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


gates = _load_gates_module()


# --- iter_cited_shas ---------------------------------------------------------


def test_cited_shas_from_url_form():
    body = "Fix it.\n  Source: https://github.com/odoo/odoo/commit/d2cb95fbc8d66d2c5350a30b037f3cf5c89bc2d3\n"
    assert gates.iter_cited_shas(body) == [("odoo", "d2cb95fbc8d66d2c5350a30b037f3cf5c89bc2d3")]


def test_cited_shas_detects_enterprise():
    body = "Source: https://github.com/odoo/enterprise/commit/2e6adbe76eaa1fbdf1e5531b74b7c6c1577a2891"
    assert gates.iter_cited_shas(body) == [("enterprise", "2e6adbe76eaa1fbdf1e5531b74b7c6c1577a2891")]


def test_cited_shas_bare_form_defaults_to_odoo():
    assert gates.iter_cited_shas("Source: d49fc64cc077 (orm: deprecate)") == [("odoo", "d49fc64cc077")]


def test_cited_shas_bare_form_with_explicit_repo():
    assert gates.iter_cited_shas("Source: enterprise 14eb7afe3f6 (budget)") == [("enterprise", "14eb7afe3f6")]


def test_cited_shas_indented_trailer_is_found():
    """Trailers are indented inside bullet lists, so anchoring to line start misses them."""
    assert gates.iter_cited_shas("- did a thing.\n    Source: abcdef1234567890\n")


def test_no_source_line_yields_nothing():
    assert gates.iter_cited_shas("Source: not identified (compared against sale_order.py)") == []


# --- dead_tokens_for --------------------------------------------------------


def test_dead_tokens_include_kanban_box_at_19():
    assert "kanban-box" in gates.dead_tokens_for("19.0")


def test_dead_tokens_exclude_kanban_box_before_19():
    assert "kanban-box" not in gates.dead_tokens_for("18.0")


def test_dead_tokens_include_attrs_at_18():
    assert "attrs=" in gates.dead_tokens_for("18.0")


# --- function_bodies / gutted_overrides -------------------------------------

WITH_LOGIC = '''
class HolidaysRequest(models.Model):
    @api.model_create_multi
    def create(self, vals_list):
        """Doc."""
        res = super().create(vals_list)
        for holiday in res:
            holiday._validate()
        return res
'''

STUBBED = '''
class HolidaysRequest(models.Model):
    @api.model_create_multi
    def create(self, vals_list):
        """Doc."""
        return super().create(vals_list)
'''

DELETED = """
class HolidaysRequest(models.Model):
    def other(self):
        return 1
"""


def test_gutted_override_is_detected():
    findings = gates.gutted_overrides(WITH_LOGIC, STUBBED)
    assert len(findings) == 1
    assert "HolidaysRequest.create" in findings[0]


def test_deleting_the_method_entirely_is_not_flagged():
    """Removing an obsolete override is the correct fix and must not be reported."""
    assert gates.gutted_overrides(WITH_LOGIC, DELETED) == []


def test_adding_logic_back_is_not_flagged():
    assert gates.gutted_overrides(STUBBED, WITH_LOGIC) == []


def test_unchanged_source_is_not_flagged():
    assert gates.gutted_overrides(WITH_LOGIC, WITH_LOGIC) == []


def test_already_stubbed_before_is_not_flagged():
    """A pre-existing bare super() is the customer's code, not a regression from this run."""
    assert gates.gutted_overrides(STUBBED, STUBBED) == []


def test_pass_body_counts_as_a_stub():
    before = "class A:\n    def m(self):\n        x = 1\n        return x\n"
    after = "class A:\n    def m(self):\n        pass\n"
    assert gates.gutted_overrides(before, after)


def test_docstring_only_change_is_not_flagged():
    before = "class A:\n    def m(self):\n        return super().m()\n"
    after = 'class A:\n    def m(self):\n        """Now documented."""\n        return super().m()\n'
    assert gates.gutted_overrides(before, after) == []


@pytest.mark.parametrize("source", ["def broken(:\n", "", "   "])
def test_unparseable_source_yields_no_bodies(source):
    assert gates.function_bodies(source) == {}


def test_unparseable_after_does_not_raise():
    assert gates.gutted_overrides(WITH_LOGIC, "def broken(:\n") == []


def test_nested_class_names_are_qualified():
    source = "class Outer:\n    class Inner:\n        def m(self):\n            return 1\n"
    assert "Outer.Inner.m" in gates.function_bodies(source)
