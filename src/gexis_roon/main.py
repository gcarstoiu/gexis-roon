# SPDX-License-Identifier: GPL-3.0-or-later
"""**The plugin**: Roon Bridge's loopback on one side, the Gexis contract on
the other (ADR-0172 in gexis-player).

Its one judgement is when Roon takes the player's output and when it gives
it back:

- **taken when Roon starts playing**: the loopback's device 0 running is
  Roon playing; the plugin sends `acquire`, and copies once the core says the
  output is free (`device_freed`);
- **kept through a pause**, as any renderer that means to play again keeps
  it; **given back** once Roon has let the loopback go for `HOLD_S`;
- **taken away by the core** (another source starts): the copy stops at
  once, Roon's zone is paused through its extension, and Roon is not
  followed again until it plays anew - the loopback stopped and running
  again, as when play is pressed in Roon - so the other source is not taken
  back from while Roon still plays into the loopback.

With Roon's extension enabled (`roon.py`), what plays in this player's zone
(`zone.py`) is the player's Now Playing, and its controls are Roon's.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import time

from gexis_roon import loopback, roon
from gexis_roon.contract import RECONNECT_S, Core, Refused
from gexis_roon.zone import ZoneFinder

logger = logging.getLogger("gexis_roon")

ID = "roon"
UNIT = "gexis-roon.service"

#: How often the loopback's state is read.
TICK_S = 0.25
#: How long Roon may leave the loopback closed before the output is given
#: back: a track change at another rate closes and reopens it in well under
#: a second; a stop leaves it closed.
HOLD_S = 10.0

RELEASE_LADDER = {"polite_grace": 1.0, "sigterm_grace": 2.0, "sigkill_grace": 2.0}

CONTROLS = ["play", "pause", "next", "previous", "seek"]
#: What Roon says its zone can do now, by the player's control.
ALLOWED = {"play": "is_play_allowed", "pause": "is_pause_allowed", "next": "is_next_allowed",
           "previous": "is_previous_allowed", "seek": "is_seek_allowed"}
TRANSPORT = {"playing": "playing", "loading": "playing", "paused": "paused", "stopped": "stopped"}
#: A position further than this from where the last one said would be by
#: now is a seek, and is said; the player moves its playhead itself.
DRIFT_S = 2.0

CAPABILITIES = {
    "audio_connection": "output",
    "acquisition_events": ["Roon starts playing to the player"],
    "supports_artwork": True,
    # The rate is Roon's own, whatever it is: nothing here resamples.
    "supports_sample_rate": True,
    # Roon plays at full scale into the loopback; the player's own volume
    # sets the level, as for Lyrion.
    "volume_managed": False,
    "volume_mechanism": "software_api",
    "dummy_mixer_card": None,
    "volume_over_bluealsa": False,
    "volume_handed": False,
    # Roon's, through its extension (ADR-0172 (6)); unavailable until it is
    # enabled and this player's zone is known.
    "controls": CONTROLS,
}


def hello() -> dict:
    return {
        "id": ID,
        "kind": "renderer",
        "name": "Roon",
        "unit": UNIT,
        "release_action": "disconnect",
        "release_ladder": RELEASE_LADDER,
        "capabilities": CAPABILITIES,
    }


def zone_metadata(zone: dict, image) -> dict:
    """The player's metadata for what plays in a Roon zone. `image` makes a
    cover's address from Roon's key and a size."""
    playing = zone.get("now_playing") or {}
    lines = playing.get("three_line") or {}
    metadata = {"transport": TRANSPORT.get(zone.get("state"), "stopped")}
    for field, line in (("title", "line1"), ("artist", "line2"), ("album", "line3")):
        if lines.get(line):
            metadata[field] = lines[line]
    if playing.get("length"):
        metadata["duration"] = float(playing["length"])
    position = zone.get("seek_position", playing.get("seek_position"))
    if playing and position is not None:
        metadata["position"] = float(position)
    if playing.get("image_key"):
        metadata["artwork"] = image(playing["image_key"], roon.ARTWORK_SIZE)
        metadata["artwork_small"] = image(playing["image_key"], roon.ARTWORK_ROW)
    metadata["unavailable"] = [c for c in CONTROLS if not zone.get(ALLOWED[c])
                               or (c == "seek" and "duration" not in metadata)]
    return metadata


class Plugin:
    """Lives for the process; the connection to the core comes and goes."""

    def __init__(self, copier: loopback.Copier | None = None, clock=time.monotonic,
                 link: roon.Link | None = None) -> None:
        self.core: Core | None = None
        self.copier = copier or loopback.Copier()
        self._clock = clock
        #: Roon's extension, when the library is there to speak it.
        self.roon = link
        self.finder = ZoneFinder(link.remembered if link else (), clock)
        self._zones: dict = {}
        self._sent: dict | None = None
        self._sent_at = 0.0
        #: The output is ours: the core said so and has not taken it back.
        self.active = False
        #: `acquire` sent, `device_freed` not yet come.
        self.asked = False
        #: The core took the output while Roon played on: not followed again
        #: until the loopback stops running.
        self.yielded = False
        self._closed_since: float | None = None
        self._playing = False
        self._stream: loopback.Stream | None = None
        self._transport: str | None = None
        self._state: str | None = None

    # --- to the core --------------------------------------------------------

    async def _say(self, t: str, **fields) -> bool:
        if self.core is None or not self.core.connected:
            return False
        try:
            await self.core.event(t, **fields)
            return True
        except (ConnectionError, OSError) as exc:
            logger.info("could not tell the core %s: %s", t, exc)
            return False

    async def _transport_is(self, transport: str) -> None:
        self._transport = transport
        await self._publish()

    def zone(self) -> dict | None:
        """This player's Roon zone, once found."""
        zone_id = self.finder.zone_id()
        return self._zones.get(zone_id) if zone_id else None

    async def _publish(self) -> None:
        """What plays: the zone's, or only the loopback's transport."""
        zone = self.zone()
        if zone is not None and self.roon is not None:
            metadata = zone_metadata(zone, self.roon.image)
        elif self._transport is not None:
            metadata = {"transport": self._transport, "unavailable": list(CONTROLS)}
        else:
            return
        if self._news(metadata) and await self._say("metadata", metadata=metadata):
            self._sent, self._sent_at = metadata, self._clock()

    def _news(self, metadata: dict) -> bool:
        last = self._sent
        if last is None:
            return True
        if {k: v for k, v in metadata.items() if k != "position"} != \
                {k: v for k, v in last.items() if k != "position"}:
            return True
        if (metadata.get("position") is None) != (last.get("position") is None):
            return True
        if metadata.get("position") is None:
            return False
        moved = self._clock() - self._sent_at if last["transport"] == "playing" else 0.0
        return abs(metadata["position"] - (last["position"] + moved)) > DRIFT_S

    # --- from Roon's extension ----------------------------------------------

    def zones_changed(self, zones: dict) -> None:
        """Roon's zones, whole, on the loop (`roon.Link`)."""
        self._zones = zones
        self.finder.zones(zones)
        asyncio.ensure_future(self._publish())

    def _pause_roon(self) -> None:
        """ADR-0172 (5): another source took the output; Roon is paused
        rather than left playing to nobody."""
        zone = self.zone()
        if zone is None or self.roon is None or zone.get("state") not in ("playing", "loading"):
            return

        async def pause() -> None:
            try:
                done = await self.roon.control(zone["zone_id"], "pause")
            except Exception as exc:  # noqa: BLE001 - the output is given back regardless
                logger.info("could not pause Roon: %s", exc)
                return
            logger.info("Roon %s", "paused" if done else "did not answer the pause")

        asyncio.ensure_future(pause())

    async def _transport_command(self, command, argument) -> bool:
        zone_id = self.finder.zone_id() if self.roon is not None else None
        if zone_id is None:
            raise ValueError("Roon's extension is not enabled, or Roon has not played here yet")
        if command == "seek":
            done = await self.roon.seek(zone_id, float(argument))
        elif command in ("play", "pause", "next", "previous"):
            done = await self.roon.control(zone_id, command)
        else:
            raise ValueError(f"{command} is not something Roon does here")
        if not done:
            raise ValueError("Roon did not do it")
        return True

    # --- the loopback -------------------------------------------------------

    async def tick(self, state: str, stream: loopback.Stream | None) -> None:
        """One reading of the loopback's device 0."""
        if state != self._state:
            # What Roon Bridge does with the loopback, said as it changes: a
            # takeover is read from these.
            logger.info("loopback %s", state)
            self._state = state
        self.finder.loopback(state == "running")
        if self.finder.check():
            logger.info("this player's Roon zone: %s", "found" if self.finder.zone_id() else "not known")
            if self.roon is not None:
                self.roon.remember(self.finder.outputs)
            await self._publish()
        playing = state == "running" and stream is not None
        self._playing, self._stream = playing, stream if playing else None
        if not playing:
            self.yielded = False
        if state in ("closed", "absent"):
            if self._closed_since is None:
                self._closed_since = self._clock()
            if self.copier.running:
                await self.copier.stop()
            if self.active and self._clock() - self._closed_since >= HOLD_S:
                logger.info("Roon let the player go")
                self.active = False
                await self._transport_is("stopped")
                await self._say("release")
            if not self.active:
                self.asked = False
            return
        self._closed_since = None
        if not playing:
            # Opened, paused or draining: the output kept, nothing copied.
            if self.copier.running:
                await self.copier.stop()
            if self.active:
                await self._transport_is("paused")
            return
        if self.yielded:
            return
        if not self.active:
            if not self.asked and self.core is not None and self.core.connected:
                logger.info("Roon started playing; asking for the output")
                self.asked = True
                await self._say("acquire")
            return
        await self._transport_is("playing")
        if not self.copier.running or self.copier.stream != stream:
            await self.copier.start(stream)

    # --- from the core ------------------------------------------------------

    async def command(self, t: str, message: dict):
        if t == "device_freed":
            self.active = True
            self.asked = False
            if self._playing:
                await self.tick("running", self._stream)
            return True
        if t in ("release", "signal_stop"):
            # Another source takes the output: the copy stops now.
            self.active = False
            self.asked = False
            self.yielded = self._playing
            self._pause_roon()
            await self.copier.stop()
            await self._transport_is("stopped")
            return True
        if t in ("restart_after_release", "setting", "output"):
            return True
        if t == "transport":
            return await self._transport_command(message.get("command"), message.get("argument"))
        raise ValueError(f"{t} is not something this plugin does")

    async def connected(self, core: Core) -> None:
        """A new connection: the core knows nothing of us yet."""
        self.core = core
        if self.active:
            await self.copier.stop()
        self.active = False
        self.asked = False
        self._transport = None
        self._sent = None


