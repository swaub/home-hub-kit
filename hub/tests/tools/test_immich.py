import pytest

from homehub.config import load_config, parse_config
from homehub.tools.immich import NOT_SET_UP, ImmichError, ImmichPanel, length, load_settings, tile

OWNER = {"person": "alex", "role": "owner"}


def test_photos_need_the_key_and_the_owner(tmp_path):
    cfg = parse_config({"paths": {"data_dir": str(tmp_path / "data"), "state_dir": str(tmp_path / "state")}})
    assert load_settings(cfg) is None
    panel = ImmichPanel(cfg)
    with pytest.raises(ImmichError, match="Only the owner"):
        panel.overview({"person": "pat", "role": "adult"})
    with pytest.raises(ImmichError, match="isn't set up"):
        panel.overview(OWNER)
    assert NOT_SET_UP.startswith("Immich isn't set up")
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "secrets.toml").write_text("[immich]\nurl = 'http://10.0.0.1:2283/'\napi_key = 'k'\n", encoding="utf-8")
    assert load_settings(cfg) == {"url": "http://10.0.0.1:2283", "api_key": "k", "web": "http://10.0.0.1:2283"}
    with pytest.raises(ImmichError, match="No such photo"):
        panel.act({"action": "photo", "id": "../../etc"}, OWNER)
    with pytest.raises(ImmichError, match="No such page"):
        panel.act({"action": "more", "page": 9999}, OWNER)
    with pytest.raises(ImmichError, match="unknown photos action"):
        panel.act({"action": "delete"}, OWNER)
    assert tile({"id": "a", "type": "VIDEO", "fileCreatedAt": "t", "createdAt": "u", "duration": "0:00:12.345000"}) == {
        "id": "a", "video": True, "taken": "t", "uploaded": "u", "favorite": False, "duration": "0:12"}
    assert [length(v) for v in (139923, 7147, 3_725_000, "1:02:05.5", None)] == ["2:19", "0:07", "1:02:05", "1:02:05", None]


@pytest.mark.live
def test_the_real_library_shows_counts_thumbnails_and_a_full_photo():
    cfg = load_config()
    if load_settings(cfg) is None:
        pytest.skip("no [immich] in secrets.toml here")
    panel = ImmichPanel(cfg)
    view = panel.overview(OWNER)
    assert view["photos"] + view["videos"] > 0 and view["disk"]["size"] > view["disk"]["free"] > 0
    assert view["recent"] and all(t["thumb"].startswith("data:image/") for t in view["recent"])
    photo = panel.act({"action": "photo", "id": view["recent"][0]["id"]}, OWNER)
    assert photo["image"].startswith("data:image/") and photo["open"].endswith(view["recent"][0]["id"])
    if view["next"]:
        assert panel.act({"action": "more", "page": int(view["next"])}, OWNER)["items"]
