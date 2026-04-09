---
name: studio_upgrade
description:
    "Workflow and technical rules for investigating and fixing Odoo Studio views deactivated during upgrades. Includes
    research strategies and automated view patching."
---

# Studio Upgrade Skill

This skill provides mandatory rules and instructions for identifying, investigating, and fixing Odoo Studio views
deactivated during a database upgrade.

## 🛑 MANDATORY RULES (CONFIDENTIALITY & ARCHITECTURE)

1. **NO DIRECT DB ACCESS**: You MUST NOT attempt to access the production or customer database directly. For
   confidentiality reasons, all direct access is strictly prohibited.
2. **USE EXTRACTED DATA**: All necessary data is provided in `/home/odev/studio_views.json`. Use only this file for
   customer-specific view details.
3. **MIGRATION SCRIPTS ONLY**: You MUST NOT fix views directly in any database. You MUST generate a Python migration
   script (e.g., `<version>/post-migration.py`) and save it in the target module directory.
4. **USE LIBRARIES**: You MUST use `odoo.upgrade.util` and `custom_util` in your generated migration script.

## Investigation Workflow

To understand why a view is broken, follow these steps using the provided data:

1. **Locate the View**: Read `/home/odev/studio_views.json` and find the `xml_id` you are currently investigating.
2. **Analyze the Architectures**: Compare the `arch` of the broken view with the `parent_arch` provided in the JSON
   entry.
3. **Reference Odoo Research**: Use the target Odoo source code (typically at `/home/odev/worktrees/<version>/odoo`) to
   understand the new parent view structure. Use `grep` or `git grep` there to find how fields or elements were moved.
4. **Identify the XPath Failure**: Use the `error` message in the JSON file and your source code research to determine
   which XPath expression is no longer valid.

## Fixing Strategy

Generate a Python migration script that uses the PS Custom `custom_util` library or standard `upgrade-util`.

### Example Fix Patterns

-   **Rename a field in multiple views**:

    ```python
    from odoo.addons.base.maintenance import custom_util

    def migrate(cr, version):
        # Rename 'x_studio_old_field' to 'new_field' in all views of 'res.partner'
        custom_util.update_custom_views(cr, [('res.partner', 'x_studio_old_field', 'new_field')])
    ```

-   **Fix a specific View Architecture**: Use `custom_util.edit_views(cr, { xml_id: (Operations) })` with classes from
    `custom_util.views.operations`:

    ```python
    from custom_util.views import edit, operations

    def migrate(cr, version):
        edit.edit_views(cr, {
            'studio_customization.view_name': (
                # Simple string replacement
                operations.ReplaceValue('old_string', 'new_string'),
                # XPath-based attribute update
                operations.UpdateAttributes('//field[@name="my_field"]', invisible=None),
                # Field removal
                operations.RemoveFields('deprecated_field'),
            ),
        })
    ```

## Best Practices

-   **Batch Updates**: If many views are broken due to the same underlying change, use batch helpers like
    `update_custom_views`.
-   **Atomic Structural Changes**: If a field was renamed or a model refactored in Odoo Core (e.g., `uom.category`
    removal), ensure ALL affected Studio views are fixed in the same migration script pass. Perform a global analysis of
    all provided Studio views to identify every instance that needs adjustment before finalizing the script.
-   **Target Custom Modules**: If the Studio customization should be moved to a custom module, use
    `custom_util.transfer_custom_fields`.
-   **Technical Documentation**: For library help, refer to:
    -   `/home/odev/skills/odoo_upgrade_utils`
    -   `/home/odev/skills/custom_util`
