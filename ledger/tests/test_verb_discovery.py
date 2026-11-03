"""Verb discovery loads the modules the static registry lists."""
from __future__ import annotations

import importlib.util
import pkgutil
from pathlib import Path
from types import ModuleType

import ledger.verbs
from ledger.verbs import _registry, discover_verbs


def load_module(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_throwaway_verb_registers_through_discovery(tmp_path: Path) -> None:
    root = tmp_path / "extverbs"
    root.mkdir()
    (root / "ping.py").write_text(
        "from ledger.verbs import Result, Verb\n"
        "\n"
        "def register(table):\n"
        "    table['ping'] = Verb(\n"
        "        name='ping',\n"
        "        help='ping',\n"
        "        add_arguments=lambda p: None,\n"
        "        run=lambda args, ctx: Result(data={'pong': True}, text='pong'),\n"
        "    )\n",
        encoding="utf-8",
    )
    ping = load_module(root / "ping.py", "extverbs.ping")
    table = discover_verbs([ping])
    assert "ping" in table
    assert table["ping"].name == "ping"
    result = table["ping"].run(None, None)
    assert result.data == {"pong": True}
    assert result.text == "pong"


def test_underscore_prefixed_module_is_not_registered(tmp_path: Path) -> None:
    root = tmp_path / "extverbs"
    root.mkdir()
    (root / "_secret.py").write_text(
        "from ledger.verbs import Result, Verb\n"
        "\n"
        "VERB = Verb(\n"
        "    name='secret',\n"
        "    help='secret',\n"
        "    add_arguments=lambda p: None,\n"
        "    run=lambda args, ctx: Result(data={'secret': True}, text='no'),\n"
        ")\n",
        encoding="utf-8",
    )
    (root / "ping.py").write_text(
        "from ledger.verbs import Result, Verb\n"
        "\n"
        "VERB = Verb(\n"
        "    name='ping',\n"
        "    help='ping',\n"
        "    add_arguments=lambda p: None,\n"
        "    run=lambda args, ctx: Result(data={'pong': True}, text='pong'),\n"
        ")\n",
        encoding="utf-8",
    )
    secret = load_module(root / "_secret.py", "extverbs_hidden._secret")
    ping = load_module(root / "ping.py", "extverbs_hidden.ping")
    table = discover_verbs([secret, ping])
    assert "ping" in table
    assert "secret" not in table


def test_registry_lists_every_verb_module() -> None:
    verbs_dir = Path(ledger.verbs.__file__).parent
    on_disk = [
        info.name
        for info in pkgutil.iter_modules([str(verbs_dir)])
        if not info.name.startswith("_")
    ]
    registered = [module.__name__ for module in _registry.MODULES]
    assert registered == [f"ledger.verbs.{name}" for name in on_disk], (
        "ledger/verbs/_registry.py and the verbs directory disagree; regenerate "
        "the registry with: python3 -m ledger.gen_verb_registry"
    )


def test_default_discovery_reads_the_registry() -> None:
    expected = discover_verbs(_registry.MODULES)
    table = discover_verbs()
    assert list(table) == list(expected)
    assert {verb.run.__module__ for verb in table.values()} == {
        module.__name__ for module in _registry.MODULES
    }
