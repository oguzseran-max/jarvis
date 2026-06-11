# Boot audio build

Regenerates the time-of-day boot tracks: `frontend/public/boot_audio_{en,fr,tr}_{morning,evening}.mp3`.

The original `boot_audio_{lang}.mp3` files mix voice + music. We keep the original
music untouched and only re-synthesise the spoken welcome line so the second
sentence can be morning (coffee) or evening (wine).

Pipeline:

1. **Isolate the music bed** from each original with Demucs (vocals removed):
   ```sh
   ../../.audio-venv/bin/python -m demucs --two-stems=vocals -o sep \
     ../../frontend/public/boot_audio_en.mp3 \
     ../../frontend/public/boot_audio_fr.mp3 \
     ../../frontend/public/boot_audio_tr.mp3
   # -> sep/htdemucs/<name>/no_vocals.wav
   ```
   (`.audio-venv` is a Python 3.12 venv: `demucs`, `torch`, `numpy<2`, `soundfile`.
   Torch 2.2 needs numpy 1.x.)

2. **Synthesise the voice lines** (Fish Audio, same voices as `server.py`):
   ```sh
   ../../venv/bin/python tts.py   # -> voice/voice_<lang>_<tod>.mp3
   ```
   Edit `FIRST` / `SECOND` in `tts.py` to change wording.

3. **Mix** voice (delayed 0.8s) over each bed, normalise, export 28s mp3 — see the
   ffmpeg `amix=...:normalize=0,alimiter` loop used to produce the public files.

The frontend (`frontend/src/main.ts`) picks morning vs evening at boot
(`timeOfDay()`, 05:00–17:59 = morning) and feeds the matching subtitle to the
cinematic HUD (`frontend/src/boot.ts`).

`sep/`, `voice/`, `shot_*.png` and `.audio-venv/` are gitignored build artifacts.
