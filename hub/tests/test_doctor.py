from homehub import config, doctor
from homehub.config import parse_config
from homehub.storage import SCHEMA_VERSION

CHECKS = ["config", "household", "storage", "claude", "time", "disk", "instance", "kdeconnect", "service", "backup"]


def laptop_config(tmp_path):
    return parse_config({"paths": {"data_dir": str(tmp_path / "data"), "state_dir": str(tmp_path / "state")},
                         "backup": {"dir": str(tmp_path / "backups")}}, source=config.EXAMPLE_PATH)


def test_all_checks_run_and_report_a_status(tmp_path, monkeypatch):
    monkeypatch.setenv("HOMEHUB_PEOPLE", str(config.CONFIG_DIR / "people.example.toml"))
    results = doctor.run_checks(laptop_config(tmp_path))
    assert list(results) == CHECKS
    assert all(status in ("ok", "warn", "fail", "skip") for status, _ in results.values())
    assert results["config"] == ("ok", f"loaded {config.EXAMPLE_PATH}")
    assert results["household"] == ("ok", f"3 people, 2 devices in {config.CONFIG_DIR / 'people.example.toml'}")
    assert results["storage"][0] == "ok" and f"schema v{SCHEMA_VERSION}" in results["storage"][1]
    assert "claude.ai" in results["claude"][1] or "claude auth login" in results["claude"][1]
    assert results["time"][0] in ("ok", "warn", "fail")
    assert results["disk"][0] == "ok" and "GB free" in results["disk"][1]
    assert results["instance"] == ("ok", f"assistant not running; state directory {tmp_path / 'state'}")
    assert results["kdeconnect"] == ("skip", "feature off in config")
    assert results["service"][0] in ("ok", "warn", "skip")
    assert results["backup"][0] == "warn" and "no backup yet" in results["backup"][1]


def test_format_results_counts_problems():
    text, failures = doctor.format_results({"storage": ("ok", "fine"), "backup": ("warn", "no backup yet"), "claude": ("fail", "not signed in")})
    assert text.splitlines() == ["OK    storage: fine", "WARN  backup: no backup yet", "FAIL  claude: not signed in", "1 problem(s), 1 warning(s)"]
    assert failures == 1
    assert doctor.format_results({"a": ("ok", "x")})[0].endswith("all checks passed")
    assert doctor.format_results({"a": ("warn", "x")})[0].endswith("no problems, 1 warning(s)")
