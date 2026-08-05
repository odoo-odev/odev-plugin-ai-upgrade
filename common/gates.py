"""Citation parsing, dead-token lookup and AST inspection used by the post-flight gates."""

import ast
import re

from odev.common.version import OdooVersion


# Tokens removed upstream: any survivor at or after ``gone_at`` is a defect.
# ``suffixes`` limits the search to file types where the token is meaningful.
DEAD_TOKENS: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "kanban-box": ("19.0", "renamed to t-name='card' in 19.0", (".xml",)),
    "attrs=": ("17.0", "split into invisible/readonly/required", (".xml",)),
    "states=": ("17.0", "use invisible= with a domain on state", (".xml",)),
}

# A citation is trusted in one of two shapes: a commit URL/shorthand, or a
# ``Source:`` trailer. The trailer form additionally requires a digit, because
# plain hex letters also spell ordinary words ("defaced", "facade").
SOURCE_URL_RE = re.compile(r"(?:github\.com/)?odoo/(?P<repo>odoo|enterprise)[/@]+(?:commit/)?(?P<sha>[0-9a-f]{7,40})\b")
SOURCE_TRAILER_RE = re.compile(
    r"Source:\s*(?P<repo>odoo|enterprise)?\s*(?P<sha>(?=[0-9a-f]{7,40}\b)[a-f]*[0-9][0-9a-f]*)\b",
    re.IGNORECASE,
)


def iter_cited_shas(commit_body: str) -> list[tuple[str, str]]:
    """Return ``(repo, sha)`` for every Odoo commit cited in a commit body.

    Deduplicated, order preserved. ``Source: not identified`` yields nothing.
    """
    found: list[tuple[str, str]] = []
    for pattern in (SOURCE_URL_RE, SOURCE_TRAILER_RE):
        for match in pattern.finditer(commit_body):
            entry = (match.group("repo") or "odoo", match.group("sha"))
            if entry not in found:
                found.append(entry)
    return found


def dead_tokens_for(target_ver: str) -> dict[str, tuple[str, str, tuple[str, ...]]]:
    """Return the tokens that must no longer appear when migrating to ``target_ver``.

    ``OdooVersion`` orders ``saas~18.1`` below ``19.0`` and ``master`` above every
    release, which a plain string comparison does not.
    """
    try:
        target = OdooVersion(target_ver)
    except (ValueError, TypeError):  # InvalidVersion subclasses ValueError
        return {}
    return {token: meta for token, meta in DEAD_TOKENS.items() if target >= OdooVersion(meta[0])}


def _function_bodies(source: str) -> dict[str, tuple[int, bool]]:
    """Map a qualified function name to (statement count, is-a-stub).

    Docstrings are ignored so documentation cannot mask an empty body. Nested
    functions are qualified by their enclosing function, so they never collide
    with a method of the same name. Returns ``{}`` when ``source`` does not parse.
    """
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return {}

    bodies: dict[str, tuple[int, bool]] = {}
    scope: list[str] = []

    def visit(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                if not isinstance(child, ast.ClassDef):
                    body = [
                        statement
                        for statement in child.body
                        if not (isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant))
                    ]
                    bodies[".".join([*scope, child.name])] = (len(body), _is_stub_body(body))
                scope.append(child.name)
                visit(child)
                scope.pop()
            else:
                visit(child)

    visit(tree)
    return bodies


def _calls_super(node: ast.AST | None) -> bool:
    """Whether ``node`` is a ``super().<attr>(...)`` call."""
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return False
    inner = node.func.value
    return isinstance(inner, ast.Call) and isinstance(inner.func, ast.Name) and inner.func.id == "super"


# `res = super().x(...)` followed by `return res` is the longest delegating body.
_DELEGATING_BODY_LENGTH = 2


def _is_single_statement_stub(statement: ast.stmt) -> bool:
    """Whether a lone statement is `pass`, a bare `return`, or a `super()` delegation."""
    if isinstance(statement, ast.Pass):
        return True
    if isinstance(statement, ast.Return):
        return statement.value is None or _calls_super(statement.value)
    if isinstance(statement, ast.Expr):  # bare `super().x(...)`, result discarded
        return _calls_super(statement.value)
    return False


def _assigns_super_then_returns_it(body: list[ast.stmt]) -> bool:
    """Whether the body is `res = super().x(...)` followed by `return res`."""
    assign, returned = body
    targets = getattr(assign, "targets", None)
    return bool(
        isinstance(assign, ast.Assign)
        and _calls_super(assign.value)
        and isinstance(returned, ast.Return)
        and isinstance(returned.value, ast.Name)
        and targets
        and isinstance(targets[0], ast.Name)
        and targets[0].id == returned.value.id
    )


def _is_stub_body(body: list[ast.stmt]) -> bool:
    """Whether a function body does nothing but delegate upwards, or nothing at all."""
    if not body:  # docstring-only, or `...`
        return True
    if len(body) == 1:
        return _is_single_statement_stub(body[0])
    if len(body) == _DELEGATING_BODY_LENGTH:
        return _assigns_super_then_returns_it(body)
    return False


def gutted_overrides(before: str, after: str) -> list[str]:
    """Return names of methods whose body was replaced by a bare ``super()`` call.

    Deleting an obsolete override is the correct fix and is not reported, and
    neither is a body that was already a stub. Only not-a-stub -> stub is a finding.
    """
    old, new = _function_bodies(before), _function_bodies(after)
    findings = []
    for name, (length, is_stub) in new.items():
        previous = old.get(name)
        if previous and is_stub and not previous[1]:
            findings.append(f"{name}: body reduced to a bare super() call ({previous[0]} -> {length})")
    return findings
