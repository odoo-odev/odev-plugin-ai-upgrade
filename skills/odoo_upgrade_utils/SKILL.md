---
name: odoo_upgrade_utils
description:
    "Standard Odoo upgrade helpers for field/model renaming, XMLID management, and view patching. Essential for safe
    data migrations across Odoo versions."
---

# Odoo Upgrade Utils Skill

Use the `odoo.upgrade.util` module for safe data migrations. These helpers handle complex database operations and ensure
consistent updates across the Odoo registry.

## Summary of Helpers

-   **`util.rename_field(cr, model, old_name, new_name)`**:
    -   Renames the column in the database and updates `ir.model.fields`, `ir.model.data`, and `ir.ui.view` references.
-   **`util.remove_field(cr, model, field_name)`**:
    -   Safely deletes a field and its data.
-   **`util.move_field_to_module(cr, model, fieldname, old_module, new_module)`**:
    -   Moves a field's metadata from one module to another.
-   **`util.rename_model(cr, old_name, new_name)`**:
    -   Renames the model and updates all its associated XMLIDs and metadata.
-   **`util.rename_xmlid(cr, old_xmlid, new_xmlid)`**:
    -   Renames an external ID. `old_xmlid` and `new_xmlid` should be fully qualified (e.g., `base.group_user`).
-   **`util.edit_view(cr, xmlid)`**:
    -   Context manager for editing a view's architecture using `lxml.etree`.
    -   Usage: `with util.edit_view(cr, 'module.view_id') as arch: ...`
-   **`util.merge_module(cr, old_name, into_name)`**:
    -   Merges one module's metadata into another.
-   **`util.module_installed(cr, module)`**:
    -   Returns `True` if the specified module is installed or being upgraded.
-   **`util.env(cr)`**:
    -   Returns an `odoo.api.Environment` instance for the current cursor.
-   **`util.table_of_model(cr, model)`**:
    -   Returns the database table name associated with an Odoo model.

## Best Practices

-   **Pre-migration**: Use for field and model renames so the new Odoo version can find the data during its own
    installation.
-   **Post-migration**: Use for complex data transformations that require the new Odoo registry to be partially loaded.
-   **Module discovery**: Migration scripts should be placed in `<module>/migrations/<target_ver>/`.

For more details, consult the
[Odoo Upgrade Utils Documentation](https://www.odoo.com/documentation/18.0/th/developer/reference/upgrades/upgrade_utils.html).
