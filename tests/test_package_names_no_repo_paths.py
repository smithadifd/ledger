"""The ``ledger/`` folder stands alone: no tracked file under it names a path outside it.

A consumer vendors ``ledger/`` by itself, so a reference to the repository's
own files would dangle there. The grammar, deliberately small:

* a path token: one of the repository's top-level entries (``docs/``,
  ``tests/``, ``.github/``, ``README.md``, ``CHANGELOG.md``, ``pyproject.toml``,
  ``VENDORING.md``, ``SECURITY.md``, ``LICENSE``) not preceded by a path
  character or a dot, so ``ledger/docs/x`` and ``a.tests/`` do not match;
* a path expression: ``parent``/``parents[...]`` (or ``../``) on one line with
  a quoted ``"docs"``, ``"tests"`` or ``".github"`` (the file names above are
  already caught as tokens).

For Python files a second, structural pass (``ast``) catches the traversal and
the repository name split across statements. Per module, flat over all scopes:

* a name is *traversal-tainted* when it is assigned (``=``, annotated, ``:=``,
  tuple targets, or ``self.x``-style dotted attributes) an expression that
  contains ``.parent``, ``.parents``, ``dirname(...)`` or a ``".."`` string, or
  that uses an already-tainted name; this repeats to a fixed point;
* a name is *repo-named* when it is assigned a string constant, an f-string or
  ``+`` concatenation whose leading constant part, or another repo-named name,
  has a first path component that is one of the repository's top-level
  entries; this joins the same fixed point;
* a ``/`` expression, ``joinpath(...)``, ``os.path.join(...)``/``join(...)`` or
  ``Path(...)`` call is flagged when its subtree holds a traversal or a
  tainted name AND a string constant or repo-named name for one of the
  repository's top-level entries; the joining expression's file:line is reported.

Not read: names built at run time (``getattr``, ``exec``, string formatting or
concatenation), values imported from another module, and taint that flows
through function arguments or return values. A Python file that does not parse
is itself a violation.

Bare prose ("the docs folder", "the README") passes. There is no opt-out marker.
"""
from __future__ import annotations

import ast
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

_NAMES = r"(?:docs/|tests/|\.github/|README\.md|CHANGELOG\.md|pyproject\.toml|VENDORING\.md|SECURITY\.md|LICENSE\b)"
_TOKEN = re.compile(r"(?<![\w./-])" + _NAMES)
_QUOTED = re.compile(r"""["'](?:docs|tests|\.github)/?["']""")
_PARENT = re.compile(r"\.parents?\b|\.\./")


_REPO_FIRST = {"docs", "tests", ".github", "README.md", "CHANGELOG.md",
               "pyproject.toml", "VENDORING.md", "SECURITY.md", "LICENSE"}


