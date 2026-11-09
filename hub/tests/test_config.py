import dataclasses
from pathlib import Path

import pytest

from homehub import config
from homehub.config import Config, ConfigError, load_config, parse_config

EXAMPLE = Path(config.EXAMPLE_PATH).read_text(encoding="utf-8")


def write(tmp_path, text):
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_example_loads_and_matches_the_code_defaults():
    cfg = load_config(config.EXAMPLE_PATH)
    defaults = Config()
    for f in dataclasses.fields(Config):
        if f.name != "source":
            assert getattr(cfg, f.name) == getattr(defaults, f.name), f.name
    assert cfg.source == config.EXAMPLE_PATH


def test_example_matches_the_code_defaults():
    cfg = load_config(config.EXAMPLE_PATH)
    assert cfg.brain.model == "claude-haiku-5-5" and cfg.brain.effort == "low"
    assert cfg.remote.address == "10.77.0.1" and cfg.web.host == "127.0.0.1"


def test_unknown_setting_is_rejected_by_name(tmp_path):
    with pytest.raises(ConfigError, match=r"unknown setting brain\.modle$"):
        load_config(write(tmp_path, EXAMPLE.replace('model = "claude-haiku-5-5"', 'modle = "claude-haiku-5-5"')))


def test_unknown_section_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="unknown setting camera"):
        load_config(write(tmp_path, EXAMPLE + "\n[camera]\nloud = true\n"))


def test_voice_sections_are_not_settings(tmp_path):
    for section in ("rooms", "tts", "wake"):
        with pytest.raises(ConfigError, match=f"unknown setting {section}"):
            parse_config({section: {}})


def test_wrong_types_are_rejected(tmp_path):
    with pytest.raises(ConfigError, match=r"brain\.max_turns must be a whole number, not a number"):
        load_config(write(tmp_path, EXAMPLE.replace("max_turns = 20", "max_turns = 2.5")))
    with pytest.raises(ConfigError, match=r"features\.google must be true or false"):
        load_config(write(tmp_path, EXAMPLE.replace("google = true", 'google = "yes"')))
    with pytest.raises(ConfigError, match=r"remote\.peers must be a list"):
        load_config(write(tmp_path, EXAMPLE.replace("peers = []", 'peers = "10.77.0.2=alex"')))


def test_unknown_effort_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="brain.effort must be one of low, medium, high"):
        load_config(write(tmp_path, EXAMPLE.replace('effort = "low"', 'effort = "max"')))


def test_bad_peers_and_apps_are_rejected():
    with pytest.raises(ConfigError, match="remote.peers entries must look like"):
        parse_config({"remote": {"peers": ["10.77.0.2"]}})
    with pytest.raises(ConfigError, match="remote.apps entries must look like"):
        parse_config({"remote": {"apps": ["Photos=photos.home"]}})


def test_notify_needs_both_halves():
    with pytest.raises(ConfigError, match="notify.url and notify.topic must be set together"):
        parse_config({"notify": {"url": "https://ntfy.sh"}})


def test_broken_toml_names_the_file(tmp_path):
    path = write(tmp_path, "[brain\nmodel = 1\n")
    with pytest.raises(ConfigError, match=str(path.name)):
        load_config(path)


def test_missing_file_is_reported(tmp_path):
    with pytest.raises(ConfigError, match="config file not found"):
        load_config(tmp_path / "nope.toml")


def test_paths_expand_home():
    cfg = parse_config({})
    assert cfg.data_dir() == Path.home() / ".local/share/homehub"
    assert cfg.state_dir() == Path.home() / ".local/state/homehub"


def test_config_path_prefers_explicit_then_env_then_default(tmp_path, monkeypatch):
    explicit = tmp_path / "explicit.toml"
    env_path = tmp_path / "env.toml"
    monkeypatch.setenv(config.ENV_VAR, str(env_path))
    assert config.config_path(explicit) == explicit
    assert config.config_path() == env_path
    monkeypatch.delenv(config.ENV_VAR)
    assert config.config_path() in (config.DEFAULT_PATH, config.EXAMPLE_PATH)


def test_env_var_selects_the_file(tmp_path, monkeypatch):
    path = write(tmp_path, EXAMPLE.replace('model = "claude-haiku-5-5"', 'model = "claude-sonnet-5-5"'))
    monkeypatch.setenv(config.ENV_VAR, str(path))
    assert load_config().brain.model == "claude-sonnet-5-5"


def test_example_file_has_no_comments():
    assert not any(line.lstrip().startswith("#") for line in EXAMPLE.splitlines())
