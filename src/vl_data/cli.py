"""Root command-line app for vl_data (run as ``python -m vl_data``)."""

import typer

import vl_data
from vl_data.etl.cli import register as register_etl
from vl_data.generator.cli import register as register_generator

app = typer.Typer(
    name="vl_data",
    help="Vehicle-location data product command line.",
    no_args_is_help=True,
    pretty_exceptions_enable=False,  # no traceback with local variables or paths
)
register_generator(app)
register_etl(app)


def _version_callback(value: bool) -> None:
    """Print the package version and exit when --version is given."""
    if value:
        typer.echo(f"vl_data {vl_data.__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool | None = typer.Option(
        None,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Show the version and exit.",
    ),
) -> None:
    """Vehicle-location data product command line."""