def _key(node: ast.AST) -> str | None:
    """``name`` or ``a.b`` for a Name / dotted Attribute chain, else None."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _key(node.value)
        return f"{base}.{node.attr}" if base else None
    return None


def _traverses(node: ast.AST, tainted: set[str]) -> bool:
    for n in ast.walk(node):
        if isinstance(n, ast.Attribute) and n.attr in ("parent", "parents"):
            return True
        if isinstance(n, ast.Call) and (_key(n.func) or "").split(".")[-1] == "dirname":
            return True
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and (
                n.value == ".." or n.value.startswith("../")):
            return True
        if isinstance(n, (ast.Name, ast.Attribute)) and _key(n) in tainted:
            return True
    return False


def _is_repo_const(n: ast.AST) -> bool:
    return (isinstance(n, ast.Constant) and isinstance(n.value, str)
            and n.value.split("/", 1)[0] in _REPO_FIRST)


def _names_repo_entry(node: ast.AST, repo_names: set[str]) -> bool:
    return any(
        _is_repo_const(n)
        or (isinstance(n, (ast.Name, ast.Attribute)) and _key(n) in repo_names)
        for n in ast.walk(node)
    )


def _repo_value(node: ast.AST, repo_names: set[str]) -> bool:
    """A string value that starts with a repository entry (leading part only)."""
    if _is_repo_const(node):
        return True
    if isinstance(node, ast.JoinedStr):
        return bool(node.values) and _repo_value(node.values[0], repo_names)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _repo_value(node.left, repo_names)
    return isinstance(node, (ast.Name, ast.Attribute)) and _key(node) in repo_names


def _targets(node: ast.AST) -> list[str]:
    if isinstance(node, (ast.Tuple, ast.List)):
        return [k for e in node.elts for k in _targets(e)]
    key = _key(node)
    return [key] if key else []


def _assignments(tree: ast.AST) -> list[tuple[list[str], ast.AST]]:
    out: list[tuple[list[str], ast.AST]] = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Assign):
            out.append(([k for t in n.targets for k in _targets(t)], n.value))
        elif isinstance(n, (ast.AnnAssign, ast.NamedExpr)) and n.value is not None:
            out.append((_targets(n.target), n.value))
    return out


def _joins(node: ast.AST) -> bool:
    if isinstance(node, ast.BinOp):
        return isinstance(node.op, ast.Div)
    if isinstance(node, ast.Call):
        last = (_key(node.func) or "").split(".")[-1]
        return last in ("joinpath", "join", "Path")
    return False


def ast_violations(rel: str, text: str) -> list[str]:
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        return [f"{rel}:{exc.lineno or 0}: does not parse ({exc.msg})"]
    assigned = _assignments(tree)
    tainted: set[str] = set()
    repo_names: set[str] = set()
    changed = True
    while changed:
        changed = False
        for names, value in assigned:
            if _traverses(value, tainted) and not set(names) <= tainted:
                tainted |= set(names)
                changed = True
            if _repo_value(value, repo_names) and not set(names) <= repo_names:
                repo_names |= set(names)
                changed = True
    found: list[str] = []
    covered: set[int] = set()
    for n in ast.walk(tree):
        if id(n) in covered or not _joins(n):
            continue
        if _traverses(n, tainted) and _names_repo_entry(n, repo_names):
            found.append(f"{rel}:{n.lineno}: {ast.unparse(n)[:100]}")
            covered |= {id(c) for c in ast.walk(n)}
    return found


def violations(rel: str, text: str) -> list[str]:
    found = []
    for number, line in enumerate(text.splitlines(), 1):
        if _TOKEN.search(line) or (_PARENT.search(line) and _QUOTED.search(line)):
            found.append(f"{rel}:{number}: {line.strip()[:100]}")
    if rel.endswith(".py"):
        found += ast_violations(rel, text)
    return found


def _tracked() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z", "--", "ledger"], cwd=ROOT,
        capture_output=True, check=True,
    ).stdout.decode()
    return [p for p in out.split("\0") if p]


def _scan_all() -> list[str]:
    found: list[str] = []
    for rel in _tracked():
        path = ROOT / rel
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, FileNotFoundError):
            continue
        found += violations(rel, text)
    return found


def test_package_names_no_repository_path() -> None:
    assert _scan_all() == []


def test_the_grammar_flags_what_it_claims_to() -> None:
    assert violations("x", "see docs/agent-contract.md")
    assert violations("x", "(REPO / 'pyproject.toml').is_file()")
    assert violations("x", "if (Path(__file__).parent.parent / 'pyproject.toml').is_file():")
    assert violations("x", "root = Path(__file__).parents[2] / 'docs'")
    assert violations("x", "the docs folder and the README") == []
    assert violations("x", "ledger/docs/x and a.tests/y") == []


def test_split_statement_traversal_is_caught() -> None:
    probe = (
        'PKG_PARENT = Path(__file__).resolve().parent.parent.parent\n'
        'CONTRACT_PAGE = PKG_PARENT / "docs" / "agent-contract.md"\n'
    )
    assert ast_violations("x.py", probe) == ["x.py:2: PKG_PARENT / 'docs' / 'agent-contract.md'"]
    # the text pass alone cannot see a bare "docs" component
    assert violations("x.py", probe.replace("agent-contract.md", "c")) != []


def test_transitive_and_call_forms_are_caught() -> None:
    transitive = (
        "PKG_PARENT = Path(__file__).resolve().parent.parent\n"
        "ROOT = PKG_PARENT\n"
        "def f():\n"
        '    return ROOT / "README.md"\n'
    )
    assert len(ast_violations("x.py", transitive)) == 1
    assert ast_violations("x.py", "B = os.path.dirname(__file__)\nP = os.path.join(B, 'tests')\n")
    assert ast_violations("x.py", "B = Path(__file__).parent\nP = B.joinpath('docs', 'a')\n")
    assert ast_violations("x.py", "self.root = Path(__file__).parents[1]\nx = self.root / 'LICENSE'\n")


def test_benign_paths_and_unparseable_files() -> None:
    benign = (
        "PKG_PARENT = Path(__file__).resolve().parent.parent.parent\n"
        'p = PKG_PARENT / "ledger"\n'
        'q = Path("docs-like") / "x"\n'
        'r = tmp_path / "docs" / "agent-contract.md"\n'
    )
    assert ast_violations("x.py", benign) == []
    broken = ast_violations("x.py", "def (:\n")
    assert len(broken) == 1 and "does not parse" in broken[0]


def test_repo_entry_held_in_a_name_is_caught() -> None:
    head = "PKG_PARENT = Path(__file__).resolve().parent.parent\n"
    direct = head + 'REPO_DIR = "docs"\nx = PKG_PARENT / REPO_DIR / "agent-contract.md"\n'
    assert len(ast_violations("x.py", direct)) == 1
    chained = head + 'D = "docs"\nE = D\nx = PKG_PARENT / E\n'
    assert len(ast_violations("x.py", chained)) == 1
    fstring = head + 'D = f"docs/{n}"\nx = PKG_PARENT / D\n'
    assert len(ast_violations("x.py", fstring)) == 1
    assert ast_violations("x.py", head + 'D = "ledger"\nx = PKG_PARENT / D\n') == []


def test_there_is_no_opt_out_marker() -> None:
    assert violations("x.md", "see docs/x.md  lint: allow-repo-path")
    assert violations("x.py", "# lint: allow-repo-path\nB = Path(__file__).parent\nP = B / 'docs'\n")
