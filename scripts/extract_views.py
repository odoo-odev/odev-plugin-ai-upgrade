import json
import os


def get_xml_host_id(env, view):
    """Return the XML ID for a given view."""
    ext_ids = view.get_external_id()
    if ext_ids and ext_ids.get(view.id):
        return ext_ids.get(view.id)

    data = env["ir.model.data"].search([("model", "=", "ir.ui.view"), ("res_id", "=", view.id)], limit=1)
    return f"{data.module}.{data.name}" if data else f"__export__.ir_ui_view_{view.id}"


def get_parent_chain(env, view):
    """Recursively extract the parent chain of a view."""
    chain = []
    curr = view.inherit_id
    while curr:
        chain.append(
            {
                "xml_id": get_xml_host_id(env, curr),
                "name": curr.name,
                "arch": curr.arch_db,
            }
        )
        curr = curr.inherit_id
    return chain


def validate_view(view):
    """Try to validate the view arch and return error if any."""
    try:
        if hasattr(view, "_check_xml"):
            view._check_xml()
        elif hasattr(view, "_validate_view_arch"):
            view._validate_view_arch()
    except Exception as e:
        return str(e)
    return None


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
        if not ids_to_search:
            with open(output_file, "w") as f:
                json.dump({"error": "No valid views found for provided IDs."}, f)
            return
        domain.append(("id", "in", ids_to_search))
    else:
        domain.extend(["|", ("name", "ilike", "Odoo Studio"), ("name", "ilike", "Studio")])

    views = env["ir.ui.view"].with_context(active_test=False).search(domain)
    result = []
    for view in views:
        result.append(
            {
                "id": view.id,
                "xml_id": get_xml_host_id(env, view),
                "name": view.name,
                "model": view.model,
                "arch": view.arch_db,
                "parent_chain": get_parent_chain(env, view),
                "validation_error": validate_view(view),
            }
        )

    with open(output_file, "w") as f:
        json.dump(result, f, indent=2)


if __name__ == "__main__":
    env = globals().get("env")
    if env:
        main(env)
