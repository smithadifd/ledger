"""Plugin verbs: a caller passes verb modules in, and they can only add verbs.

The leaf never loads a module by name; the caller imports its plugins and
passes them to ``discover_verbs(plugins=...)`` or ``cli.main(plugin_modules=...)``.
Each fixture plugin here is a module object built in the test.
"""
from __future__ import annotations

import json
from types import ModuleType

import pytest

from ledger import cli
from ledger.verbs import (
    EXIT_REFUSED,
    PluginRefused,
    Result,
    Verb,
    _registry,
    discover_verbs,
)


def make_verb(name: str, text: str = "hello from a plugin") -> Verb:
    return Verb(
        name=name,
        help=f"plugin verb {name}",
        add_arguments=lambda parser: None,
        run=lambda args, ctx: Result(data={"verb": name}, text=text),
    )


def verb_plugin(module_name: str, verb_name: str) -> ModuleType:
    module = ModuleType(module_name)
    module.VERB = make_verb(verb_name)
    return module


def register_plugin(module_name: str, *verb_names: str) -> ModuleType:
    module = ModuleType(module_name)

    def register(table):
        for verb_name in verb_names:
            table[verb_name] = make_verb(verb_name)

    module.register = register
    return module


# (1) A plugin verb is added after the leaf's, and the CLI shows and runs it.


def test_plugin_verb_is_appended_after_the_leaf_verbs() -> None:
    leaf = discover_verbs()
    table = discover_verbs(plugins=[verb_plugin("fixture_plugins.hello", "zz-hello")])
    assert list(table) == [*leaf, "zz-hello"]
    assert all(table[name] is leaf[name] for name in leaf)


def test_plugin_verb_appears_in_ledger_help(capsys) -> None:
    plugin = verb_plugin("fixture_plugins.hello", "zz-hello")
    assert cli.main(["--help"], plugin_modules=[plugin]) == 0
    out = capsys.readouterr().out
    assert "zz-hello" in out
    assert "plugin verb zz-hello" in out


def test_plugin_verb_runs_through_the_cli(tmp_path, capsys) -> None:
    plugin = verb_plugin("fixture_plugins.hello", "zz-hello")
    code = cli.main(
        ["--db", str(tmp_path / "ledger.db"), "zz-hello"], plugin_modules=[plugin]
    )
    assert code == 0
    assert capsys.readouterr().out == "hello from a plugin\n"


def test_register_hook_plugin_adds_its_verbs() -> None:
    table = discover_verbs(plugins=[register_plugin("fixture_plugins.pair", "zz-a", "zz-b")])
    assert list(table)[-2:] == ["zz-a", "zz-b"]


# (2) A plugin can never replace or shadow a verb that is already loaded.


def test_plugin_verb_colliding_with_a_leaf_verb_is_refused(capsys) -> None:
    plugin = verb_plugin("fixture_plugins.shadow", "note")
    with pytest.raises(PluginRefused) as caught:
        discover_verbs(plugins=[plugin])
    assert caught.value.code == "verb_collision"
    assert caught.value.exit_code == EXIT_REFUSED
    assert "'note'" in str(caught.value)
    assert "fixture_plugins.shadow" in str(caught.value)
    assert "the ledger already provides it" in str(caught.value)

    assert cli.main(["--help"], plugin_modules=[plugin]) == EXIT_REFUSED
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "fixture_plugins.shadow cannot add verb 'note'" in captured.err


def test_plugin_collision_is_refused_in_the_json_envelope(capsys) -> None:
    plugin = verb_plugin("fixture_plugins.shadow", "note")
    assert cli.main(["--json", "--help"], plugin_modules=[plugin]) == EXIT_REFUSED
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "verb_collision"
    assert "'note'" in envelope["error"]["message"]


def test_register_hook_cannot_overwrite_a_leaf_verb() -> None:
    leaf = discover_verbs()
    plugin = register_plugin("fixture_plugins.sneaky", "note")
    with pytest.raises(PluginRefused, match="cannot add verb 'note'"):
        discover_verbs(plugins=[plugin])
    assert discover_verbs()["note"] is leaf["note"]


