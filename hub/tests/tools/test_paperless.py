import io
import json
import random
import time
import urllib.request

import pytest

from homehub.config import load_config, parse_config
from homehub.tools.paperless import PaperlessError, PaperlessPanel, load_settings, safe_name

OWNER = {"person": "alex", "role": "owner"}


def test_documents_need_the_key_the_owner_and_a_real_document_type(tmp_path):
    cfg = parse_config({"paths": {"data_dir": str(tmp_path / "data"), "state_dir": str(tmp_path / "state")}})
    panel = PaperlessPanel(cfg)
    with pytest.raises(PaperlessError, match="Only the owner"):
        panel.overview({"person": "kid", "role": "child"})
    with pytest.raises(PaperlessError, match="isn't set up"):
        panel.overview(OWNER)
    assert safe_name("../../Tax Return 2025.PDF") == "Tax Return 2025.PDF" and safe_name("scan:1.jpg") == "scan 1.jpg"
    for bad in ("virus.exe", "", "noextension"):
        with pytest.raises(PaperlessError, match="takes PDFs"):
            safe_name(bad)
    with pytest.raises(PaperlessError, match="up to 100 MB"):
        panel.upload("a.pdf", io.BytesIO(b""), 0, OWNER)
    with pytest.raises(PaperlessError, match="No such upload"):
        panel.act({"action": "task", "id": "x"}, OWNER)
    assert panel.act({"action": "search", "query": "  "}, OWNER) == {"results": []}


@pytest.mark.live
def test_a_scan_uploads_gets_read_found_by_its_words_and_is_removed_again(tmp_path):
    from PIL import Image, ImageDraw, ImageFont

    cfg = load_config()
    settings = load_settings(cfg)
    if settings is None:
        pytest.skip("no [paperless] in secrets.toml here")
    panel = PaperlessPanel(cfg)
    word = "hubtest" + "".join(random.choice(("ka", "lo", "mi", "ne", "su", "ri", "to", "va")) for _ in range(4))
    page = Image.new("RGB", (1240, 1754), "white")
    ImageDraw.Draw(page).text((120, 200), f"Hub upload test {word}", fill="black", font=ImageFont.load_default(size=64))
    buffer = io.BytesIO()
    page.save(buffer, "PDF", resolution=150)
    data = buffer.getvalue()
    sent = panel.upload("hub-upload-test.pdf", io.BytesIO(data), len(data), OWNER)
    assert sent["task"]
    status = {}
    try:
        for _ in range(60):
            status = panel.act({"action": "task", "id": sent["task"]}, OWNER)
            if status["status"] in ("SUCCESS", "FAILURE"):
                break
            time.sleep(3)
        assert status["status"] == "SUCCESS" and status["document"], status
        found = []
        for _ in range(20):
            found = panel.act({"action": "search", "query": word}, OWNER)["results"]
            if found:
                break
            time.sleep(3)
        assert [d["id"] for d in found] == [int(status["document"])] and found[0]["thumb"].startswith("data:image/")
        assert any(d["id"] == int(status["document"]) for d in panel.overview(OWNER)["recent"])
    finally:
        if status.get("document"):
            doc = int(status["document"])
            headers = {"Authorization": f"Token {settings['token']}"}
            urllib.request.urlopen(urllib.request.Request(f"{settings['url']}/api/documents/{doc}/", method="DELETE", headers=headers), timeout=20).read()
            empty = urllib.request.Request(f"{settings['url']}/api/trash/", data=json.dumps({"action": "empty", "documents": [doc]}).encode(),
                                           headers={**headers, "Content-Type": "application/json"})
            urllib.request.urlopen(empty, timeout=20).read()
