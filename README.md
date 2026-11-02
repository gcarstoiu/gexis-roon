# gexis-roon

Roon as a [Gexis Player](https://github.com/gcarstoiu/gexis-player) plugin:
the player becomes a Roon device, and what Roon plays goes through the
player's own output - its equaliser, its multiroom groups, and its turns with
the other sources - unchanged when the equaliser is off.

How it fits together (Gexis Player's ADR-0172):

- **Roon Bridge**, Roon Labs' endpoint software, is not part of this
  repository or of Gexis Player: the player downloads it from Roon Labs when
  Roon is added, and it keeps itself up to date. It runs as a user of its
  own and sees one sound card, `Roon`, a loopback.
- **This plugin** watches that loopback. When Roon starts playing, it asks
  the player for its output (`acquire`, over the plugin contract,
  `docs/PLUGIN-CONTRACT.md` there, version 1) and, once given it, copies the
  loopback into the output with `arecord | aplay`, raw, at the format and
  rate Roon plays. A pause keeps the output; ten seconds after Roon lets the
  loopback go, it is given back. When another source takes the output, the
  copy stops, and Roon is followed again when it plays anew.

- **Roon's extension API** (Debian's `python3-roonapi`) gives the player
  what plays in its zone - title, artist, album, cover, position - and its
  play, pause, next, previous and seek. The plugin finds its zone itself:
  the zone that plays while the loopback plays, and not while it does not,
  is this player's, and it is remembered. When another source takes the
  output, Roon's zone is paused.

In Roon, the player appears under Settings → Audio as a Roon Bridge device
with two outputs named *Loopback PCM*: enable the top one, and name the zone
as you like. Then, under
Settings → Extensions, enable *Gexis Player* with the player's name: once
Roon has played to the player, its Now Playing is Roon's. Without the
extension, Roon still plays, and the player says only that Roon does.

## Tests

    python3 -m venv .venv && .venv/bin/pip install --no-cache-dir -e '.[test]'
    .venv/bin/python -m pytest

The tests that need Roon's library are skipped without it; with Debian's
`python3-roonapi` (or `roonapi` 0.1.6 and its dependencies) importable, they
run against it.

## Licence

GPL-3.0-or-later. Roon and Roon Bridge are Roon Labs' and under Roon's own
terms.
