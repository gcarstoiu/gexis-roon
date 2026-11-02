import asyncio

import pytest

from gexis_roon import main
from gexis_roon.loopback import Stream
from gexis_roon.zone import SETTLE_S

from test_plugin import Clock, FakeCopier, FakeCore

CD = Stream("S32_LE", 44100, 2)


class FakeLink:
    remembered = []

    def __init__(self):
        self.asked = []
        self.kept = None

    def remember(self, outputs):
        self.kept = outputs

    def image(self, key, size):
        return f"http://core/api/image/{key}?width={size}"

    async def control(self, zone_id, command):
        self.asked.append((zone_id, command))
        return True

    async def seek(self, zone_id, seconds):
        self.asked.append((zone_id, "seek", seconds))
        return True


def zone(state="playing", position=12, **flags):
    return {
        "zone_id": "z1", "state": state, "seek_position": position,
        "outputs": [{"output_id": "o1"}],
        "now_playing": {"length": 240, "image_key": "cover1",
                        "three_line": {"line1": "Title", "line2": "Artist", "line3": "Album"}},
        **{f: True for f in main.ALLOWED.values()}, **flags,
    }


@pytest.fixture
def plugin():
    p = main.Plugin(copier=FakeCopier(), clock=Clock(), link=FakeLink())
    p.core = FakeCore()
    return p


def metadata_said(plugin):
    return [f["metadata"] for t, f in plugin.core.said if t == "metadata"]


async def found(plugin, z):
    plugin.zones_changed({"z1": z})
    await plugin.tick("running", CD)
    plugin._clock.now += SETTLE_S
    await plugin.tick("running", CD)
    await asyncio.sleep(0)


async def test_what_plays_in_this_player_s_zone_is_its_now_playing(plugin):
    await found(plugin, zone())
    assert plugin.roon.kept == {"o1"}, "remembered for the next start"
    assert metadata_said(plugin)[-1] == {
        "transport": "playing", "title": "Title", "artist": "Artist", "album": "Album",
        "duration": 240.0, "position": 12.0,
        "artwork": "http://core/api/image/cover1?width=500",
        "artwork_small": "http://core/api/image/cover1?width=100",
        "unavailable": [],
    }


async def test_before_the_zone_is_known_the_controls_are_unavailable(plugin):
    await plugin.tick("running", CD)
    await plugin.command("device_freed", {})
    assert metadata_said(plugin)[-1] == {"transport": "playing", "unavailable": main.CONTROLS}
    with pytest.raises(ValueError):
        await plugin.command("transport", {"command": "pause"})


async def test_the_controls_are_roon_s(plugin):
    await found(plugin, zone())
    assert await plugin.command("transport", {"command": "next", "argument": None}) is True
    assert await plugin.command("transport", {"command": "seek", "argument": 90}) is True
    assert plugin.roon.asked == [("z1", "next"), ("z1", "seek", 90.0)]


async def test_what_roon_cannot_do_now_is_unavailable(plugin):
    await found(plugin, zone(is_next_allowed=False))
    assert metadata_said(plugin)[-1]["unavailable"] == ["next"]


async def test_a_position_where_it_would_be_anyway_is_not_said_again(plugin):
    await found(plugin, zone(position=12))
    count = len(metadata_said(plugin))
    plugin._clock.now += 5
    plugin.zones_changed({"z1": zone(position=12 + 5.5)})
    await asyncio.sleep(0)
    assert len(metadata_said(plugin)) == count
    plugin.zones_changed({"z1": zone(position=200)})
    await asyncio.sleep(0)
    assert metadata_said(plugin)[-1]["position"] == 200.0, "a seek is said"


async def test_another_source_taking_over_pauses_roon(plugin):
    await found(plugin, zone())
    await plugin.command("device_freed", {})
    assert await plugin.command("release", {}) is True
    await asyncio.sleep(0)
    assert ("z1", "pause") in plugin.roon.asked
    assert not plugin.copier.running
