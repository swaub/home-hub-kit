import asyncio
import json
import urllib.parse

import pytest

from homehub.config import load_config, parse_config
from homehub.tools import result_texts
from homehub.tools.google_tasks import NOT_SET_UP, VERIFIER_FILE, GoogleTasks, TasksError, build_module, load_app, ready


def world(tmp_path, secrets_text=None):
    cfg = parse_config({"paths": {"data_dir": str(tmp_path / "data"), "state_dir": str(tmp_path / "state")}})
    if secrets_text is not None:
        (tmp_path / "data").mkdir(exist_ok=True)
        (tmp_path / "data" / "secrets.toml").write_text(secrets_text, encoding="utf-8")
    return cfg


def test_tasks_stay_off_until_the_app_and_sign_in_exist(tmp_path):
    cfg = world(tmp_path)
    assert load_app(cfg) is None and not build_module(cfg).available(cfg)
    tools = {spec.name: spec.fn.handler for spec in build_module(cfg).tools}
    assert result_texts(asyncio.run(tools["tasks_read"]({})))[0] == NOT_SET_UP


def test_the_sign_in_link_uses_pkce_and_offline_access(tmp_path):
    cfg = world(tmp_path, '[google_tasks]\nclient_id = "abc.apps.googleusercontent.com"\nclient_secret = "def"\n')
    query = urllib.parse.parse_qs(urllib.parse.urlparse(GoogleTasks(cfg).auth_url()).query)
    assert query["scope"] == ["https://www.googleapis.com/auth/tasks"] and query["access_type"] == ["offline"]
    assert query["code_challenge_method"] == ["S256"] and len(query["code_challenge"][0]) == 43
    saved = json.loads((tmp_path / "data" / VERIFIER_FILE).read_text())
    assert len(saved["verifier"]) >= 43 and oct((tmp_path / "data" / VERIFIER_FILE).stat().st_mode)[-3:] == "600"


@pytest.mark.live
def test_a_bad_sign_in_is_refused_by_google_with_a_readable_message(tmp_path):
    cfg = world(tmp_path, '[google_tasks]\nclient_id = "not-real.apps.googleusercontent.com"\nclient_secret = "nope"\n')
    client = GoogleTasks(cfg)
    client.auth_url()
    with pytest.raises(TasksError, match="Google refused the sign-in"):
        client.login("http://127.0.0.1:8899/?code=bogus&scope=x")
    assert not ready(cfg)


@pytest.mark.live
def test_the_real_account_reads_the_default_list():
    cfg = load_config()
    if not ready(cfg):
        pytest.skip("Google Tasks isn't connected here")
    assert ": " in GoogleTasks(cfg).read()
