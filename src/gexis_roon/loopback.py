# SPDX-License-Identifier: GPL-3.0-or-later
"""**Roon's audio, from its loopback into the player's output** (ADR-0172 in
gexis-player).

Roon Bridge sees one sound card, `Roon`, an `snd-aloop` loopback, and plays
into its device 0. What it plays comes out of device 1's capture side, at the
format, rate and channels it was played at. This module reads device 0's
state in /proc, and copies device 1 into the player's output (`output`, the
ALSA device every renderer plays into) with `arecord | aplay`, raw and
unchanged: with the player's equaliser off, what reaches the DAC is what Roon
sent.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("gexis_roon.loopback")

CARD = "Roon"
PROC = Path("/proc/asound")
#: The player's output, as every renderer plays into it.
OUTPUT = "output"


@dataclass(frozen=True)
class Stream:
    """What Roon plays: the loopback's capture side must be opened alike."""

    format: str
    rate: int
    channels: int


def read(proc: Path = PROC, card: str = CARD) -> tuple[str, Stream | None]:
    """Device 0's state - `absent` (no such card), `closed`, or ALSA's own in
    lower case (`running`, `prepared`, `paused`, `draining`, ...) - and, once
    its parameters are set, the stream."""
    base = proc / card / "pcm0p" / "sub0"
    try:
        status = (base / "status").read_text()
    except OSError:
        return "absent", None
    if status.strip() == "closed":
        return "closed", None
    found = re.search(r"^state:\s*(\S+)", status, re.M)
    state = found.group(1).lower() if found else "unknown"
    try:
        params = (base / "hw_params").read_text()
    except OSError:
        return state, None
    return state, parse_params(params)


def parse_params(text: str) -> Stream | None:
    fields = dict(re.findall(r"^(\w+):\s*(\S+)", text, re.M))
    try:
        return Stream(fields["format"], int(fields["rate"]), int(fields["channels"]))
    except (KeyError, ValueError):
        return None


class Copier:
    """`arecord` from the loopback's capture side, `aplay` into the output,
    joined by a pipe; started for one stream, stopped whole."""

    def __init__(self, source: str = f"hw:{CARD},1,0", output: str = OUTPUT,
                 spawn=asyncio.create_subprocess_exec) -> None:
        self.source = source
        self.output = output
        self._spawn = spawn
        self._procs: list = []
        self.stream: Stream | None = None

    @property
    def running(self) -> bool:
        return bool(self._procs) and all(p.returncode is None for p in self._procs)

    def args(self, stream: Stream) -> tuple[list[str], list[str]]:
        common = ["-q", "-t", "raw", "-f", stream.format, "-r", str(stream.rate), "-c", str(stream.channels)]
        return (["arecord", "-D", self.source, *common], ["aplay", "-D", self.output, *common])

    async def start(self, stream: Stream) -> None:
        await self.stop()
        record, play = self.args(stream)
        read_end, write_end = os.pipe()
        try:
            reader = await self._spawn(*record, stdout=write_end, stderr=asyncio.subprocess.PIPE)
            player = await self._spawn(*play, stdin=read_end, stderr=asyncio.subprocess.PIPE)
        finally:
            os.close(read_end)
            os.close(write_end)
        self._procs = [reader, player]
        self.stream = stream
        logger.info("copying %s, %d Hz, %d channels into %s", stream.format, stream.rate, stream.channels, self.output)

    async def stop(self) -> None:
        procs, self._procs, self.stream = self._procs, [], None
        for proc in procs:
            if proc.returncode is None:
                try:
                    proc.terminate()
                except ProcessLookupError:
                    pass
        for proc in procs:
            try:
                await asyncio.wait_for(proc.wait(), 2.0)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
        if procs:
            logger.info("stopped copying")
