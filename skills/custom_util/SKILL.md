---
name: custom_util
description:
    "PS-specific Odoo migration helpers for model/field renaming, Studio field transfers, and batch view updates,
    ensuring proper registry state management."
---

# PS Custom Upgrade Utils Skill

Use the `custom_util` module for PS-specific Odoo data migrations. These helpers extend the standard `upgrade-util`
library to handle common PS patterns and ensure technical debt (like Studio fields) is cleanly versioned.

## Summary of Helpers

-   **`custom_util.custom_rename_model(cr, old_name, new_name)`**:
    -   Renames the model and ensures its state is set to `'base'`. Required for custom models to be correctly handled
        by the Odoo registry.
-   **`custom_util.custom_rename_field(cr, model, old_name, new_name)`**:
    -   Renames a field and ensures its state is set to `'base'`. Use this for all `x_` or `x_studio_` fields.
-   **`custom_util.transfer_custom_fields(cr, src_module, dest_module, fields_to_transfer)`**:
    -   Moves fields between modules. `fields_to_transfer` is a list of `(model, field)` or
        `(model, old_field, new_field)` tuples.
-   **`custom_util.custom_rename_module(cr, old_name, new_name)`**:
    -   Safely renames a custom module, handling cases where a new module record might already exist.
-   **`custom_util.update_custom_views(cr, list_fields)`**:
    -   Batch updates multiple fields in ALL views. `list_fields` is a list of `(model, old_field, new_field)` tuples.
-   **`custom_util.views.edit.edit_views(cr, { xmlid: (Operations) })`**:
    -   Fine-grained view modification using specific operation objects:
        -   **`operations.ReplaceValue(search, replace)`**: Search/replace strings or regex.
        -   **`operations.UpdateAttributes(xpath, **attrs)`**: Update or remove (if `None`) XML attributes.
        -   **`operations.RemoveFields(names)`**: Shorthand to remove one or more fields.
        -   **`operations.RenameElements(old, new)`**: Renames elements (and their labels) while keeping other
            attributes.
-   **`custom_util.update_related_field(cr, list_fields)`**:
    -   Updates `related` definitions on fields that point to renamed custom fields.
-   **`custom_util.merge_groups(cr, src_xmlid, dest_xmlid)`**:
    -   Merges one security group into another, remapping users and updating references.
-   **`custom_util.merge_model_and_data(cr, source_model, target_model, copy_fields, set_values=None)`**:
    -   Merges one model into another, copying the records and remapping all IDs and references.
-   **`custom_util.rename_xmlids(cr, pairs, detect_module=True)`**:
    -   Renames a batch of XML IDs. `pairs` is a list of `(old_xmlid, new_xmlid)` tuples.

## Usage Example

```python
from odoo.upgrade import util
from custom_util import custom_util

def migrate(cr, version):
    # Rename a custom model and field
    custom_util.custom_rename_model(cr, 'x_my_old_model', 'my.new.model')
    custom_util.custom_rename_field(cr, 'my.new.model', 'x_my_old_field', 'my_new_field')

    # Transfer Studio fields
    custom_util.transfer_custom_fields(cr, 'studio_customization', 'my_module', [
        ('res.partner', 'x_studio_custom_field', 'custom_field'),
    ])

    # Bulk update views for renamed fields
    custom_util.update_custom_views(cr, [
        ('res.partner', 'x_old_field', 'new_field'),
    ])
```

## Best Practices

-   **Registry Health**: Always use `custom_util` for custom/studio fields to keep the Odoo registry in the standard
    `'base'` state.
-   **Documentation**: If a field is renamed, verify if other fields' `related` or `depends` attributes need updating
    via `update_related_field`.
