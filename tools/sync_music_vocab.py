#!/usr/bin/env python3
"""Sync the user's Spotify top artists + tracks into the Whisper music primer.

It pulls the artists/tracks he actually listens to and writes them into
`data/music_vocab_fr.txt` and `data/music_vocab_en.txt`, so the local Whisper
service transcribes music requests ("mets du …", "play …") right the first time.

Idempotent: everything above the SENTINEL line in each file is the hand-curated
seed and is preserved; everything below is regenerated on each run.

Usage (from the repo root, with the project venv):
    ./venv/bin/python tools/sync_music_vocab.py

Needs the `user-top-read` Spotify scope. If you authorised before that scope was
added, re-run the Spotify auth flow once:  ./venv/bin/python spotify_access.py --auth
"""
import asyncio
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import spotify_access  # noqa: E402

_DATA = _ROOT / "data"
SENTINEL = "# === auto-synced from Spotify (regenerated each run — do not edit below) ==="

# Keep the primer short: Whisper echoes long initial prompts back on silence.
MAX_ARTISTS = 22
MAX_TRACKS = 8


def _curated_head(path: Path) -> tuple[list[str], set[str]]:
    """Return (lines above the sentinel, lowercased entry names already present)."""
    head: list[str] = []
    seen: set[str] = set()
    if path.exists():
        for line in path.read_text().splitlines():
            if line.strip() == SENTINEL:
                break
            head.append(line)
            s = line.strip()
            if s and not s.startswith("#"):
                for part in s.split(","):
                    seen.add(part.strip().lower())
    return head, seen


def _write(path: Path, synced: list[str]) -> int:
    head, seen = _curated_head(path)
    fresh = []
    for name in synced:
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        fresh.append(name)
    body = head + [SENTINEL] + fresh
    # Trim a trailing blank line for tidiness, ensure newline at EOF.
    path.write_text("\n".join(body).rstrip() + "\n")
    return len(fresh)


async def main() -> int:
    if not spotify_access.is_configured():
        print("Spotify is not configured (.env SPOTIFY_* missing).", file=sys.stderr)
        return 1

    token = await spotify_access.get_access_token()
    if not token:
        print("Could not get a Spotify access token (check the refresh token).", file=sys.stderr)
        return 1

    # Blend recent + 6-month tastes so both current and staple artists are primed.
    short = await spotify_access.get_top_artists(limit=30, time_range="short_term", token=token)
    medium = await spotify_access.get_top_artists(limit=30, time_range="medium_term", token=token)
    tracks = await spotify_access.get_top_tracks(limit=20, time_range="medium_term", token=token)

    if not short and not medium and not tracks:
        print("Spotify returned nothing. If you just added the user-top-read scope, "
              "re-authorise:  ./venv/bin/python spotify_access.py --auth", file=sys.stderr)
        return 2

    # Dedup artists, recent first, capped.
    artists: list[str] = []
    seen: set[str] = set()
    for name in short + medium:
        k = name.lower()
        if k not in seen:
            seen.add(k)
            artists.append(name)
    artists = artists[:MAX_ARTISTS]

    # A few distinctive track titles (skip ones that are just the artist name).
    track_titles: list[str] = []
    tseen: set[str] = set()
    for t in tracks:
        title = t["name"]
        k = title.lower()
        if k in tseen or k in seen:
            continue
        tseen.add(k)
        track_titles.append(title)
    track_titles = track_titles[:MAX_TRACKS]

    synced = artists + track_titles
    n_fr = _write(_DATA / "music_vocab_fr.txt", synced)
    n_en = _write(_DATA / "music_vocab_en.txt", synced)
    print(f"Synced {len(artists)} artists + {len(track_titles)} tracks from Spotify.")
    print(f"  music_vocab_fr.txt: +{n_fr} new entries")
    print(f"  music_vocab_en.txt: +{n_en} new entries")
    print("Whisper picks this up live (no restart needed).")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
