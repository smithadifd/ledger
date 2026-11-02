"""The verb registry generator: current on the tree, stale on drift, fail closed.

Every case runs the generator the way CI does, ``python3 -m
ledger.gen_verb_registry`` from the folder that holds ``ledger/``. Every case
that changes the verbs directory runs it in a scratch copy of the ledger
package; only the first test reads the real tree, and it writes nothing.
"""
from __future__ import annotations

import ast
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

LEDGER = Path(__file__).resolve().parents[1]
GENERATOR = LEDGER / "gen_verb_registry.py"
VERBS = LEDGER / "verbs"
REGISTRY = VERBS / "_registry.py"
COMMAND = "python3 -m ledger.gen_verb_registry"
IMPORT_PREFIX = "from ledger.verbs import "
VERB_SOURCE = (
    "from ledger.verbs import Result, Verb\n"
    "\n"
    "VERB = Verb(\n"
    "    name='zz-added',\n"
    "    help='added',\n"
    "    add_arguments=lambda p: None,\n"
    "    run=lambda args, ctx: Result(data={}),\n"
    ")\n"
)


def run(generator: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run ``python3 -m ledger.gen_verb_registry`` beside ``generator``'s package."""
    env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    return subprocess.run(
        [sys.executable, "-m", "ledger.gen_verb_registry", *args],
        cwd=generator.parent.parent,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def scratch(tmp_path: Path, *, verbs: bool = True) -> Path:
    """Copy the ledger package (with the generator), and by default its verbs
    directory, into a scratch tree; return the scratch generator's path."""
    package = tmp_path / "lib" / "ledger"
    shutil.copytree(
        LEDGER,
        package,
        ignore=shutil.ignore_patterns("__pycache__", "tests", "verbs"),
    )
    if verbs:
        shutil.copytree(
            VERBS,
            package / "verbs",
            ignore=shutil.ignore_patterns("__pycache__"),
        )
    return package / GENERATOR.name


def registry_of(generator: Path) -> Path:
    return generator.parent / "verbs" / "_registry.py"


def imported(text: str) -> list[str]:
    return [
        line.removeprefix(IMPORT_PREFIX)
        for line in text.splitlines()
        if line.startswith(IMPORT_PREFIX)
    ]


def modules_tuple(text: str) -> list[str]:
    for node in ast.parse(text).body:
        if (
            isinstance(node, ast.Assign)
            and [target.id for target in node.targets if isinstance(target, ast.Name)]
            == ["MODULES"]
        ):
            assert isinstance(node.value, ast.Tuple)
            return [element.id for element in node.value.elts]
    raise AssertionError("the registry assigns no MODULES tuple")


# (1) The committed registry is what the generator writes.


def test_check_passes_on_the_committed_tree() -> None:
    before = REGISTRY.read_bytes()
    result = run(GENERATOR, "--check")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "is current" in result.stdout
    assert REGISTRY.read_bytes() == before


# (2) --check fails on drift, names the command, and writes nothing.


def test_check_fails_when_a_verb_is_added_without_regenerating(tmp_path: Path) -> None:
    generator = scratch(tmp_path)
    registry = registry_of(generator)
    before = registry.read_bytes()
    (registry.parent / "zz_added.py").write_text(VERB_SOURCE, encoding="utf-8")
    result = run(generator, "--check")
    assert result.returncode == 1, result.stdout + result.stderr
    assert COMMAND in result.stderr
    assert registry.read_bytes() == before


def test_check_fails_when_a_verb_is_removed_without_regenerating(tmp_path: Path) -> None:
    generator = scratch(tmp_path)
    registry = registry_of(generator)
    before = registry.read_bytes()
    (registry.parent / "guidance.py").unlink()
    result = run(generator, "--check")
    assert result.returncode == 1, result.stdout + result.stderr
    assert COMMAND in result.stderr
    assert registry.read_bytes() == before


def test_check_fails_when_the_registry_is_missing(tmp_path: Path) -> None:
    generator = scratch(tmp_path)
    registry = registry_of(generator)
    registry.unlink()
    result = run(generator, "--check")
    assert result.returncode == 1, result.stdout + result.stderr
    assert COMMAND in result.stderr
    assert not registry.exists()


def test_regenerating_after_adding_a_verb_makes_check_pass(tmp_path: Path) -> None:
    generator = scratch(tmp_path)
    registry = registry_of(generator)
    (registry.parent / "zz_added.py").write_text(VERB_SOURCE, encoding="utf-8")
    assert run(generator).returncode == 0
    text = registry.read_text(encoding="utf-8")
    assert imported(text)[-1] == "zz_added"
    assert modules_tuple(text)[-1] == "zz_added"
    result = run(generator, "--check")
    assert result.returncode == 0, result.stdout + result.stderr


# (3) Generation is byte-stable, and its output is the committed file.


def test_generation_is_byte_stable_and_matches_the_committed_registry(
    tmp_path: Path,
) -> None:
    generator = scratch(tmp_path)
    registry = registry_of(generator)
    registry.unlink()
    assert run(generator).returncode == 0
    first = registry.read_bytes()
    assert run(generator).returncode == 0
    assert registry.read_bytes() == first
    assert first == REGISTRY.read_bytes()


def test_generated_registry_lists_verb_modules_by_file_name(tmp_path: Path) -> None:
    generator = scratch(tmp_path)
    verbs = registry_of(generator).parent
    (verbs / "_helper.py").write_text("value = 1\n", encoding="utf-8")
    (verbs / "notes.txt").write_text("not a module\n", encoding="utf-8")
    (verbs / "aa_first.py").write_text(VERB_SOURCE, encoding="utf-8")
    assert run(generator).returncode == 0
    text = registry_of(generator).read_text(encoding="utf-8")
    expected = [
        path.stem
        for path in sorted(verbs.glob("*.py"), key=lambda path: path.name)
        if path.name != "__init__.py" and not path.name.startswith("_")
    ]
    assert expected[0] == "aa_first"
    assert "guidance" in expected
    assert imported(text) == expected
    assert modules_tuple(text) == expected
    assert COMMAND in text
    assert "Never edit this file by hand" in text
    compile(text, "_registry.py", "exec")


# (4) Fail closed: exit 2 and write nothing, never an empty registry.


@pytest.mark.parametrize("args", [(), ("--check",)], ids=["write", "check"])
def test_missing_verbs_directory_refuses(tmp_path: Path, args: tuple[str, ...]) -> None:
    generator = scratch(tmp_path, verbs=False)
    result = run(generator, *args)
    assert result.returncode == 2, result.stdout + result.stderr
    assert "cannot list the verbs directory" in result.stderr
    assert not (generator.parent / "verbs").exists()


@pytest.mark.parametrize("args", [(), ("--check",)], ids=["write", "check"])
@pytest.mark.parametrize("kind", ["regular-file", "symlink-loop"])
def test_unlistable_verbs_directory_refuses(
    tmp_path: Path, kind: str, args: tuple[str, ...]
) -> None:
    generator = scratch(tmp_path, verbs=False)
    verbs = generator.parent / "verbs"
    if kind == "regular-file":
        verbs.write_text("not a directory\n", encoding="utf-8")
    else:
        verbs.symlink_to("verbs")
    result = run(generator, *args)
    assert result.returncode == 2, result.stdout + result.stderr
    assert "cannot list the verbs directory" in result.stderr


@pytest.mark.parametrize("args", [(), ("--check",)], ids=["write", "check"])
@pytest.mark.parametrize(
    "name", ["bad-name.py", "1verb.py", "class.py", "with space.py"]
)
def test_invalid_module_name_refuses_and_writes_nothing(
    tmp_path: Path, name: str, args: tuple[str, ...]
) -> None:
    generator = scratch(tmp_path)
    registry = registry_of(generator)
    before = registry.read_bytes()
    (registry.parent / name).write_text(VERB_SOURCE, encoding="utf-8")
    result = run(generator, *args)
    assert result.returncode == 2, result.stdout + result.stderr
    assert "not a valid Python identifier" in result.stderr
    assert registry.read_bytes() == before


@pytest.mark.parametrize("args", [(), ("--check",)], ids=["write", "check"])
def test_verbs_directory_without_verb_modules_refuses(
    tmp_path: Path, args: tuple[str, ...]
) -> None:
    generator = scratch(tmp_path)
    registry = registry_of(generator)
    for path in registry.parent.glob("*.py"):
        if not path.name.startswith("_"):
            path.unlink()
    before = registry.read_bytes()
    result = run(generator, *args)
    assert result.returncode == 2, result.stdout + result.stderr
    assert "holds no verb module" in result.stderr
    assert registry.read_bytes() == before


@pytest.mark.parametrize("args", [(), ("--check",)], ids=["write", "check"])
def test_py_entry_that_is_not_a_file_refuses(
    tmp_path: Path, args: tuple[str, ...]
) -> None:
    generator = scratch(tmp_path)
    registry = registry_of(generator)
    before = registry.read_bytes()
    (registry.parent / "pkg.py").mkdir()
    result = run(generator, *args)
    assert result.returncode == 2, result.stdout + result.stderr
    assert "not a regular file" in result.stderr
    assert registry.read_bytes() == before


# (5) A symlink is refused by name, before any other test, in both modes.


def test_symlinked_py_verb_refuses_in_generate_mode(tmp_path: Path) -> None:
    generator = scratch(tmp_path)
    registry = registry_of(generator)
    before = registry.read_bytes()
    verbs = registry.parent
    link = verbs / "linked.py"
    link.symlink_to("guidance.py")
    result = run(generator)
    assert result.returncode == 2, result.stdout + result.stderr
    assert "verb entry is a symlink" in result.stderr
    assert "linked.py" in result.stderr
    assert registry.read_bytes() == before


def test_symlinked_py_verb_refuses_in_check_mode_not_as_drift(
    tmp_path: Path,
) -> None:
    generator = scratch(tmp_path)
    registry = registry_of(generator)
    before = registry.read_bytes()
    verbs = registry.parent
    link = verbs / "linked.py"
    link.symlink_to("guidance.py")
    result = run(generator, "--check")
    assert result.returncode == 2, result.stdout + result.stderr
    assert "verb entry is a symlink" in result.stderr
    assert "linked.py" in result.stderr
    assert COMMAND not in result.stderr
    assert "does not match the verbs directory" not in result.stderr
    assert registry.read_bytes() == before


@pytest.mark.parametrize("args", [(), ("--check",)], ids=["write", "check"])
@pytest.mark.parametrize("kind", ["directory", "non-py-file"])
def test_symlink_to_directory_or_non_py_file_refuses(
    tmp_path: Path, kind: str, args: tuple[str, ...]
) -> None:
    generator = scratch(tmp_path)
    registry = registry_of(generator)
    before = registry.read_bytes()
    verbs = registry.parent
    if kind == "directory":
        target = tmp_path / "target_dir"
        target.mkdir()
        name = "linked_dir"
    else:
        target = tmp_path / "notes.txt"
        target.write_text("not a module\n", encoding="utf-8")
        name = "linked.txt"
    link = verbs / name
    link.symlink_to(target)
    result = run(generator, *args)
    assert result.returncode == 2, result.stdout + result.stderr
    assert "verb entry is a symlink" in result.stderr
    assert name in result.stderr
    assert registry.read_bytes() == before
