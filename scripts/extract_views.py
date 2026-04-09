import json
import os


def main(env):
    output_file = os.environ.get("ODEV_STUDIO_OUT_FILE")
    if not output_file:
        return

    view_ids = os.environ.get("ODEV_STUDIO_VIEW_IDS", "")
    domain: list[tuple[str, str, object] | str] = [("active", "=", False)]

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
        # Recursive Parent Chain extraction
        parent_chain = []
        curr = view.inherit_id
        while curr:
            # Try to get XML ID for parent
            p_xml_id = None
            p_ext_ids = curr.get_external_id()
            if p_ext_ids and p_ext_ids.get(curr.id):
                p_xml_id = p_ext_ids.get(curr.id)

            if not p_xml_id:
                p_data = env["ir.model.data"].search([("model", "=", "ir.ui.view"), ("res_id", "=", curr.id)], limit=1)
                p_xml_id = f"{p_data.module}.{p_data.name}" if p_data else f"__export__.ir_ui_view_{curr.id}"

            parent_chain.append(
                {
                    "xml_id": p_xml_id,
                    "name": curr.name,
                    "arch": curr.arch_db,
                }
            )
            curr = curr.inherit_id

        # Try to get a real XML ID for current view
        xml_id = None
        ext_ids = view.get_external_id()
        if ext_ids and ext_ids.get(view.id):
            xml_id = ext_ids.get(view.id)

        if not xml_id:
            data = env["ir.model.data"].search([("model", "=", "ir.ui.view"), ("res_id", "=", view.id)], limit=1)
            xml_id = f"{data.module}.{data.name}" if data else f"__export__.ir_ui_view_{view.id}"

        # Capture validation error
        validation_error = None
        try:
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
                "parent_chain": parent_chain,
                "error": validation_error,
            }
        )

    with open(output_file, "w") as f:
        json.dump(result, f, indent=2)


if __name__ == "__main__":
    env = globals().get("env")
    if env:
        main(env)
