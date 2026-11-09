import tomllib
from datetime import timedelta

import pytest

from homehub.config import load_config, parse_config
from homehub.tools.psn import Psn, PsnError, build_module, describe_status, hours, image_url, load_login, presence_of, with_token

SECRETS = '[router]\npassword = "r"\n\n[psn]\nnpsso = "OLD"\nexpires_at = 1\n\n[tv]\npsk = "1234"\n'


def test_presence_reads_online_offline_and_the_game():
    playing = presence_of({"basicPresence": {"availability": "availableToPlay", "primaryPlatformInfo": {"onlineStatus": "online", "platform": "PS5"},
                                             "gameTitleInfoList": [{"titleName": "Roblox", "conceptIconUrl": "https://image.api.playstation.com/x.png"}]}})
    assert playing == {"online": True, "platform": "PS5", "last_online": None, "game": "Roblox", "game_image": "https://image.api.playstation.com/x.png"}
    away = presence_of({"accountId": "1", "availability": "unavailable", "lastAvailableDate": "2026-11-08T20:16:36Z",
                        "primaryPlatformInfo": {"onlineStatus": "offline", "platform": "PS5", "lastOnlineDate": "2026-11-08T20:16:36Z"}})
    assert away["online"] is False and away["game"] == "" and away["last_online"] == "2026-11-08T20:16:36Z"
    assert hours(timedelta(hours=9, minutes=29, seconds=23)) == "9 h 29 min" and hours(timedelta(minutes=50)) == "50 min" and hours(None) == "0 min"


def test_only_playstation_image_hosts_are_fetched_and_large_pictures_are_shrunk():
    assert image_url("https://image.api.playstation.com/vulcan/a.png") == "https://image.api.playstation.com/vulcan/a.png?w=200"
    assert image_url("https://image.api.playstation.com/a.png?x=1") == "https://image.api.playstation.com/a.png?x=1&w=200"
    assert image_url("https://psnobj.prod.dl.playstation.net/t.png") == "https://psnobj.prod.dl.playstation.net/t.png"
    assert image_url("http://static-resource.np.community.playstation.net/avatar/a.png") == "https://static-resource.np.community.playstation.net/avatar/a.png"
    assert image_url("http://image.api.playstation.com/a.png") == "https://image.api.playstation.com/a.png?w=200"
    for bad in ("http://evil.example/a.png", "https://evil.example/a.png", "ftp://image.api.playstation.com/a.png", "file:///etc/passwd", "", None):
        assert image_url(bad) is None


def test_a_new_sign_in_replaces_only_the_psn_section():
    updated = with_token(SECRETS, "N" * 64, 1799355805)
    parsed = tomllib.loads(updated)
    assert parsed["psn"] == {"npsso": "N" * 64, "expires_at": 1799355805}
    assert parsed["router"] == {"password": "r"} and parsed["tv"] == {"psk": "1234"}
    assert tomllib.loads(with_token('[tv]\npsk = "1"\n', "M" * 64, 5))["psn"]["npsso"] == "M" * 64
    assert tomllib.loads(with_token("", "M" * 64, 5)) == {"psn": {"npsso": "M" * 64, "expires_at": 5}}


def test_status_sentences():
    view = {"online_id": "PlayerOne", "me": {"online": True, "game": "Roblox"},
            "friends": [{"name": "A", "online": True, "game": ""}, {"name": "B", "online": False, "game": ""}]}
    assert describe_status(view) == "PlayerOne is online, playing Roblox. Friends online: A."
    assert describe_status({**view, "me": {"online": False, "game": ""}, "friends": []}) == "PlayerOne is offline on PlayStation. None of 0 friends are online."


def test_renewing_is_for_the_owner_and_needs_a_real_looking_code(tmp_path):
    cfg = parse_config({"paths": {"data_dir": str(tmp_path / "data"), "state_dir": str(tmp_path / "state")}})
    psn = Psn(cfg)
    assert load_login(cfg) is None and build_module(cfg).available(cfg) is False
    with pytest.raises(PsnError, match="isn't connected"):
        psn.overview()
    with pytest.raises(PsnError, match="Only the household's owner"):
        psn.act({"action": "set_token", "token": "x" * 64}, {"person": "kid", "role": "child"})
    for bad in ("", "short", '{"npsso": "abc"}', "x" * 63 + "!"):
        with pytest.raises(PsnError, match="isn't an npsso code"):
            psn.act({"action": "set_token", "token": bad}, {"person": "alex", "role": "owner"})
    with pytest.raises(PsnError, match="unknown PlayStation action"):
        psn.act({"action": "buy"})


def test_reminders_come_a_week_ahead_and_the_day_before_once_each(tmp_path):
    sent = []
    psn = Psn(parse_config({"paths": {"data_dir": str(tmp_path / "d"), "state_dir": str(tmp_path / "s")}}), notify=lambda title, text: sent.append(title))
    for days in (30, 8, 7, 6, 5, 1, 1, 0):
        psn.remind(days)
    assert sent == ["PlayStation sign-in expires in 7 days", "PlayStation sign-in expires tomorrow"]


@pytest.mark.live
def test_the_real_account_loads_and_answers_the_chat():
    import asyncio

    from homehub.tools import result_texts

    cfg = load_config()
    if load_login(cfg) is None:
        pytest.skip("no PSN sign-in here")
    psn = Psn(cfg)
    view = psn.load()
    assert view["online_id"] and isinstance(view["friends"], list) and view["library"] and view["trophies"]["level"] >= 1
    assert psn.overview()["online_id"] == view["online_id"]
    tools = {spec.name: spec.fn.handler for spec in build_module(cfg, psn).tools}
    assert result_texts(asyncio.run(tools["psn_games"]({})))[0].startswith("Recently played: ")
