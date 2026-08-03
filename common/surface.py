"""Static enumeration of what an Odoo module actually does.

The behaviour inventory an upgrade must preserve cannot be taken from the manifest: in
custom code it is routinely empty, partial or stale. It *can* be read off the source, and
the parts that matter are mechanically enumerable — models, fields, computes, overrides,
inherited views, security rules. Listing them turns "cover the module's behaviour" into a
checklist that can be worked through item by item, instead of a judgement call that
silently skips whatever the reader did not think of.

This is deliberately a *surface*, not an analysis: it says a compute exists, not what it
computes. Understanding is the reader's job; not missing anything is this module's.
"""

import ast
import csv
from dataclasses import dataclass, field
from pathlib import Path

# The XML parsed here is the developer's own module source, not untrusted input, so the
# stdlib parser is appropriate and `defusedxml` would be a dependency for nothing.
from xml.etree import ElementTree as ET

from odev.common.logging import logging


logger = logging.getLogger(__name__)

ORM_OVERRIDES = frozenset(
    {
        "create",
        "write",
        "unlink",
        "copy",
        "read",
        "read_group",
        "search",
        "search_read",
        "search_fetch",
        "default_get",
        "name_get",
        "name_create",
        "name_search",
        "_name_search",
        "fields_get",
        "get_view",
        "web_read_group",
        "onchange",
    }
)
"""ORM methods whose override changes framework behaviour rather than adding a feature."""

MODEL_BASES = frozenset({"Model", "TransientModel", "AbstractModel"})

DECORATOR_PREFIXES = ("depends", "constrains", "onchange", "model_create_multi", "model", "ondelete", "autovacuum")


@dataclass
class ModelSurface:
    """What a module adds to, or changes about, a single Odoo model."""

    name: str
    is_new: bool = False
    fields: list[str] = field(default_factory=list)
    computes: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    onchanges: list[str] = field(default_factory=list)
    overrides: list[str] = field(default_factory=list)
    methods: list[str] = field(default_factory=list)
    sql_constraints: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not any((self.fields, self.computes, self.constraints, self.onchanges, self.overrides, self.methods))


@dataclass
class ModuleSurface:
    """Everything a module declares, grouped by the kind of behaviour it represents."""

    models: list[ModelSurface] = field(default_factory=list)
    inherited_views: list[str] = field(default_factory=list)
    new_views: list[str] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)
    menus: list[str] = field(default_factory=list)
    data_records: list[str] = field(default_factory=list)
    security: list[str] = field(default_factory=list)
    form_fields: dict[str, list[str]] = field(default_factory=dict)
    """``{model: [field, ...]}`` for fields the module puts on a **form** view.

    A field on a form has an entry path the ORM does not exercise: the onchange cycle on an
    unsaved record, where `id` and `create_date` are still falsy. Computes that quietly
    assume a saved record fail there and nowhere else, so these fields need a `Form`-based
    test on top of any `create()` one.

    For inherited views the form-ness is inferred from the referenced view's name, which is
    a heuristic — `sale.view_order_form` is a form, but a badly named core view could be
    missed. Over-reporting is harmless here; under-reporting is what costs behaviour.
    """
    unreadable: list[str] = field(default_factory=list)
    """Files that could not be parsed, reported rather than silently dropped."""
    redeclared_models: list[str] = field(default_factory=list)
    """Models whose ``_name`` is declared by more than one class.

    Legal Python and silent at install: the last class simply shadows the earlier ones.
    Worth reporting, because it usually means a copy-paste that was never noticed.
    """

    @property
    def is_empty(self) -> bool:
        return not any((self.models, self.inherited_views, self.new_views, self.actions, self.menus, self.data_records))


def _decorator_name(node: ast.expr) -> str | None:
    """Return the trailing name of a decorator, e.g. ``api.depends(...)`` -> ``depends``."""
    if isinstance(node, ast.Call):
        node = node.func
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return None


def _decorator_args(node: ast.expr) -> list[str]:
    """Return the literal string arguments of a decorator call."""
    if not isinstance(node, ast.Call):
        return []
    return [arg.value for arg in node.args if isinstance(arg, ast.Constant) and isinstance(arg.value, str)]


def _field_signature(name: str, call: ast.Call) -> str:
    """Render a field declaration compactly, keeping the keywords that change behaviour."""
    field_type = call.func.attr if isinstance(call.func, ast.Attribute) else "Field"
    interesting = ("compute", "inverse", "related", "store", "required", "default", "readonly", "comodel_name")
    details = []
    for keyword in call.keywords:
        if keyword.arg not in interesting:
            continue
        if isinstance(keyword.value, ast.Constant):
            details.append(f"{keyword.arg}={keyword.value.value!r}")
        else:
            details.append(f"{keyword.arg}=…")
    suffix = f" [{', '.join(details)}]" if details else ""
    return f"{name}: {field_type}{suffix}"


