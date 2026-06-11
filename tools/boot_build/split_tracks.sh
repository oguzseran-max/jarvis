#!/bin/bash
# Build separate music-only (looping) and voice-only boot tracks.
set -e
cd "$(dirname "$0")/../.."
SEP=tools/boot_build/sep/htdemucs
VOICE=tools/boot_build/voice
OUT=frontend/public

# Music: a single user-provided score (assets/boot_music.mp3) — audio only,
# loudness-normalised, gentle fade-in. It loops forever and fades to a faint
# ambient level once the orb takes over (handled in main.ts). Falls back to the
# separated music bed if no external track is supplied.
if [ -f assets/boot_music.mp3 ]; then
  ffmpeg -y -loglevel error -i assets/boot_music.mp3 \
    -map 0:a:0 -af "loudnorm=I=-15:TP=-1.5:LRA=11,afade=t=in:st=0:d=0.4" \
    -ar 44100 -ac 2 -c:a libmp3lame -q:a 2 "$OUT/boot_music.mp3"
else
  ffmpeg -y -loglevel error -i "$SEP/boot_audio_en/no_vocals.wav" \
    -c:a libmp3lame -q:a 3 "$OUT/boot_music.mp3"
fi

for lang in en fr tr; do
  for tod in morning evening; do
    ffmpeg -y -loglevel error -i "$VOICE/voice_${lang}_${tod}.mp3" \
      -af "adelay=800:all=1,volume=2.4,alimiter=limit=0.97" \
      -c:a libmp3lame -q:a 3 "$OUT/boot_voice_${lang}_${tod}.mp3"
  done
done

# Align the HUD climax to a musical hit in the (new) music → boot_cue.json
if [ -x .audio-venv/bin/python ]; then
  .audio-venv/bin/python tools/boot_build/analyze_beats.py || true
fi

echo "generated music + voice tracks"
ls -1 "$OUT"/boot_music.mp3 "$OUT"/boot_voice_*.mp3 "$OUT"/boot_cue.json 2>/dev/null
