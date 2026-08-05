"""Pure helpers for the post-flight gates, free of odev imports so they stay unit-testable."""

import ast
import re


# Tokens removed upstream: any survivor at or after ``gone_at`` is a defect.
DEAD_TOKENS: dict[str, tuple[str, str]] = {
    "kanban-box": ("19.0", "renamed to t-name='card' in 19.0"),
    "attrs=": ("17.0", "split into invisible/readonly/required"),
    "states=": ("17.0", "use invisible= with a domain on state"),
}

SOURCE_SHA_RE = re.compile(
    r"(?:github\.com/odoo/(?P<repo>odoo|enterprise)/commit/|Source:\s*(?P<repo2>odoo|enterprise)?\s*)"
    r"(?P<sha>[0-9a-f]{7,40})\b"
)

MIN_SHA_LENGTH = 12


def iter_cited_shas(commit_body: str) -> list[tuple[str, str]]:
    """Yield ``(repo, sha)`` for every Odoo commit cited in a commit body."""
    return [
        (match.group("repo") or match.group("repo2") or "odoo", match.group("sha"))
        for match in SOURCE_SHA_RE.finditer(commit_body)
    ]


def dead_tokens_for(target_ver: str) -> dict[str, tuple[str, str]]:
    """The tokens that must no longer appear when migrating to ``target_ver``."""
    return {token: meta for token, meta in DEAD_TOKENS.items() if target_ver >= meta[0]}


def function_bodies(source: str) -> dict[str, tuple[int, bool]]:
    """Map ``Class.method`` -> (statement count, is a bare ``super()``/``pass`` stub).

    Docstrings are ignored so a method's documentation does not mask an empty body.
    Returns an empty mapping when ``source`` does not parse.
    """
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return {}

    bodies: dict[str, tuple[int, bool]] = {}
    scope: list[str] = []

    def visit(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                scope.append(child.name)
                visit(child)
                scope.pop()
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                body = [
                    statement
                    for statement in child.body
                    if not (isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant))
                ]
                stub = len(body) == 1 and _is_stub(body[0])
                bodies[".".join([*scope, child.name])] = (len(body), stub)
                visit(child)
            else:
                visit(child)

    visit(tree)
    return bodies


def _is_stub(statement: ast.stmt) -> bool:
    """Whether a lone statement is ``pass`` or ``return super().x(...)``."""
    if isinstance(statement, ast.Pass):
        return True
    value = statement.value if isinstance(statement, ast.Return) else None
    if isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute):
        inner = value.func.value
        return isinstance(inner, ast.Call) and isinstance(inner.func, ast.Name) and inner.func.id == "super"
    return False


def gutted_overrides(before: str, after: str) -> list[str]:
    """Names of methods whose body collapsed to a bare ``super()`` call.

    Deleting an obsolete override is correct and is not reported; replacing its
    body with a stub that keeps the signature is what this finds.
    """
    old, new = function_bodies(before), function_bodies(after)
    found = []
    for name, (length, is_stub) in new.items():
        previous = old.get(name)
        if previous and is_stub and not previous[1] and previous[0] > length:
            found.append(f"{name}: body reduced to a bare super() call ({previous[0]} -> {length})")
    return found