def _merge_unique(target: list[str], extra: list[str]) -> None:
    """Append entries not already present, preserving order."""
    for item in extra:
        if item not in target:
            target.append(item)


def _is_model_class(node: ast.ClassDef) -> bool:
    return any(isinstance(base, ast.Attribute) and base.attr in MODEL_BASES for base in node.bases)


def _model_names(node: ast.ClassDef) -> tuple[str | None, list[str]]:
    """Return ``(_name, [_inherit, ...])`` as declared on a model class."""
    declared_name: str | None = None
    inherits: list[str] = []

    for statement in node.body:
        if not isinstance(statement, ast.Assign) or not statement.targets:
            continue
        target = statement.targets[0]
        if not isinstance(target, ast.Name):
            continue

        if target.id == "_name" and isinstance(statement.value, ast.Constant):
            declared_name = str(statement.value.value)
        elif target.id == "_inherit":
            if isinstance(statement.value, ast.Constant):
                inherits.append(str(statement.value.value))
            elif isinstance(statement.value, ast.List | ast.Tuple):
                inherits.extend(
                    str(element.value) for element in statement.value.elts if isinstance(element, ast.Constant)
                )

    return declared_name, inherits


def _collect_assignment(statement: ast.Assign, surface: ModelSurface) -> None:
    """Record a field declaration or an ``_sql_constraints`` list."""
    target = statement.targets[0]
    if not isinstance(target, ast.Name):
        return

    if (
        not target.id.startswith("_")
        and isinstance(statement.value, ast.Call)
        and isinstance(statement.value.func, ast.Attribute)
        and isinstance(statement.value.func.value, ast.Name)
        and statement.value.func.value.id == "fields"
    ):
        surface.fields.append(_field_signature(target.id, statement.value))
    elif target.id == "_sql_constraints":
        surface.sql_constraints.extend(
            str(element.elts[0].value)
            for element in getattr(statement.value, "elts", [])
            if isinstance(element, ast.Tuple) and element.elts and isinstance(element.elts[0], ast.Constant)
        )


def _collect_model(node: ast.ClassDef) -> ModelSurface | None:
    """Build the surface of a single model class."""
    declared_name, inherits = _model_names(node)
    name = declared_name or (inherits[0] if inherits else None)
    if not name:
        return None

    surface = ModelSurface(name=name, is_new=bool(declared_name) and name not in inherits)

    for statement in node.body:
        if isinstance(statement, ast.Assign) and statement.targets:
            _collect_assignment(statement, surface)

        elif isinstance(statement, ast.FunctionDef):
            decorators = {_decorator_name(decorator) for decorator in statement.decorator_list}
            depends: list[str] = []
            for decorator in statement.decorator_list:
                if _decorator_name(decorator) in ("depends", "constrains", "onchange"):
                    depends.extend(_decorator_args(decorator))
            trigger = f" ← {', '.join(depends)}" if depends else ""

            if "depends" in decorators or statement.name.startswith(("_compute_", "_inverse_", "_search_")):
                surface.computes.append(f"{statement.name}(){trigger}")
            elif "constrains" in decorators:
                surface.constraints.append(f"{statement.name}(){trigger}")
            elif "onchange" in decorators:
                surface.onchanges.append(f"{statement.name}(){trigger}")
            elif statement.name in ORM_OVERRIDES:
                surface.overrides.append(f"{statement.name}()")
            elif not statement.name.startswith("_"):
                surface.methods.append(f"{statement.name}()")

    return surface


def _scan_python(module_path: Path, surface: ModuleSurface) -> None:
    """Collect the model surface from every non-test Python file in the module."""
    by_name: dict[str, ModelSurface] = {}
    declaring_classes: dict[str, int] = {}

    for python_file in sorted(module_path.rglob("*.py")):
        if "tests" in python_file.relative_to(module_path).parts:
            continue
        try:
            tree = ast.parse(python_file.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, SyntaxError) as error:
            surface.unreadable.append(f"{python_file.relative_to(module_path)} ({type(error).__name__})")
            continue

        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef) or not _is_model_class(node):
                continue
            model = _collect_model(node)
            if model is None or model.is_empty:
                continue

            if model.is_new:
                declaring_classes[model.name] = declaring_classes.get(model.name, 0) + 1

            if existing := by_name.get(model.name):
                _merge_unique(existing.fields, model.fields)
                _merge_unique(existing.computes, model.computes)
                _merge_unique(existing.constraints, model.constraints)
                _merge_unique(existing.onchanges, model.onchanges)
                _merge_unique(existing.overrides, model.overrides)
                _merge_unique(existing.methods, model.methods)
                _merge_unique(existing.sql_constraints, model.sql_constraints)
                existing.is_new = existing.is_new or model.is_new
            else:
                by_name[model.name] = model

    surface.models = sorted(by_name.values(), key=lambda model: (not model.is_new, model.name))
    surface.redeclared_models = sorted(name for name, count in declaring_classes.items() if count > 1)


