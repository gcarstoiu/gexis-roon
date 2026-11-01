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
  once, and Roon is not followed again until it plays anew - the loopback
  stopped and running again, as when play is pressed in Roon - so the other
  source is not taken back from while Roon still plays into the loopback.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import time

from gexis_roon import loopback
from gexis_roon.contract import RECONNECT_S, Core, Refused

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

CAPABILITIES = {
    "audio_connection": "output",
    "acquisition_events": ["Roon starts playing to the player"],
    "supports_artwork": False,
    # The rate is Roon's own, whatever it is: nothing here resamples.
    "supports_sample_rate": True,
    # Roon plays at full scale into the loopback; the player's own volume
    # sets the level, as for Lyrion.
    "volume_managed": False,
    "volume_mechanism": "none",
    "dummy_mixer_card": None,
    "volume_over_bluealsa": False,
    "volume_handed": False,
    # Transport from the player comes with Roon's extension (ADR-0172 (6)).
    "controls": [],
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


class Plugin:
    """Lives for the process; the connection to the core comes and goes."""

    def __init__(self, copier: loopback.Copier | None = None, clock=time.monotonic) -> None:
        self.core: Core | None = None
        self.copier = copier or loopback.Copier()
        self._clock = clock
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
        if transport != self._transport:
            self._transport = transport
            await self._say("metadata", metadata={"transport": transport})

    # --- the loopback -------------------------------------------------------

    async def tick(self, state: str, stream: loopback.Stream | None) -> None:
        """One reading of the loopback's device 0."""
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
            await self.copier.stop()
            await self._transport_is("stopped")
            return True
        if t in ("restart_after_release", "setting", "output"):
            return True
        if t == "transport":
            raise ValueError("Roon is controlled from Roon")
        raise ValueError(f"{t} is not something this plugin does")

    async def connected(self, core: Core) -> None:
        """A new connection: the core knows nothing of us yet."""
        self.core = core
        if self.active:
            await self.copier.stop()
        self.active = False
        self.asked = False
        self._transport = None


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
