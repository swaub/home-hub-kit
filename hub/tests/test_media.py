import io
from pathlib import Path

import pytest

from homehub.config import parse_config
from homehub.media import MAX_CHUNK, MediaError, MediaLibrary, season_of, target_for

OWNER = {"person": "alex", "role": "owner"}


def test_files_are_named_the_way_jellyfin_expects():
    assert target_for("movie", "inception.2010.1080p.mkv", "Inception", 2010) == Path("Movies/Inception (2010)/Inception (2010).mkv")
    assert target_for("movie", "Inception.en.srt", "Inception", "2010") == Path("Movies/Inception (2010)/Inception (2010).en.srt")
    assert target_for("movie", "poster.jpg", "Inception", 2010) == Path("Movies/Inception (2010)/poster.jpg")
    assert target_for("movie", "Some Film.mp4") == Path("Movies/Some Film/Some Film.mp4")
    assert target_for("show", "The.Office.S02E03.mkv", "The Office") == Path("Shows/The Office/Season 02/The.Office.S02E03.mkv")
    assert target_for("show", "episode 4.mkv", "The Office", season=3) == Path("Shows/The Office/Season 03/episode 4.mkv")
    assert target_for("show", "Show 1x05.mp4", "Show") == Path("Shows/Show/Season 01/Show 1x05.mp4")
    assert target_for("music", "01 Song.flac", "Artist", album="Album") == Path("Music/Artist/Album/01 Song.flac")
    assert target_for("music", "song.mp3") == Path("Music/song.mp3")
    assert target_for("movie", "../../etc/passwd.mkv", "../../x", None) == Path("Movies/x/x.mkv")
    assert season_of("S11E02.mkv") == 11 and season_of("nothing.mkv") is None


def test_wrong_files_and_missing_details_are_refused():
    for kind, name, title, message in (("movie", "virus.exe", "", "isn't a movie file"), ("music", "film.mkv", "", "isn't a music file"),
                                       ("show", "S01E01.mkv", "", "Give the show's name"), ("show", "random.mkv", "Show", "can't tell which season"),
                                       ("podcast", "a.mp3", "", "Choose movie, show or music")):
        with pytest.raises(MediaError, match=message):
            target_for(kind, name, title)
    with pytest.raises(MediaError, match="year doesn't look right"):
        target_for("movie", "a.mkv", "A", 1700)


def library(tmp_path):
    cfg = parse_config({"paths": {"data_dir": str(tmp_path / "data"), "state_dir": str(tmp_path / "state")}, "media": {"folder": str(tmp_path / "media")}})
    for folder in ("Movies", "Shows", "Music"):
        (tmp_path / "media" / folder).mkdir(parents=True)
    return MediaLibrary(cfg)


def test_an_upload_arrives_in_pieces_resumes_and_lands_under_its_proper_name(tmp_path):
    lib = library(tmp_path)
    data = bytes(range(256)) * 4096
    first = lib.act({"action": "start", "kind": "movie", "name": "film.mkv", "size": len(data), "title": "Film", "year": 2001}, OWNER)
    assert first["path"] == "Movies/Film (2001)/Film (2001).mkv" and first["received"] == 0
    assert lib.write(first["id"], 0, io.BytesIO(data[:300000]), 300000, OWNER) == {"received": 300000}
    assert lib.write(first["id"], 999, io.BytesIO(data[999:2000]), 1001, OWNER) == {"received": 300000}
    again = lib.act({"action": "start", "kind": "movie", "name": "film.mkv", "size": len(data), "title": "Film", "year": 2001}, OWNER)
    assert again["received"] == 300000
    with pytest.raises(MediaError, match="Not all of the file"):
        lib.act({"action": "finish", "id": again["id"]}, OWNER)
    lib.write(again["id"], 300000, io.BytesIO(data[300000:]), len(data) - 300000, OWNER)
    assert lib.act({"action": "finish", "id": again["id"]}, OWNER)["text"] == "Added Film (2001).mkv."
    final = tmp_path / "media/Movies/Film (2001)/Film (2001).mkv"
    assert final.read_bytes() == data and not list(final.parent.glob(".*.part"))
    assert lib.act({"action": "start", "kind": "movie", "name": "film.mkv", "size": len(data), "title": "Film", "year": 2001}, OWNER)["done"] is True
    view = lib.overview(OWNER)
    assert [l["count"] for l in view["libraries"]] == [1, 0, 0] and view["libraries"][0]["recent"] == ["Film (2001)"]


def test_uploads_are_checked_for_size_owner_and_who_may_add(tmp_path):
    lib = library(tmp_path)
    started = lib.act({"action": "start", "kind": "music", "name": "song.mp3", "size": 10, "title": "Band"}, OWNER)
    with pytest.raises(MediaError, match="more than the file's size"):
        lib.write(started["id"], 0, io.BytesIO(b"x" * 11), 11, OWNER)
    with pytest.raises(MediaError, match="pieces of up to 32 MB"):
        lib.write(started["id"], 0, io.BytesIO(b""), MAX_CHUNK + 1, OWNER)
    with pytest.raises(MediaError, match="isn't open any more"):
        lib.write(started["id"], 0, io.BytesIO(b"x"), 1, {"person": "pat", "role": "adult"})
    with pytest.raises(MediaError, match="Only adults"):
        lib.act({"action": "start", "kind": "music", "name": "a.mp3", "size": 5}, {"person": "kid", "role": "child"})
    with pytest.raises(MediaError, match="empty"):
        lib.act({"action": "start", "kind": "music", "name": "a.mp3", "size": 0}, OWNER)
    assert lib.act({"action": "cancel", "id": started["id"]}, OWNER)["text"] == "Upload cancelled."
    assert not list((tmp_path / "media/Music/Band").glob(".*.part"))
    off = MediaLibrary(parse_config({"paths": {"data_dir": str(tmp_path / "d2"), "state_dir": str(tmp_path / "s2")}}))
    with pytest.raises(MediaError, match="aren't set up"):
        off.overview(OWNER)
