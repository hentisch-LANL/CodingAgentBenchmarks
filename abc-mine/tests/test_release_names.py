"""Public release names and the exported verifier environment contract."""

from importlib import resources

import pytest

from abc_mine import __version__, cli


def test_cli_release_name(capsys):
    parser = cli.build_parser()
    assert parser.prog == "abc-mine"
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"abc-mine {__version__}"


@pytest.mark.parametrize(
    ("options", "expected"),
    [([], True), (["--abc-bench-root"], True), (["--raw"], False)],
)
def test_export_destination_mode(options, expected):
    args = cli.build_parser().parse_args(
        ["export", "--config", "mine.yaml", "--dest", "../abc-bench", *options]
    )
    assert args.abc_bench_root is expected


@pytest.mark.parametrize("name", ["build_system.md", "build_task.md"])
def test_build_templates_use_release_workspace_variable(name):
    template = resources.files("abc_mine").joinpath("templates", name).read_text()
    assert "ABC_BENCH_WORKSPACE" in template
    assert "LANLBENCH" not in template
