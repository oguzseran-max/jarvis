# Whisper on the new (Apple Silicon) Mac — fast AND fluent French

On the current **Intel** Mac, Whisper runs on the CPU via faster-whisper
(CTranslate2), which has **no GPU path on macOS**. That's why big, French-strong
models (`large-v3-turbo`, a French fine-tune) are accurate but ~2.5–3× slower
than real time — you have to choose speed *or* French quality.

On an **Apple Silicon** Mac that wall disappears. [MLX](https://github.com/ml-explore/mlx)
runs Whisper on the GPU + Neural Engine, so `large-v3-turbo` transcribes at
roughly real time. You get the accurate French model **without** the latency.

`whisper_service.py` is already backend-pluggable (`WHISPER_BACKEND`), so the
switch is a dependency install + a couple of env vars — no code changes.

## Steps on the new Mac

1. **Confirm it's Apple Silicon**

   ```sh
   uname -m            # arm64  → Apple Silicon (good).  x86_64 → still Intel.
   ```

2. **Install mlx-whisper into the whisper venv**

   ```sh
   cd ~/jarvis
   ./whisper-venv/bin/pip install mlx-whisper
   ```

   (If the venv was built on Intel, recreate it on the new Mac first:
   `python3.12 -m venv whisper-venv && ./whisper-venv/bin/pip install -r whisper-requirements.txt mlx-whisper`.)

3. **Point the service at the MLX backend** — add to `.env`:

   ```sh
   WHISPER_BACKEND=mlx
   WHISPER_MODEL=large-v3-turbo
   # optional: a French-specialised model (see below)
   # WHISPER_MLX_REPO=mlx-community/whisper-large-v3-turbo
   ```

4. **Restart the Whisper service**

   ```sh
   pkill -f whisper_service.py
   ./whisper-venv/bin/python whisper_service.py
   ```

   The log should read `backend=mlx repo='…' (Apple Silicon GPU/ANE)`. First run
   downloads + caches the MLX weights.

5. **Test latency + French**

   ```sh
   say -v Thomas "Marion mets ta tenue de pluie à Veigy-Foncenex" \
     --file-format=WAVE --data-format=LEI16@16000 -o /tmp/t.wav
   curl -s -X POST "http://localhost:8765/transcribe?lang=fr" \
     --data-binary @/tmp/t.wav -w "\n[%{time_total}s]\n"
   rm /tmp/t.wav
   ```

   Expect a ~real-time figure (vs ~10 s on Intel CPU) with clean French.

## Going for maximum French (optional)

`large-v3-turbo` is already much better in French than `small`. For the absolute
best, use a **French fine-tune**:

- Look for an MLX build, e.g. on the Hub: `mlx-community` whisper French models,
  or convert `bofenghuang/whisper-large-v3-french` with `mlx_whisper.convert`.
- Set `WHISPER_MLX_REPO=<that repo>` (keep `WHISPER_BACKEND=mlx`). `WHISPER_MODEL`
  is then only used for the faster-whisper fallback.

## Safety / fallback

- `WHISPER_BACKEND` defaults to `faster` — nothing changes unless you opt in.
- If `WHISPER_BACKEND=mlx` is set but `mlx_whisper` can't import (e.g. still on
  Intel), the service logs a warning and **falls back to faster-whisper**, so it
  never goes deaf.
- The decoder primer + the learned-vocab layer (`data/whisper_vocab_fr.txt`) and
  the confidence filter work identically on both backends.

## Reverting

Remove `WHISPER_BACKEND=mlx` from `.env` (or set it to `faster`) and restart.
