#!/usr/bin/env python3
"""Find a strong musical hit near the boot climax and emit boot_cue.json.

Lightweight spectral-flux onset detection (numpy only). We scan the first ~28s
of the boot music and pick the strongest onset inside a target window, then
place the HUD burst on it; the handoff + end follow a few beats later. The
frontend fetches boot_cue.json and aligns the climax to it.
"""
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "frontend/public/boot_music.mp3"
OUT = ROOT / "frontend/public/boot_cue.json"

# Burst search window (s). The handoff is placed on the next strong beat after
# the burst (searched in HANDOFF_WIN seconds relative to it); end trails the
# handoff so the music keeps playing through the dissolve.
WIN = (16.5, 21.0)
HANDOFF_WIN = (2.2, 5.5)
HANDOFF_FALLBACK = 3.3
END_AFTER_HANDOFF = 5.2
DEFAULT_BURST = 19.0

SR = 22050
N_FFT = 1024
HOP = 512


def decode(path: Path) -> np.ndarray:
    """Decode the first 30s to mono float32 @ SR via ffmpeg → soundfile."""
    tmp = path.with_suffix(".cue.wav")
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(path),
         "-t", "30", "-ac", "1", "-ar", str(SR), str(tmp)],
        check=True)
    y, _ = sf.read(str(tmp))
    tmp.unlink(missing_ok=True)
    return y.astype(np.float32)


def onset_envelope(y: np.ndarray):
    win = np.hanning(N_FFT).astype(np.float32)
    frames = 1 + max(0, (len(y) - N_FFT) // HOP)
    flux = np.zeros(frames, dtype=np.float32)
    prev = np.zeros(N_FFT // 2 + 1, dtype=np.float32)
    for i in range(frames):
        seg = y[i * HOP: i * HOP + N_FFT]
        if len(seg) < N_FFT:
            seg = np.pad(seg, (0, N_FFT - len(seg)))
        mag = np.abs(np.fft.rfft(seg * win))
        flux[i] = np.sum(np.maximum(0.0, mag - prev))
        prev = mag
    times = np.arange(frames) * HOP / SR
    # smooth a touch
    if frames >= 5:
        k = np.ones(5, dtype=np.float32) / 5
        flux = np.convolve(flux, k, mode="same")
    return times, flux


def pick_peaks(times, flux):
    """Local maxima of the onset envelope above a strength threshold."""
    if len(flux) < 3:
        return []
    radius = max(1, int(0.12 * SR / HOP))  # ~120ms neighbourhood
    thresh = float(flux.mean() + 0.5 * flux.std())
    peaks = []
    for i in range(len(flux)):
        lo, hi = max(0, i - radius), min(len(flux), i + radius + 1)
        if flux[i] >= flux[lo:hi].max() and flux[i] >= thresh:
            peaks.append((float(times[i]), float(flux[i])))
    return peaks


def main():
    burst = DEFAULT_BURST
    handoff = None
    try:
        y = decode(SRC)
        times, flux = onset_envelope(y)
        mask = (times >= WIN[0]) & (times <= WIN[1])
        if mask.any():
            idx = np.argmax(flux[mask])
            burst = float(times[mask][idx])
        # handoff = strongest beat in [burst+2.2, burst+5.5]
        peaks = pick_peaks(times, flux)
        cands = [(s, tm) for (tm, s) in peaks
                 if burst + HANDOFF_WIN[0] <= tm <= burst + HANDOFF_WIN[1]]
        if cands:
            handoff = max(cands)[1]
    except Exception as e:  # fall back to the default choreography
        print(f"beat analysis failed ({e}); using default", file=sys.stderr)

    if handoff is None:
        handoff = burst + HANDOFF_FALLBACK
    cue = {
        "burst": round(burst, 2),
        "handoff": round(handoff, 2),
        "end": round(handoff + END_AFTER_HANDOFF, 2),
    }
    OUT.write_text(json.dumps(cue))
    print("wrote", OUT, cue)


if __name__ == "__main__":
    main()
