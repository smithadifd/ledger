"""Print the agent contract: the one page an agent working with a ledger reads.

``ledger contract`` prints the full page from the installed package, so an
agent that has only the package can read it without fetching anything.
``ledger resume --brief`` prints the short form and points here. The page
ships as ``agent_contract.md`` beside this package's modules; a repository
test keeps it byte-equal to the page the repository documents.

The verb never opens the database. With ``--json`` the page is the
``contract`` field.
"""
from __future__ import annotations

from importlib import resources

from ledger.verbs import Context, Result, Verb

CONTRACT_FILE = "agent_contract.md"


def contract_text() -> str:
    """The agent contract page, read from the installed package."""
    return resources.files("ledger").joinpath(CONTRACT_FILE).read_text(encoding="utf-8")


def add_arguments(parser) -> None:
    return None


def run(args, ctx: Context) -> Result:
    text = contract_text()
    return Result(data={"contract": text}, text=text)


VERB = Verb(
    name="contract",
    help="print the full agent contract from the installed package",
    add_arguments=add_arguments,
    run=run,
)
