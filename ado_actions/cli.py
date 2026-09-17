"""`ado` command line entry point."""
from __future__ import annotations

import click

from ado_actions.__about__ import __version__
from ado_actions.board import board_cli
from ado_actions.fetch_test_plan import testing_cli
from ado_actions.pipeline import pipeline_cli
from ado_actions.repo import repo_cli


@click.group()
@click.version_option(__version__, prog_name="ado")
def cli() -> None:
    """Azure DevOps helpers: boards, pipelines, repos and test plans."""


cli.add_command(board_cli)
cli.add_command(pipeline_cli)
cli.add_command(repo_cli)
cli.add_command(testing_cli)


if __name__ == "__main__":
    cli()
