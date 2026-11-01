from pathlib import Path

from gexis_roon import loopback


def _card(tmp_path: Path, status: str, params: str = "closed") -> Path:
    sub = tmp_path / "card5" / "pcm0p" / "sub0"
    sub.mkdir(parents=True)
    (sub / "status").write_text(status)
    (sub / "hw_params").write_text(params)
    (tmp_path / "Roon").symlink_to("card5")
    return tmp_path


PARAMS = """access: MMAP_INTERLEAVED
format: S32_LE
subformat: STD
channels: 2
rate: 96000 (96000/1)
period_size: 4800
buffer_size: 19200
"""


def test_no_card_is_absent(tmp_path):
    assert loopback.read(tmp_path) == ("absent", None)


def test_closed(tmp_path):
    assert loopback.read(_card(tmp_path, "closed\n")) == ("closed", None)


def test_running_with_its_stream(tmp_path):
    proc = _card(tmp_path, "state: RUNNING\nowner_pid   : 1234\n", PARAMS)
    assert loopback.read(proc) == ("running", loopback.Stream("S32_LE", 96000, 2))


def test_the_copy_is_raw_and_alike_on_both_sides():
    record, play = loopback.Copier().args(loopback.Stream("S24_3LE", 44100, 2))
    assert record[:3] == ["arecord", "-D", "hw:Roon,1,0"] and play[:3] == ["aplay", "-D", "output"]
    assert record[3:] == play[3:] == ["-q", "-t", "raw", "-f", "S24_3LE", "-r", "44100", "-c", "2"]


class FakeProc:
    def __init__(self):
        self.returncode = None
        self.terminated = False

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def kill(self):
        self.returncode = -9

    async def wait(self):
        return self.returncode


async def test_the_copier_starts_two_and_stops_both():
    spawned = []

    async def spawn(*args, **kwargs):
        spawned.append(args)
        return FakeProc()
    copier = loopback.Copier(spawn=spawn)
    stream = loopback.Stream("S16_LE", 48000, 2)
    await copier.start(stream)
    assert copier.running and copier.stream == stream and [a[0] for a in spawned] == ["arecord", "aplay"]
    await copier.stop()
    assert not copier.running and copier.stream is None
