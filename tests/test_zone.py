from gexis_roon.zone import SETTLE_S, ZoneFinder


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def zone(state, *outputs):
    return {"state": state, "outputs": [{"output_id": o} for o in outputs]}


def settled(finder, clock):
    clock.now += SETTLE_S
    return finder.check()


def test_the_one_zone_playing_with_the_loopback_is_this_player_s():
    clock = Clock()
    finder = ZoneFinder(clock=clock)
    finder.zones({"kitchen": zone("stopped", "k"), "here": zone("playing", "h")})
    finder.loopback(True)
    assert not finder.check(), "not before both have held still"
    assert settled(finder, clock)
    assert finder.outputs == {"h"} and finder.zone_id() == "here"


def test_two_playing_wait_for_one_to_stop():
    clock = Clock()
    finder = ZoneFinder(clock=clock)
    finder.zones({"kitchen": zone("playing", "k"), "here": zone("playing", "h")})
    finder.loopback(True)
    settled(finder, clock)
    assert finder.outputs == {"k", "h"} and finder.zone_id() is None
    finder.zones({"kitchen": zone("paused", "k"), "here": zone("playing", "h")})
    settled(finder, clock)
    assert finder.zone_id() == "here"


def test_the_loopback_closed_rules_out_the_zones_that_play():
    clock = Clock()
    finder = ZoneFinder(clock=clock)
    finder.zones({"kitchen": zone("playing", "k"), "here": zone("playing", "h")})
    finder.loopback(True)
    settled(finder, clock)
    finder.zones({"kitchen": zone("playing", "k"), "here": zone("stopped", "h")})
    finder.loopback(False)
    settled(finder, clock)
    assert finder.zone_id() == "here"


def test_a_zone_still_loading_is_not_held_against_it():
    clock = Clock()
    finder = ZoneFinder({"k", "h"}, clock=clock)
    finder.zones({"kitchen": zone("stopped", "k"), "here": zone("loading", "h")})
    settled(finder, clock)
    assert finder.outputs == {"k", "h"}


def test_a_group_is_the_zone_it_plays_in():
    clock = Clock()
    finder = ZoneFinder(clock=clock)
    finder.zones({"both": zone("playing", "k", "h")})
    finder.loopback(True)
    settled(finder, clock)
    assert finder.outputs == {"k", "h"} and finder.zone_id() == "both"


def test_a_remembered_output_holds_through_one_odd_play_and_goes_after_two():
    clock = Clock()
    finder = ZoneFinder({"old"}, clock=clock)
    finder.zones({"other": zone("stopped", "o"), "here": zone("stopped", "old")})
    settled(finder, clock)
    finder.zones({"other": zone("playing", "o"), "here": zone("paused", "old")})
    finder.loopback(True)
    settled(finder, clock)
    assert finder.outputs == {"old"}, "one play is not enough"
    finder.loopback(False)
    settled(finder, clock)
    finder.loopback(True)
    settled(finder, clock)
    assert finder.outputs == {"o"}


def test_an_output_roon_no_longer_lists_is_forgotten():
    clock = Clock()
    finder = ZoneFinder({"gone"}, clock=clock)
    finder.zones({"here": zone("playing", "h")})
    finder.loopback(True)
    settled(finder, clock)
    assert finder.outputs == {"h"}


def test_a_track_change_is_not_a_reading():
    clock = Clock()
    finder = ZoneFinder(clock=clock)
    finder.zones({"here": zone("playing", "h")})
    finder.loopback(True)
    clock.now += 1
    finder.loopback(False)
    clock.now += 0.5
    finder.loopback(True)
    clock.now += SETTLE_S - 1
    assert not finder.check()
