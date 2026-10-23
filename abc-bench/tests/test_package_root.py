import importlib
from pathlib import Path
import sys


def test_get_package_root_resolves_repo_root_layout() -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

    config = importlib.import_module("abc_bench.config")
    package_root = config.get_package_root()

    assert package_root == Path(__file__).resolve().parent.parent
    assert (package_root / "harnesses").is_dir()
    assert (package_root / "containers").is_dir()


def test_default_config_template_enables_sandbox_network() -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

    config = importlib.import_module("abc_bench.config")

    assert "sandbox_network: true" in config.DEFAULT_CONFIG_TEMPLATE


def test_default_config_template_declares_package_root() -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

    config = importlib.import_module("abc_bench.config")

    assert "package_root: null" in config.DEFAULT_CONFIG_TEMPLATE
    assert "data_dir" not in config.DEFAULT_CONFIG_TEMPLATE


def test_release_package_root_override(monkeypatch, tmp_path: Path) -> None:
    config = importlib.import_module("abc_bench.config")
    (tmp_path / "harnesses").mkdir()
    (tmp_path / "containers").mkdir()
    monkeypatch.setenv("ABC_BENCH_PACKAGE_ROOT", str(tmp_path))

    assert config.get_package_root() == tmp_path


def test_release_default_config_location() -> None:
    config = importlib.import_module("abc_bench.config")

    assert config.DEFAULT_CONFIG_DIR == Path("~/.abc-bench")
    assert config.DEFAULT_CONFIG_PATH == Path("~/.abc-bench/config.yaml")


def test_default_config_uses_portable_example_provider(tmp_path: Path) -> None:
    import yaml
    from abc_bench.config import Config, DEFAULT_CONFIG_TEMPLATE

    data = yaml.safe_load(DEFAULT_CONFIG_TEMPLATE)
    assert data["providers"]["example"] == {
        "base_url": "https://api.example.com/v1",
        "api_key_env": "MODEL_API_KEY",
        "cert": None,
    }
    assert data["models"]["example-model"]["provider"] == "example"
    assert data["models"]["example-model"]["model"] == "your-model-id"
    assert data["agents"]["opencode-default"]["models"] == ["example-model"]

    config_path = tmp_path / "config.yaml"
    config_path.write_text(DEFAULT_CONFIG_TEMPLATE)
    config = Config(config_path)
    assert config.get_model("example-model")["provider"] == "example"


def test_release_cli_name(capsys, monkeypatch) -> None:
    import pytest
    from abc_bench.cli import main

    monkeypatch.setattr(sys, "argv", ["abc-bench", "--help"])
    with pytest.raises(SystemExit) as exc:
        main()

    assert exc.value.code == 0
    assert "usage: abc-bench " in capsys.readouterr().out