async def watch(plugin: Plugin, read=loopback.read) -> None:
    while True:
        state, stream = read()
        try:
            await plugin.tick(state, stream)
        except OSError as exc:
            logger.warning("the copy could not start: %s", exc)
        await asyncio.sleep(TICK_S)


async def run(socket_path: str) -> None:
    plugin = Plugin()
    link = roon.Link(asyncio.get_running_loop(), plugin.zones_changed)
    plugin.roon, plugin.finder = link, ZoneFinder(link.remembered)
    link.start()
    watching = asyncio.ensure_future(watch(plugin))
    try:
        while True:
            core = Core(socket_path)
            try:
                await core.connect(hello())
            except Refused as exc:
                logger.error("the core refused us: %s", exc)
                return
            except (ConnectionError, OSError) as exc:
                logger.info("cannot reach the core (%s); retrying", exc)
                await asyncio.sleep(RECONNECT_S)
                continue
            await plugin.connected(core)
            try:
                await core.serve(plugin.command)
            finally:
                await core.close()
                plugin.core = None
            logger.info("disconnected; reconnecting in %.0fs", RECONNECT_S)
            await asyncio.sleep(RECONNECT_S)
    finally:
        watching.cancel()
        link.stop()
        await plugin.copier.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description="Roon as a Gexis plugin")
    parser.add_argument("--socket", default=os.environ.get("GEXIS_SOCKET", "/run/gexis/plugins.sock"))
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(name)s %(levelname)s %(message)s")
    try:
        asyncio.run(run(args.socket))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
