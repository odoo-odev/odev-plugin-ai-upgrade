"""Render-contract tests for the verification directives in ``templates/upgrade_prompt.md.j2``.

Framework-free: renders the template directly with Jinja2, mirroring
``commands/upgrade.py::_build_final_prompt``.
"""

from pathlib import Path

import jinja2
import pytest


def _plugin_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "__manifest__.py").exists():
            return parent
    raise RuntimeError("Could not locate the plugin root (no __manifest__.py found).")


def _render(**overrides) -> str:
    template_path = _plugin_root() / "templates" / "upgrade_prompt.md.j2"
    template = jinja2.Template(template_path.read_text(encoding="utf-8"))
    context = {
        "task_id": "12345",
        "from_ver": "17.0",
        "target_ver": "19.0",
        "from_odoo_path": "/wt/17.0",
        "target_odoo_path": "/wt/19.0",
        "project_path": "/proj",
        "upgrade_instructions": "",
        "k_path": "/knowledge",
        "no_ruff": False,
        "is_ps_custom": False,
        "comment": "",
        "submodules": False,
        "modules": [{"name": "sale_x", "path": "/proj/sale_x"}],
    }
    context.update(overrides)
    return template.render(**context)


# --- the run's blind spot must be stated, never implied ----------------------


def test_database_resident_customisation_is_declared_unreachable():
    out = _render()
    assert "Outside your reach" in out
    assert "no customer database is provisioned to this run" in out


@pytest.mark.parametrize(
    "area",
    ["Studio views", "cowed", "automations", "mail.template", "ir_filters"],
)
def test_each_unreachable_area_is_named(area):
    assert area in _render()


def test_completeness_claims_are_forbidden():
    out = _render()
    assert "never describe the upgrade as complete or verified" in out
    assert "Not verified by this run" in out


# --- Source: resolution ------------------------------------------------------


def test_source_sha_must_be_resolved():
    out = _render()
    assert "cat-file -e <sha>^{commit}" in out
    assert "Source: not identified" in out
    assert "Never write a SHA you have not resolved" in out


def test_target_path_is_interpolated_into_the_resolve_command():
    assert "git -C /wt/19.0 cat-file -e" in _render(target_odoo_path="/wt/19.0")


# --- override boundaries -----------------------------------------------------


def test_override_boundary_rules_present():
    out = _render()
    assert "Override boundaries" in out
    assert "Never reduce an override to a bare `super()` call" in out


def test_compute_loop_rule_present():
    assert "assign to the loop variable, never to `self`" in _render()


def test_unverified_claims_are_banned():
    assert "No unverified claims" in _render()


# --- efficiency must not licence skipping verification -----------------------


def test_efficiency_directive_does_not_licence_skipping_verification():
    out = _render()
    assert "never skip *verifying*" in out
    assert "Reason only about the immediate action." not in out


# --- pre-existing contract stays intact --------------------------------------


def test_workflow_still_delegates_to_the_skill():
    assert "Refer to the `odoo_upgrade_skill`" in _render()


@pytest.mark.parametrize("no_ruff", [False, True])
def test_ruff_sniper_gated_on_no_ruff(no_ruff):
    out = _render(no_ruff=no_ruff)
    assert ("Ruff Sniper" in out) is (not no_ruff)


def test_modules_are_listed():
    assert "`sale_x`" in _render()
