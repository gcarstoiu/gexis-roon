import pytest

from gexis_roon import main
from gexis_roon.loopback import Stream

CD = Stream("S16_LE", 44100, 2)
HIRES = Stream("S32_LE", 96000, 2)


class FakeCore:
    connected = True

    def __init__(self):
        self.said = []

    async def event(self, t, **fields):
        self.said.append((t, fields))


class FakeCopier:
    def __init__(self):
        self.stream = None
        self.starts = 0

    @property
    def running(self):
        return self.stream is not None

    async def start(self, stream):
        self.stream = stream
        self.starts += 1

    async def stop(self):
        self.stream = None


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def plugin():
    p = main.Plugin(copier=FakeCopier(), clock=Clock())
    p.core = FakeCore()
    return p


def said(plugin):
    return [t for t, _ in plugin.core.said]


async def test_roon_playing_asks_once_and_copies_when_freed(plugin):
    await plugin.tick("running", CD)
    await plugin.tick("running", CD)
    assert said(plugin) == ["acquire"] and not plugin.copier.running
    await plugin.command("device_freed", {})
    assert plugin.copier.stream == CD
    assert ("metadata", {"metadata": {"transport": "playing", "unavailable": main.CONTROLS}}) in plugin.core.said


async def test_a_new_rate_restarts_the_copy_at_it(plugin):
    await plugin.tick("running", CD)
    await plugin.command("device_freed", {})
    await plugin.tick("closed", None)
    await plugin.tick("running", HIRES)
    assert plugin.copier.stream == HIRES and plugin.active and "release" not in said(plugin)


async def test_a_pause_keeps_the_output(plugin):
    await plugin.tick("running", CD)
    await plugin.command("device_freed", {})
    await plugin.tick("paused", CD)
    assert plugin.active and not plugin.copier.running
    assert plugin.core.said[-1] == ("metadata", {"metadata": {"transport": "paused", "unavailable": main.CONTROLS}})


async def test_a_stop_gives_the_output_back_after_the_hold(plugin):
    await plugin.tick("running", CD)
    await plugin.command("device_freed", {})
    await plugin.tick("closed", None)
    plugin._clock.now += main.HOLD_S - 1
    await plugin.tick("closed", None)
    assert "release" not in said(plugin)
    plugin._clock.now += 2
    await plugin.tick("closed", None)
    assert said(plugin)[-1] == "release" and not plugin.active


async def test_taken_away_it_waits_for_roon_to_play_anew(plugin):
    await plugin.tick("running", CD)
    await plugin.command("device_freed", {})
    assert await plugin.command("release", {}) is True
    assert not plugin.copier.running and not plugin.active
    await plugin.tick("running", CD)
    assert said(plugin).count("acquire") == 1
    await plugin.tick("prepared", CD)
    await plugin.tick("running", CD)
    assert said(plugin).count("acquire") == 2


async def test_transport_is_refused_without_the_extension(plugin):
    with pytest.raises(ValueError):
        await plugin.command("transport", {"command": "pause"})


def test_hello_uses_the_contracts_own_words():
    """The core refuses a hello with a value it does not know
    (docs/PLUGIN-CONTRACT.md in gexis-player): 0.1.0's first try said
    `volume_mechanism: none` and was refused."""
    hello = main.hello()
    assert hello["kind"] == "renderer" and hello["release_action"] in ("pause", "disconnect")
    assert hello["capabilities"]["volume_mechanism"] in ("dummy_mixer", "software_api")
    assert hello["capabilities"]["audio_connection"] == "output"
