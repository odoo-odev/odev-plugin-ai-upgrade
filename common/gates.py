"""Helpers for the post-flight checks: reading citations, and spotting emptied overrides.

Both answer questions the agent cannot answer about itself — does this commit it
cited actually exist, and did it quietly delete a method body — so neither
re-does any part of the migration.
"""

import ast
import re


# A citation appears either as a commit URL or as a `Source:` trailer. The repository
# qualifier shows up on either side in practice (`Source: enterprise <sha>` and
# `Source: <sha> (enterprise)`), and is usually absent; any other parenthetical is a
# human note, not a repository. Abbreviations are accepted — whether the commit
# resolves is what matters, and that is decided by looking it up, not by its shape.
SOURCE_URL_RE = re.compile(r"(?:github\.com/)?odoo/(?P<repo>odoo|enterprise)[/@]+(?:commit/)?(?P<sha>[0-9a-f]{7,40})\b")
SOURCE_TRAILER_RE = re.compile(
    r"Source:\s*(?P<repo>odoo|enterprise)?\s*(?P<sha>[0-9a-f]{7,40})\b"
    r"(?:\s*\((?P<repo_after>odoo|enterprise)\))?",
    re.IGNORECASE,
)


def iter_cited_shas(commit_body: str) -> list[tuple[str | None, str]]:
    """Return ``(repo, sha)`` for every Odoo commit cited in a commit body.

    ``repo`` is ``None`` when the citation does not name one, which is the common
    case; the caller should then accept the commit from any provisioned checkout.
    Deduplicated, order preserved. ``Source: not identified`` yields nothing.
    """
    found: list[tuple[str | None, str]] = []
    for pattern in (SOURCE_URL_RE, SOURCE_TRAILER_RE):
        for match in pattern.finditer(commit_body):
            groups = match.groupdict()
            repo = groups.get("repo") or groups.get("repo_after")
            entry = (repo.lower() if repo else None, match.group("sha"))
            if entry not in found:
                found.append(entry)
    return found


def gutted_overrides(before: str, after: str) -> list[str]:
    """Return methods whose body was replaced by a bare ``super()`` call.

    Deleting an obsolete override is the correct fix and is not reported; neither
    is a body that was already a stub. Only not-a-stub -> stub is a finding, since
    that keeps the signature and the docstring while dropping the behaviour, which
    installs clean and passes tests.
    """
    old, new = _function_bodies(before), _function_bodies(after)
    if new is None:
        return ["file no longer parses as Python"]
    if old is None:
        return []

    findings = []
    for name, definitions in new.items():
        previous = old.get(name)
        # A name defined twice (a property and its setter, an @overload stub) is
        # skipped rather than guessed at.
        if not previous or len(definitions) != 1 or len(previous) != 1:
            continue
        (length, is_stub), (was_length, was_stub) = definitions[0], previous[0]
        if is_stub and not was_stub:
            findings.append(f"{name}: body reduced to a bare super() call ({was_length} -> {length})")
    return findings


def _function_bodies(source: str) -> dict[str, list[tuple[int, bool]]] | None:
    """Map a qualified function name to every (statement count, is-a-stub) it has.

    Returns ``None`` when the source does not parse, which is itself worth
    reporting. Docstrings are ignored so documentation cannot mask an empty body.
    """
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return None

    bodies: dict[str, list[tuple[int, bool]]] = {}
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
                    name = ".".join([*scope, child.name])
                    bodies.setdefault(name, []).append((len(body), _is_stub(body)))
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
_DELEGATING_LENGTH = 2


def _is_stub(body: list[ast.stmt]) -> bool:
    """Whether a body does nothing but delegate upwards, or nothing at all."""
    if not body:  # docstring-only, or `...`
        return True
    if len(body) == 1:
        return _is_lone_stub(body[0])
    if len(body) == _DELEGATING_LENGTH:
        return _assigns_super_then_returns_it(body)
    return False


def _is_lone_stub(statement: ast.stmt) -> bool:
    """Whether a single statement is `pass`, a bare `return`, or a `super()` delegation."""
    if isinstance(statement, ast.Pass):
        return True
    if isinstance(statement, ast.Return):
        return statement.value is None or _calls_super(statement.value)
    if isinstance(statement, ast.Expr):  # bare `super().x(...)`, result discarded
        return _calls_super(statement.value)
    return False


def _assigns_super_then_returns_it(body: list[ast.stmt]) -> bool:
    """Whether the body is `res = super().x(...)` then `return res`."""
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
