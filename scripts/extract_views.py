import json
import os


def main(env):
    output_file = os.environ.get("ODEV_STUDIO_OUT_FILE")
    if not output_file:
        return

    view_ids = os.environ.get("ODEV_STUDIO_VIEW_IDS", "")
    domain = [("active", "=", False)]

    if view_ids:
        ids_to_search = []
        for v in view_ids.split(","):
            v = v.strip()
            if not v:
                continue
            if "." in v:
                record = env.ref(v, raise_if_not_found=False)
                if record and record._name == "ir.ui.view":
                    ids_to_search.append(record.id)
            elif v.isdigit():
                ids_to_search.append(int(v))
        if ids_to_search:
            domain.append(("id", "in", ids_to_search))
        else:
            with open(output_file, "w") as f:
                json.dump({"error": "No valid views found for provided IDs."}, f)
            return
    else:
        domain.append("|")
        domain.append(("name", "ilike", "Odoo Studio"))
        domain.append(("name", "ilike", "Studio"))

    views = env["ir.ui.view"].with_context(active_test=False).search(domain)

    result = []
    for view in views:
        parent_arch = view.inherit_id.arch_db if view.inherit_id else None
        parent_name = view.inherit_id.name if view.inherit_id else None

        # Try to get a real XML ID
        xml_id = None
        ext_ids = view.get_external_id()
        if ext_ids and ext_ids.get(view.id):
            xml_id = ext_ids.get(view.id)

        if not xml_id:
            # Fallback to searching ir_model_data if get_external_id fails
            data = env["ir.model.data"].search([("model", "=", "ir.ui.view"), ("res_id", "=", view.id)], limit=1)
            if data:
                xml_id = f"{data.module}.{data.name}"
            else:
                xml_id = f"__export__.ir_ui_view_{view.id}"

        parent_xml_id = None
        if view.inherit_id:
            parent_ext_ids = view.inherit_id.get_external_id()
            if parent_ext_ids and parent_ext_ids.get(view.inherit_id.id):
                parent_xml_id = parent_ext_ids.get(view.inherit_id.id)

        # Capture validation error
        validation_error = None
        try:
            # Check if we can validate the view.
            # In some Odoo versions it's _check_xml, in others it might be different.
            if hasattr(view, "_check_xml"):
                view._check_xml()
            elif hasattr(view, "_validate_view_arch"):
                view._validate_view_arch()
        except Exception as e:
            validation_error = str(e)

        result.append(
            {
                "id": view.id,
                "xml_id": xml_id,
                "name": view.name,
                "model": view.model,
                "arch": view.arch_db,
                "parent_xml_id": parent_xml_id,
                "parent_name": parent_name,
                "parent_arch": parent_arch,
                "error": validation_error,
            }
        )

    with open(output_file, "w") as f:
        json.dump(result, f, indent=2)


if __name__ == "__main__":
    main(env)  # type: ignore
