import pytest

from homehub.config import load_config, parse_config
from homehub.tools.adguard import AdGuard, AdGuardError, build_module, clean_domain, load_login


def test_website_names_are_cleaned_and_anything_else_is_refused():
    assert clean_domain("https://Ads.Example.com/path?x=1") == "ads.example.com"
    assert clean_domain("example.co.uk.") == "example.co.uk"
    assert clean_domain("http://tv.example.org:8080") == "tv.example.org"
    for bad in ("", "localhost", "not a domain; rm -rf /", "||evil^", "@@||x^", "a" * 300 + ".com", "-bad-.com"):
        with pytest.raises(AdGuardError, match="isn't a website name"):
            clean_domain(bad)


def test_it_is_only_offered_with_a_login(tmp_path):
    cfg = parse_config({"paths": {"data_dir": str(tmp_path / "data"), "state_dir": str(tmp_path / "state")}})
    assert load_login(cfg) is None and build_module(cfg).available(cfg) is False
    with pytest.raises(AdGuardError, match="isn't set up"):
        AdGuard(cfg).overview()


def live():
    cfg = load_config()
    if load_login(cfg) is None:
        pytest.skip("no AdGuard login here")
    return AdGuard(cfg)


@pytest.mark.live
def test_the_real_adguard_reports_its_day():
    view = live().overview({"192.168.1.26": "Roku"})
    assert isinstance(view["protection"], bool) and view["queries"] >= view["blocked"] >= 0 and 0 <= view["percent"] <= 100
    assert view["lists"] and all(r["domain"] for r in view["recent_blocked"]) and len(view["recent_blocked"]) <= 12


@pytest.mark.live
def test_rules_round_trip_and_leave_the_list_as_it_was():
    guard = live()
    before = guard.call("GET", "/control/filtering/status")["user_rules"]
    try:
        assert guard.set_rule("hub-test.example.org", True) == "hub-test.example.org is now always allowed for the whole house."
        assert guard.set_rule("hub-test.example.org", True) == "hub-test.example.org is already allowed."
        assert guard.set_rule("hub-test.example.org", False) == "hub-test.example.org is now blocked for the whole house."
        rules = guard.call("GET", "/control/filtering/status")["user_rules"]
        assert "||hub-test.example.org^" in rules and "@@||hub-test.example.org^" not in rules
        assert guard.remove_rule("hub-test.example.org").startswith("Removed the rule")
        assert guard.remove_rule("hub-test.example.org") == "There's no rule for hub-test.example.org."
    finally:
        assert guard.call("GET", "/control/filtering/status")["user_rules"] == before
    with pytest.raises(AdGuardError, match="Pause for 1 to"):
        guard.pause(999)
