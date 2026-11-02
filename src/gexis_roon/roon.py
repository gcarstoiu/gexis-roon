# SPDX-License-Identifier: GPL-3.0-or-later
"""**Roon's extension API** (ADR-0172 decision 4 in gexis-player): what
plays in this player's zone, and play, pause, next, previous and seek for it.

Debian's `python3-roonapi` speaks it, on threads of its own; this module is
the one place those threads meet the plugin's event loop. The extension is
enabled once in Roon, under Settings → Extensions, as *Gexis Player* and the
player's name; the token Roon gives it is kept in the plugin's state
directory, with the core it came from and the output found to be this
player's (`zone.py`).
"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import socket
import threading
import time
from pathlib import Path

from gexis_roon import VERSION

logger = logging.getLogger("gexis_roon.roon")

STATE = Path(os.environ.get("STATE_DIRECTORY", "/var/lib/gexis-roon")) / "roon.json"
#: Out of reach this long, the core is looked for again: the library tries
#: only the address it was given, and a core's address can change.
REDISCOVER_S = 60.0
#: Searches for the remembered core before any core will do: a player moved
#: to another core finds it, and one whose core is only restarting does not
#: ask a second core in the house meanwhile.
ANY_CORE_AFTER = 20
#: Now playing's cover and the small places' (gexis-player's TrackMetadata).
ARTWORK_SIZE = 500
ARTWORK_ROW = 100


def appinfo(host: str) -> dict:
    """One extension per player, named after it: Roon lists each, and each
    player follows its own zone."""
    return {
        "extension_id": f"gexis_player_{host}",
        "display_name": f"Gexis Player ({host})",
        "display_version": VERSION,
        "publisher": "Gexis Player",
        "email": "noreply@github.com",
        "website": "https://github.com/gcarstoiu/gexis-roon",
    }


class _Quiet(logging.Filter):
    """The library logs whole messages from Roon - titles, zone names - when
    something goes wrong. Only what went wrong is kept."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = str(record.msg).split("%", 1)[0].split(":", 1)[0].strip(" '")
        record.args = ()
        record.exc_info = record.exc_text = None
        return True


def quieten() -> None:
    """After the library is imported: its import sets its logger up."""
    library = logging.getLogger("roonapi")
    library.handlers.clear()  # its own handler would log every line twice
    library.setLevel(logging.WARNING)
    if not any(isinstance(f, _Quiet) for f in library.filters):
        library.addFilter(_Quiet())
    logging.getLogger("websocket").setLevel(logging.WARNING)


def load(path: Path = STATE) -> dict:
    try:
        saved = json.loads(path.read_text())
        return saved if isinstance(saved, dict) else {}
    except (OSError, ValueError):
        return {}


def save(saved: dict, path: Path = STATE) -> None:
    try:
        new = path.with_suffix(".new")
        new.write_text(json.dumps(saved))
        os.chmod(new, 0o600)
        os.replace(new, path)
    except OSError as exc:
        logger.warning("could not keep Roon's token: %s", exc)


def api_class():
    """`RoonApi` as this plugin needs it. Imported here, so that a player
    without the library still plays Roon, with nothing shown."""
    from roonapi import RoonApi

    class Api(RoonApi):
        def __init__(self, appinfo, token, host, port, changed) -> None:
            # The library keeps these on the class: shared by every
            # instance, and the next core would inherit the last one's.
            self._zones, self._outputs = {}, {}
            self._volume_controls = {}
            self._state_callbacks = [(lambda event, ids: changed(self), [], [])]
            super().__init__(appinfo, token, host, port, blocking_init=False)

        def _on_state_change(self, msg):
            # Roon sends a changed zone whole; the library merges it into
            # the one it had, so a field Roon left out - what played, at the
            # end of the queue - stayed.
            if isinstance(msg, dict):
                for key in ("zones", "zones_added", "zones_changed"):
                    for zone in msg.get(key) or ():
                        if isinstance(zone, dict):
                            self._zones.pop(zone.get("zone_id"), None)
            super()._on_state_change(msg)

    return Api


