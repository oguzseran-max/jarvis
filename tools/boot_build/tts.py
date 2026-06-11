#!/usr/bin/env python3
"""Generate the boot welcome voice lines via Fish Audio TTS.

One file per language x time-of-day. The first sentence is unchanged; the
second sentence is the morning (coffee) or evening (wine) variant. Voices match
the originals so the re-synthesised line blends with the separated music bed.
"""
import json
import os
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent / "voice"
OUT.mkdir(exist_ok=True)

# Load FISH_API_KEY from .env
KEY = None
for line in (ROOT / ".env").read_text().splitlines():
    if line.startswith("FISH_API_KEY="):
        KEY = line.split("=", 1)[1].strip()
if not KEY:
    sys.exit("FISH_API_KEY not found in .env")

# voice id + optional model header (matches server.py _LANG_VOICE)
VOICE = {
    "en": ("612b878b113047d9a770c069c8b4fdfe", None),
    "fr": ("1d24b6b70ea8400b9d19a21ecca65be3", "s1"),  # Marion (consented)
    "tr": ("afeae5154fba4128884b2d291c5b7b68", "s1")  # Sevgi (consented),
}

FIRST = {
    "en": "Welcome back, sir. All systems will be ready in a few minutes.",
    "fr": "Bon retour, mon amour. Tous les systèmes seront prêts dans quelques minutes.",
    "tr": "Tekrar hoş geldin canım. Tüm sistemler birkaç dakika içinde hazır olacak.",
}
SECOND = {
    "en": {
        "morning": "In the meantime, grab a cup of coffee and relax.",
        "evening": "In the meantime, pour yourself a nice glass of wine and enjoy the terrace.",
    },
    "fr": {
        "morning": "En attendant, prends une tasse de café et détends-toi.",
        "evening": "En attendant, sers-toi un bon verre de vin et profite de la terrasse.",
    },
    "tr": {
        "morning": "Bu arada, kendine bir fincan kahve al ve rahatla.",
        "evening": "Bu arada, kendine güzel bir kadeh şarap koy ve terasın keyfini çıkar.",
    },
}


def synth(text: str, voice_id: str, model: str | None, dest: Path) -> None:
    body = json.dumps({"text": text, "reference_id": voice_id, "format": "mp3"}).encode()
    req = urllib.request.Request(
        "https://api.fish.audio/v1/tts", data=body, method="POST",
        headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
    )
    if model:
        req.add_header("model", model)
    with urllib.request.urlopen(req, timeout=60) as r:
        data = r.read()
    dest.write_bytes(data)
    print(f"  wrote {dest.name} ({len(data)} bytes)")


for lang, (vid, model) in VOICE.items():
    for tod in ("morning", "evening"):
        text = FIRST[lang] + " " + SECOND[lang][tod]
        dest = OUT / f"voice_{lang}_{tod}.mp3"
        print(f"[{lang}/{tod}] {text}")
        synth(text, vid, model, dest)

print("done")
