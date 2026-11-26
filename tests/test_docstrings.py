"""Every function of the package documents its arguments and what it returns.

Google style (Sphinx napoleon): each parameter but ``self``/``cls`` has an
entry in ``Args:``, and a function that returns (or yields) a value has a
``Returns:`` (``Yields:``) section. Properties, dunder methods and functions
annotated ``-> None`` need no ``Returns:``.

Every ``UPPER_CASE`` constant, at module or class level, has a ``#:`` comment
right above it (or a docstring right after it): that is what Sphinx autodoc
shows in the reference. Enum members are documented by their class.
"""

import ast
import re
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src" / "dynacamp"
ARGS = re.compile(r"^(?:Args|Arguments|Parameters):[ \t]*\n((?:[ \t]+.*\n?|[ \t]*\n)*)", re.M)
ENTRY = re.compile(r"^[ \t]+\**(\w+)\s*(?:\([^)]*\))?:", re.M)
Function = ast.FunctionDef | ast.AsyncFunctionDef
CONSTANT = re.compile(r"^[A-Z][A-Z0-9_]*$")
ENUM_BASES = {"Enum", "StrEnum", "IntEnum", "Flag", "IntFlag"}


def _functions():
    """Every function and method of the package, nested ones included.

    Yields:
        ``(path:line name, function node)``.
    """
    for path in sorted(SRC.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                yield f"{path.relative_to(SRC.parent)}:{node.lineno} {node.name}", node


def _params(fn: Function) -> list[str]:
    """Names of a function's parameters, without ``self`` and ``cls``.

    Args:
        fn: The function node.

    Returns:
        The parameter names in signature order.
    """
    a = fn.args
    names = [x.arg for x in a.posonlyargs + a.args + a.kwonlyargs]
    names += [x.arg for x in (a.vararg, a.kwarg) if x is not None]
    return [n for n in names if n not in ("self", "cls")]


def _own_nodes(fn: Function):
    """The nodes of a function's body, not descending into nested scopes.

    Args:
        fn: The function node.

    Yields:
        Each node of the body outside nested functions, lambdas and classes.
    """
    stack = list(ast.iter_child_nodes(fn))
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            continue
        yield node
        stack.extend(ast.iter_child_nodes(node))


def _returns_value(fn: Function) -> bool:
    """Whether a function returns something other than None.

    Args:
        fn: The function node.

    Returns:
        True if a ``return`` in its own body has a value other than ``None``.
    """
    return any(
        isinstance(n, ast.Return) and n.value is not None
        and not (isinstance(n.value, ast.Constant) and n.value.value is None)
        for n in _own_nodes(fn)
    )


def test_every_argument_is_documented():
    missing = []
    for where, fn in _functions():
        doc = (ast.get_docstring(fn) or "") + "\n"
        section = ARGS.search(doc)
        documented = set(ENTRY.findall(section.group(1))) if section else set()
        undocumented = [p for p in _params(fn) if p not in documented]
        if undocumented:
            missing.append(f"{where}: {', '.join(undocumented)}")
    assert not missing, "arguments missing from Args:\n" + "\n".join(missing)


def test_every_return_value_is_documented():
    missing = []
    for where, fn in _functions():
        decorators = {getattr(d, "id", getattr(d, "attr", None)) for d in fn.decorator_list}
        if fn.name.startswith("__") or decorators & {"property", "setter", "cached_property"}:
            continue
        doc = ast.get_docstring(fn) or ""
        if any(isinstance(n, (ast.Yield, ast.YieldFrom)) for n in _own_nodes(fn)):
            if "Yields:" not in doc and "Returns:" not in doc:
                missing.append(f"{where}: Yields")
        elif _returns_value(fn) and "Returns:" not in doc:
            missing.append(f"{where}: Returns")
    assert not missing, "undocumented results:\n" + "\n".join(missing)


def _undocumented_constants(path: Path) -> list[str]:
    """Constants of a module without a ``#:`` comment or a docstring.

    Args:
        path: The module file.

    Returns:
        ``path:line NAME`` of each undocumented constant.
    """
    source = path.read_text(encoding="utf-8")
    lines = source.splitlines()
    out = []

    def scan(body: list[ast.stmt], owner: str) -> None:
        """Check the assignments of a module or class body.

        Args:
            body: The statements.
            owner: ``"Class."`` for class attributes, ``""`` at module level.
        """
        for i, node in enumerate(body):
            if isinstance(node, ast.ClassDef):
                if not {getattr(b, "id", getattr(b, "attr", "")) for b in node.bases} & ENUM_BASES:
                    scan(node.body, node.name + ".")
                continue
            if isinstance(node, ast.Assign):
                names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                names = [node.target.id]
            else:
                continue
            after = body[i + 1] if i + 1 < len(body) else None
            documented = (node.lineno > 1 and lines[node.lineno - 2].lstrip().startswith("#:")) or (
                isinstance(after, ast.Expr) and isinstance(after.value, ast.Constant) and isinstance(after.value.value, str)
            )
            out.extend(
                f"{path.relative_to(SRC.parent)}:{node.lineno} {owner}{n}"
                for n in names if CONSTANT.match(n) and not documented
            )

    scan(ast.parse(source).body, "")
    return out


def test_every_constant_is_documented():
    missing = [c for path in sorted(SRC.rglob("*.py")) for c in _undocumented_constants(path)]
    assert not missing, "constants without a #: comment:\n" + "\n".join(missing)
