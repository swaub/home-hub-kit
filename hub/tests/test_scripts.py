import configparser
import subprocess

from homehub.config import REPO_ROOT

SCRIPTS = REPO_ROOT / "scripts"


def unit(name):
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.optionxform = str
    parser.read_string((SCRIPTS / name).read_text(encoding="utf-8"))
    return parser


def test_shell_scripts_parse_and_are_executable():
    for name in ("setup_hub.sh", "install_service.sh", "update_hub.sh", "backup_services.sh", "duckdns_update.sh"):
        path = SCRIPTS / name
        assert subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True).returncode == 0, name
        assert path.read_text(encoding="utf-8").startswith("#!/usr/bin/env bash\nset -euo pipefail\n"), name


def test_service_unit_matches_the_design():
    service = unit("homehub.service")
    assert service["Service"]["Type"] == "notify" and service["Service"]["WatchdogSec"] == "60"
    assert service["Service"]["Restart"] == "on-failure" and service["Service"]["RestartSec"] == "5"
    assert service["Service"]["ExecStart"] == "REPO_DIR/.venv/bin/python -m homehub run --headless"
    assert service["Service"]["WorkingDirectory"] == "REPO_DIR"
    assert service["Unit"]["After"] == "network-online.target" and service["Install"]["WantedBy"] == "default.target"
    assert "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/%U/bus" in (SCRIPTS / "homehub.service").read_text(encoding="utf-8")


def test_backup_timer_runs_nightly():
    timer = unit("homehub-backup.timer")
    assert timer["Timer"]["OnCalendar"] == "*-*-* 03:00:00" and timer["Timer"]["Persistent"] == "true"
    assert timer["Install"]["WantedBy"] == "timers.target"
    job = unit("homehub-backup.service")
    assert job["Service"]["Type"] == "oneshot" and job["Service"]["ExecStart"] == "REPO_DIR/.venv/bin/homehub backup --yes"


def test_install_and_setup_scripts_cover_every_step():
    text = (SCRIPTS / "install_service.sh").read_text(encoding="utf-8")
    for name in ("homehub.service", "homehub-backup.service", "homehub-backup.timer", "hub-services-backup.service", "hub-services-backup.timer",
                 "duckdns-update.service", "duckdns-update.timer", "cert-renew.service", "cert-renew.timer", "notify-failure@.service"):
        assert name in text, name
    for step in ("loginctl enable-linger", "systemctl --user daemon-reload", "enable --now homehub", "enable --now homehub-backup.timer", "s|REPO_DIR|"):
        assert step in text, step
    setup = (SCRIPTS / "setup_hub.sh").read_text(encoding="utf-8")
    for step in ("wifi.powersave = 2", "systemctl mask sleep.target", "python3 -m venv .venv", "pip install -e", "claude.ai/install.sh", "config.example.toml", "people.example.toml"):
        assert step in setup, step
    assert "libportaudio2" not in setup and "download_models" not in setup
    update = (SCRIPTS / "update_hub.sh").read_text(encoding="utf-8")
    assert "git pull --ff-only" in update and "systemctl --user restart homehub" in update


def test_hub_job_units_parse_and_name_their_scripts():
    services = {"hub-services-backup.service": "REPO_DIR/scripts/backup_services.sh", "duckdns-update.service": "REPO_DIR/scripts/duckdns_update.sh",
                "cert-renew.service": "acme.sh --cron"}
    for name, command in services.items():
        assert command in unit(name)["Service"]["ExecStart"], name
    for name in ("hub-services-backup.service", "duckdns-update.service", "cert-renew.service"):
        assert unit(name)["Service"]["Type"] == "oneshot", name
    for name in ("hub-services-backup.timer", "duckdns-update.timer", "cert-renew.timer"):
        timer = unit(name)
        assert timer["Install"]["WantedBy"] == "timers.target" and ("OnCalendar" in timer["Timer"] or "OnUnitActiveSec" in timer["Timer"]), name


def test_failed_hub_jobs_push_a_notification_through_the_template_unit():
    for name in ("homehub-backup.service", "hub-services-backup.service", "duckdns-update.service"):
        assert unit(name)["Unit"]["OnFailure"] == "notify-failure@%n.service", name
    template = unit("notify-failure@.service")
    assert "OnFailure" not in template["Unit"] and template["Service"]["Type"] == "oneshot"
    assert template["Service"]["ExecStart"].startswith('REPO_DIR/.venv/bin/homehub notify --title "Hub job failed" ')
    assert "%i failed on the hub" in template["Service"]["ExecStart"]


def test_duckdns_token_stays_out_of_the_process_list():
    text = (SCRIPTS / "duckdns_update.sh").read_text(encoding="utf-8")
    assert "curl -fsS --max-time 20 -K -" in text and '"https://www.duckdns.org/update?domains=$1&token=$token' not in text
    assert "psn" in (SCRIPTS / "setup_hub.sh").read_text(encoding="utf-8").split('pip install -e ".[', 1)[1].split("]", 1)[0].split(",")