def discover(core_id: str | None) -> tuple[str, int] | None:
    from roonapi import RoonDiscovery

    found = RoonDiscovery(core_id).all()
    return found[0] if found else None


class Link:
    """The extension, connected to one Roon core at a time."""

    def __init__(self, loop: asyncio.AbstractEventLoop, on_zones, path: Path = STATE,
                 host: str | None = None) -> None:
        self._loop = loop
        self._on_zones = on_zones
        self._path = path
        self.saved = load(path)
        self._host = host or socket.gethostname()
        self._api = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="roon", daemon=True)

    @property
    def remembered(self) -> list[str]:
        return list(self.saved.get("outputs") or [])

    def remember(self, outputs) -> None:
        """This player's output, as `zone.py` found it, kept for the next start."""
        self._loop.run_in_executor(None, self._keep, {"outputs": sorted(outputs or ())})

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._api is not None:
            self._api.stop()

    # --- commands, from the event loop --------------------------------------

    async def control(self, zone_id: str, command: str) -> bool:
        return await self._ask(lambda api: api.playback_control(zone_id, command))

    async def seek(self, zone_id: str, seconds: float) -> bool:
        return await self._ask(lambda api: api.seek(zone_id, int(seconds)))

    async def _ask(self, call) -> bool:
        api = self._api
        if api is None or not api.ready:
            return False
        result = await asyncio.to_thread(call, api)
        return isinstance(result, str) and "Success" in result

    def image(self, key: str, size: int) -> str | None:
        api = self._api
        if api is None or not key:
            return None
        return api.get_image(key, scale="fit", width=size, height=size)

    # --- the library's threads ----------------------------------------------

    def _keep(self, fields: dict) -> None:
        with self._lock:
            if any(self.saved.get(k) != v for k, v in fields.items()):
                self.saved = {**self.saved, **fields}
                save(self.saved, self._path)

    def _changed(self, api) -> None:
        """On the library's socket thread, which is also the one that changes
        the zones: copied here, whole, before the loop reads them."""
        try:
            zones = copy.deepcopy(api.zones)
        except RuntimeError:  # changed meanwhile: the change's own call follows
            return
        self._loop.call_soon_threadsafe(self._on_zones, zones)

    def _run(self) -> None:
        try:
            Api = api_class()
        except ImportError:
            logger.warning("python3-roonapi is not installed: Roon plays, with nothing shown")
            return
        quieten()
        misses = 0
        while not self._stop.is_set():
            wanted = self.saved.get("core_id") if misses < ANY_CORE_AFTER else None
            try:
                core = discover(wanted)
            except OSError as exc:
                logger.info("cannot look for Roon on the network: %s", exc)
                core = None
            if core is None:
                misses += 1
                self._stop.wait(10)
                continue
            misses = 0
            logger.info("Roon found; enable Gexis Player (%s) in Roon's Settings → Extensions "
                        "if it is not already", self._host)
            self._api = api = Api(appinfo(self._host), self.saved.get("token"), *core, self._changed)
            self._changed(api)
            away_since = None
            while not self._stop.wait(2):
                if api.ready and api.token:
                    if api.core_id != self.saved.get("core_id"):
                        logger.info("Roon enabled the extension")
                        self._keep({"core_id": api.core_id, "outputs": []})
                    self._keep({"token": api.token})
                socket_ = api._roonsocket
                if socket_ is not None and socket_.connected:
                    away_since = None
                elif away_since is None:
                    away_since = time.monotonic()
                elif time.monotonic() - away_since > REDISCOVER_S:
                    logger.info("Roon out of reach; looking for it again")
                    break
            self._api = None
            api.stop()
            self._loop.call_soon_threadsafe(self._on_zones, {})
