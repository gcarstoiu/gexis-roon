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

In Roon, the player appears under Settings → Audio as a Roon Bridge device
with one output, the loopback: enable it, and name the zone as you like.

Not yet: what plays and its controls on the player's screen and phone,
through Roon's extension API.

## Tests

    python3 -m venv .venv && .venv/bin/pip install --no-cache-dir -e '.[test]'
    .venv/bin/python -m pytest

## Licence

GPL-3.0-or-later. Roon and Roon Bridge are Roon Labs' and under Roon's own
terms.