def _record_label(record: ET.Element) -> str:
    return str(record.get("id") or record.get("name") or "?")


def _record_field_text(record: ET.Element, name: str) -> str | None:
    """Return the text of a record-level ``<field name="...">``."""
    element = record.find(f"field[@name='{name}']")
    return element.text.strip() if element is not None and element.text else None


def _view_anchors(arch: ET.Element) -> list[str]:
    """Return the inheritance anchors a view arch targets.

    Both styles matter: ``<xpath expr="..."/>`` and the far more common
    ``<field name="x" position="after">``. An anchor that still exists but has moved, or
    that left the subview entirely, is the most frequent silent breakage of an upgrade.
    """
    anchors: list[str] = []
    for element in arch.iter():
        if element.tag == "xpath" and element.get("expr"):
            anchors.append(f"xpath {element.get('expr')}")
        elif element.get("position") and element.tag != "xpath":
            target = element.get("name") or element.tag
            anchors.append(f"{element.tag} {target!r} position={element.get('position')}")
    return sorted(set(anchors))


def _arch_fields(arch: ET.Element) -> list[str]:
    """Return the field names a view arch *places* on screen, in order of appearance.

    Excludes the ``<field name="arch">`` wrapper itself, and any element carrying a
    ``position`` attribute: those name the anchor the module attaches to, which belongs to
    the view being inherited, not to this module. Anchors are reported separately.
    """
    names: list[str] = []
    for element in arch.iter("field"):
        name = element.get("name")
        if element is arch or not name or element.get("position"):
            continue
        if name not in names:
            names.append(name)
    return names


def _is_form_view(record: ET.Element, arch: ET.Element) -> bool:
    """Whether this view puts fields on a form.

    Direct for a view that declares its own arch; a name heuristic for an inherited one,
    whose root element belongs to the view it extends.
    """
    if arch.find(".//form") is not None or arch.tag == "form":
        return True
    inherit = record.find("field[@name='inherit_id']")
    reference = (inherit.get("ref") or "") if inherit is not None else ""
    return "form" in reference.lower() or "form" in _record_label(record).lower()


def _scan_xml(module_path: Path, surface: ModuleSurface) -> None:
    """Collect views, actions, menus and data records declared in XML."""
    for xml_file in sorted(module_path.rglob("*.xml")):
        if "tests" in xml_file.relative_to(module_path).parts:
            continue
        try:
            root = ET.parse(xml_file).getroot()  # noqa: S314
        except (OSError, ET.ParseError) as error:
            surface.unreadable.append(f"{xml_file.relative_to(module_path)} ({type(error).__name__})")
            continue

        for menu in root.iter("menuitem"):
            surface.menus.append(_record_label(menu))

        for record in root.iter("record"):
            model = record.get("model", "")
            label = _record_label(record)

            if model == "ir.ui.view":
                arch = record.find("field[@name='arch']")
                view_model = _record_field_text(record, "model")

                if arch is not None and view_model and _is_form_view(record, arch):
                    exposed = surface.form_fields.setdefault(view_model, [])
                    _merge_unique(exposed, _arch_fields(arch))

                inherit = record.find("field[@name='inherit_id']")
                if inherit is not None:
                    target = inherit.get("ref", "?")
                    anchors = _view_anchors(arch) if arch is not None else []
                    detail = f" — anchors: {'; '.join(anchors)}" if anchors else ""
                    surface.inherited_views.append(f"{label} on `{view_model}` → inherits {target}{detail}")
                else:
                    surface.new_views.append(f"{label} on `{view_model}`" if view_model else label)
            elif model.startswith("ir.actions"):
                surface.actions.append(label)
            elif model in ("ir.rule", "res.groups"):
                surface.security.append(f"{model}: {label}")
            elif model:
                surface.data_records.append(f"{model}: {label}")


def _scan_security(module_path: Path, surface: ModuleSurface) -> None:
    """Collect access rules from ir.model.access.csv."""
    access_file = module_path / "security" / "ir.model.access.csv"
    if not access_file.is_file():
        return
    try:
        with access_file.open(encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
    except (OSError, UnicodeDecodeError, csv.Error) as error:
        surface.unreadable.append(f"security/ir.model.access.csv ({type(error).__name__})")
        return

    for row in rows:
        name = (row.get("id") or row.get("name") or "?").strip()
        surface.security.append(f"ir.model.access: {name}")


def extract_surface(module_path: Path) -> ModuleSurface:
    """Enumerate everything the module at ``module_path`` declares."""
    surface = ModuleSurface()
    try:
        _scan_python(module_path, surface)
        _scan_xml(module_path, surface)
        _scan_security(module_path, surface)
    except Exception as error:  # noqa: BLE001 - a partial surface beats no surface at all
        logger.debug(f"Could not fully scan {module_path}: {error}")
        surface.unreadable.append(f"{module_path.name} ({type(error).__name__})")
    return surface
