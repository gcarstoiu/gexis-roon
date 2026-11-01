# SPDX-License-Identifier: GPL-3.0-or-later
"""**The Gexis plugin contract, from the plugin's side** (`docs/PLUGIN-CONTRACT.md`
in gexis-player, version 1), as gexis-plexamp and gexis-airplay speak it.

A Unix socket, one JSON object per line. The core listens; we connect, and
reconnect for as long as we are running.
"""
from __future__ import annotations

import asyncio
import json
import logging

logger = logging.getLogger("gexis_roon.contract")

DEFAULT_SOCKET = "/run/gexis/plugins.sock"
CONTRACT = 1

#: How long to wait before reconnecting: the core restarts on an upgrade, and
#: a plugin that gave up would be a renderer that silently stopped existing.
RECONNECT_S = 3.0


class Refused(Exception):
    """The core said no, and said why. Not retried by reconnecting: a
    contract mismatch or a missing manifest is the same on the next try."""


class Core:
    """One connection to the core.

    `on_command` is called with `(t, message)` for every command and returns
    the value for `ok.result`, or raises to send `error`. It may be a
    coroutine function.
    """

    def __init__(self, path: str = DEFAULT_SOCKET) -> None:
        self.path = path
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self.settings: dict = {}

    @property
    def connected(self) -> bool:
        return self._writer is not None

    async def connect(self, hello: dict) -> None:
        reader, writer = await asyncio.open_unix_connection(self.path, limit=2 ** 21)
        self._reader, self._writer = reader, writer
        await self.send({"t": "hello", "contract": CONTRACT, **hello})
        line = await reader.readline()
        if not line:
            raise Refused("the core closed the connection without answering")
        answer = json.loads(line)
        if answer.get("t") == "refused":
            raise Refused(answer.get("reason", "no reason given"))
        if answer.get("t") != "welcome":
            raise Refused(f"expected welcome, got {answer.get('t')!r}")
        self.settings = answer.get("settings") or {}
        logger.info("connected; the core holds %d setting(s) for us", len(self.settings))

    async def send(self, message: dict) -> None:
        if self._writer is None:
            raise ConnectionError("not connected")
        self._writer.write((json.dumps(message, separators=(",", ":")) + "\n").encode())
        await self._writer.drain()

    async def event(self, t: str, **fields) -> None:
        """One of the things a plugin may say unprompted. Dropped, and
        logged once by the caller's reconnect, while not connected."""
        await self.send({"t": t, **fields})

    async def serve(self, on_command) -> None:
        """Answer commands until the connection ends: exactly once per `id`,
        in order, each before the next is read."""
        assert self._reader is not None
        while True:
            line = await self._reader.readline()
            if not line:
                logger.info("the core closed the connection")
                return
            try:
                message = json.loads(line)
            except ValueError:
                logger.warning("ignoring a line that was not JSON")
                continue
            ident = message.get("id")
            if ident is None:
                logger.debug("ignoring %r, which carries no id", message.get("t"))
                continue
            try:
                result = on_command(message.get("t"), message)
                if asyncio.iscoroutine(result):
                    result = await result
                await self.send({"t": "ok", "id": ident, "result": result})
            except Exception as exc:  # noqa: BLE001 - a refusal is ours to report
                logger.warning("%s failed: %s", message.get("t"), exc)
                await self.send({"t": "error", "id": ident, "message": str(exc)})

    async def close(self) -> None:
        if self._writer is not None:
            self._writer.close()
            self._writer = None
            self._reader = None
