# SPDX-License-Identifier: GPL-3.0-or-later
"""**Which of Roon's zones is this player's** (ADR-0172 decision 6 in
gexis-player: found automatically, no setting).

Roon's extension API names zones and outputs by ids of its own and by the
names the user gave them; nothing in it says which device an output is on.
The loopback says it instead. While Roon Bridge plays into it, this player's
output is in a zone that is playing; while it is closed, it is in none that
is. Each reading, taken once both have held still for `SETTLE_S`, keeps the
outputs that agree, and the one left is this player's. A group in Roon keeps
its outputs together, which is right: the group is the zone this player
plays in.

Readings that keep nothing, through two plays of the loopback in a row,
start again from what plays then: the zone was replaced, or the remembered
output was another core's.
"""
from __future__ import annotations

import time

#: How long the loopback and Roon's zones must hold still before a reading.
#: Roon's zone update and the loopback's opening are well under a second
#: apart; a track change closes and reopens the loopback in less.
SETTLE_S = 3.0


class ZoneFinder:
    def __init__(self, remembered=(), clock=time.monotonic) -> None:
        #: This player's output, or the outputs it may still be; None when
        #: nothing is known.
        self.outputs: frozenset[str] | None = frozenset(remembered) or None
        self._clock = clock
        self._loopback = False
        self._plays = 0
        self._layout: frozenset[tuple[str, str]] = frozenset()
        self._playing: frozenset[str] = frozenset()
        self._active: frozenset[str] = frozenset()
        self._since = clock()
        self._read = True
        self._missed_in: int | None = None

    def loopback(self, playing: bool) -> None:
        """Whether Roon Bridge plays into the loopback now."""
        if playing != self._loopback:
            self._loopback = playing
            self._plays += playing
            self._moved()

    def zones(self, zones: dict) -> None:
        """Roon's zones, by id, as its transport service describes them."""
        layout = frozenset((o["output_id"], zone_id) for zone_id, zone in zones.items()
                           for o in zone.get("outputs") or () if "output_id" in o)
        playing = frozenset(z for z, zone in zones.items() if zone.get("state") == "playing")
        # Loading is about to play: counted when the loopback plays, never
        # held against an output when it does not.
        active = playing | {z for z, zone in zones.items() if zone.get("state") == "loading"}
        if (layout, playing, active) != (self._layout, self._playing, self._active):
            self._layout, self._playing, self._active = layout, playing, active
            self._moved()

    def _moved(self) -> None:
        self._since = self._clock()
        self._read = False

    def check(self) -> bool:
        """A reading, once both have held still and not been read since.
        True when what is known changed."""
        if self._read or not self._layout or self._clock() - self._since < SETTLE_S:
            return False
        self._read = True
        known = self.outputs
        listed = frozenset(o for o, _ in self._layout)
        if known is not None:
            # An output Roon no longer lists: a zone removed, another core.
            known = known & listed or None
        if self._loopback:
            here = frozenset(o for o, z in self._layout if z in self._active)
            if known is None:
                new = here or None
            elif known & here:
                new, self._missed_in = known & here, None
            elif self._missed_in is not None and self._missed_in != self._plays:
                new, self._missed_in = here or None, None
            else:
                new, self._missed_in = known, self._plays
        elif known is not None:
            elsewhere = frozenset(o for o, z in self._layout if z in self._playing)
            new = known - elsewhere or known
        else:
            new = None
        changed = new != self.outputs
        self.outputs = new
        return changed

    def zone_id(self) -> str | None:
        """The zone this player plays in, once its outputs are all in one."""
        if not self.outputs:
            return None
        zones = {z for o, z in self._layout if o in self.outputs}
        return zones.pop() if len(zones) == 1 else None