def test_two_plugins_naming_the_same_verb_are_refused() -> None:
    first = verb_plugin("fixture_plugins.first", "zz-same")
    second = verb_plugin("fixture_plugins.second", "zz-same")
    with pytest.raises(PluginRefused) as caught:
        discover_verbs(plugins=[first, second])
    message = str(caught.value)
    assert "fixture_plugins.second cannot add verb 'zz-same'" in message
    assert "plugin fixture_plugins.first already provides it" in message


def test_plugin_registering_something_other_than_a_verb_is_refused() -> None:
    module = ModuleType("fixture_plugins.bogus")
    module.register = lambda table: table.__setitem__("zz-bogus", object())
    with pytest.raises(PluginRefused) as caught:
        discover_verbs(plugins=[module])
    assert caught.value.code == "invalid_plugin"
    assert "fixture_plugins.bogus" in str(caught.value)


def test_plugin_exporting_a_non_verb_VERB_is_refused(capsys) -> None:
    module = ModuleType("fixture_plugins.badexport")
    module.VERB = "not a verb"
    with pytest.raises(PluginRefused) as caught:
        discover_verbs(plugins=[module])
    assert caught.value.code == "invalid_plugin"
    assert "fixture_plugins.badexport exports VERB as str" in str(caught.value)

    assert cli.main(["--help"], plugin_modules=[module]) == EXIT_REFUSED
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "fixture_plugins.badexport exports VERB as str" in captured.err


def test_plugin_colliding_with_a_caller_extra_verb_is_refused(capsys) -> None:
    plugin = verb_plugin("fixture_plugins.wrapper_clash", "zz-import")
    extra = {"zz-import": make_verb("zz-import")}
    assert (
        cli.main(["--help"], extra_verbs=extra, plugin_modules=[plugin])
        == EXIT_REFUSED
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "fixture_plugins.wrapper_clash cannot add verb 'zz-import'" in captured.err
    assert "the caller already provides it" in captured.err
    assert "Traceback" not in captured.err


def test_plugin_caller_collision_is_refused_in_the_json_envelope(capsys) -> None:
    plugin = verb_plugin("fixture_plugins.wrapper_clash", "zz-import")
    extra = {"zz-import": make_verb("zz-import")}
    code = cli.main(
        ["--json", "--help"], extra_verbs=extra, plugin_modules=[plugin]
    )
    assert code == EXIT_REFUSED
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "verb_collision"
    assert "'zz-import'" in envelope["error"]["message"]


def test_plugin_and_extra_verbs_with_distinct_names_both_load(capsys) -> None:
    plugin = verb_plugin("fixture_plugins.hello", "zz-hello")
    extra = {"zz-import": make_verb("zz-import")}
    assert cli.main(["--help"], extra_verbs=extra, plugin_modules=[plugin]) == 0
    out = capsys.readouterr().out
    assert "zz-hello" in out and "zz-import" in out


# (2b) Modules named with a leading underscore are skipped silently (decision
# D5): they are private helpers beside the verb modules, never verbs.


def test_plugin_module_named_with_a_leading_underscore_is_skipped() -> None:
    leaf = discover_verbs()
    hidden = verb_plugin("fixture_plugins._private", "zz-hidden")
    hidden_register = register_plugin("fixture_plugins._private_reg", "zz-hidden2")
    table = discover_verbs(plugins=[hidden, hidden_register])
    assert list(table) == list(leaf)
    # a skipped module is not validated either: no refusal for a bad export
    bad = ModuleType("fixture_plugins._bad")
    bad.VERB = "not a verb"
    assert list(discover_verbs(plugins=[bad])) == list(leaf)


# (3) With no plugins, the verb set is the static registry's, unchanged.


def test_no_plugins_gives_exactly_the_registry_verbs() -> None:
    expected = discover_verbs(_registry.MODULES)
    for table in (discover_verbs(), discover_verbs(plugins=())):
        assert list(table) == list(expected)
        assert {verb.run.__module__ for verb in table.values()} == {
            module.__name__ for module in _registry.MODULES
        }


def test_no_plugins_leaves_the_help_text_unchanged(capsys) -> None:
    assert cli.main(["--help"]) == 0
    default = capsys.readouterr().out
    assert cli.main(["--help"], plugin_modules=()) == 0
    assert capsys.readouterr().out == default
