import logging
import os
import stat

import pytest

from gexis_roon import VERSION, roon


def test_one_extension_per_player_named_after_it():
    info = roon.appinfo("sofapi")
    assert info["extension_id"] == "gexis_player_sofapi"
    assert info["display_name"] == "Gexis Player (sofapi)"
    assert info["display_version"] == VERSION
    assert {"publisher", "email"} <= set(info)


def test_what_plays_never_reaches_the_log():
    record = logging.LogRecord("roonapi", logging.ERROR, __file__, 1,
                               "Error while parsing message '%s'", ("a title",), None)
    roon._Quiet().filter(record)
    assert record.getMessage() == "Error while parsing message"
    record = logging.LogRecord("roonapi", logging.WARNING, __file__, 1,
                               "unknown state change: {'zone': 'Kitchen'}", (), None)
    roon._Quiet().filter(record)
    assert record.getMessage() == "unknown state change"


def test_the_token_is_kept_for_its_owner_only(tmp_path):
    path = tmp_path / "roon.json"
    roon.save({"token": "t", "core_id": "c"}, path)
    assert roon.load(path) == {"token": "t", "core_id": "c"}
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert roon.load(tmp_path / "none.json") == {}


def test_the_version_is_the_package_s():
    from pathlib import Path

    text = (Path(__file__).parents[1] / "pyproject.toml").read_text()
    assert f'version = "{VERSION}"' in text


# --- against the library itself (Debian's python3-roonapi), where it is ----


@pytest.fixture
def Api(monkeypatch):
    pytest.importorskip("roonapi")
    from roonapi import RoonApi

    monkeypatch.setattr(RoonApi, "_server_setup", lambda self, host, port: None)
    return roon.api_class()


def test_each_api_keeps_its_own_zones(Api):
    seen = []
    first = Api({"extension_id": "x"}, None, "core", 9330, seen.append)
    second = Api({"extension_id": "x"}, None, "core", 9330, seen.append)
    try:
        first._on_state_change({"zones": [{"zone_id": "z1", "state": "playing"}]})
        assert first.zones and not second.zones
        assert seen == [first], "the change names its own api"
    finally:
        first.stop()
        second.stop()


def test_a_changed_zone_replaces_the_one_it_had(Api):
    api = Api({"extension_id": "x"}, None, "core", 9330, lambda api: None)
    try:
        api._on_state_change({"zones": [{"zone_id": "z1", "state": "playing",
                                         "now_playing": {"length": 200}}]})
        api._on_state_change({"zones_changed": [{"zone_id": "z1", "state": "stopped"}]})
        assert api.zones["z1"] == {"zone_id": "z1", "state": "stopped"}
        api._on_state_change({"zones_seek_changed": [{"zone_id": "z1", "seek_position": 3}]})
        assert api.zones["z1"]["state"] == "stopped", "a seek is merged, as before"
    finally:
        api.stop()
