#!/usr/bin/env python3
"""Whisper transcription microservice for JARVIS.

Runs under the dedicated Python 3.12 venv (whisper-venv), because faster-whisper
and its deps (av, ctranslate2, onnxruntime) have no Python 3.14 wheels. The main
JARVIS backend (3.14) calls this over localhost HTTP, one request per utterance.

Protocol:
  GET  /health                      -> {"status":"ok","model":...}
  POST /transcribe  (raw body)      -> {"text","language","probability"}
      Body: an encoded audio blob from the browser's MediaRecorder (Opus/WebM).
      faster-whisper decodes it via ffmpeg/av. Auto-detects the language; if it
      lands outside the allowed set it is clamped to English so JARVIS never
      replies in a language it has no voice for.
"""

import json
import os
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import numpy as np
from faster_whisper import WhisperModel
from faster_whisper.audio import decode_audio

MODEL_SIZE = os.getenv("WHISPER_MODEL", "small")
PORT = int(os.getenv("WHISPER_PORT", "8765"))
ALLOWED = {l.strip() for l in os.getenv("WHISPER_LANGS", "en,fr,tr").split(",") if l.strip()}
# Decode beam width. Greedy (beam=1) is faster, BUT on this room mic (music +
# ambient) it both garbled transcripts and lowered avg_logprob enough that the
# confidence filter below dropped real speech — Marion stopped "hearing" the user.
# Beam search finds higher-probability paths, so 5 is the reliable default for
# listening; the CPU_THREADS cap below recovers most of the speed. max(1, …) guards
# a misconfigured 0/negative. Override via WHISPER_BEAM_SIZE to trade speed back.
BEAM_SIZE = max(1, int(os.getenv("WHISPER_BEAM_SIZE", "5")))

# CPU thread count. ctranslate2 defaults to only 4 threads, so giving it the full
# core count helps — UP TO A POINT. Past ~8 threads an int8 model oversubscribes
# (cache thrash + contention with the server/TTS/browser sharing this machine) and
# transcription can run SLOWER than real-time — the pathological "slow transcription"
# tail (a sub-18s clip measured at 29s). Cap the default at 8; low-core machines are
# unaffected, and WHISPER_CPU_THREADS still overrides explicitly.
CPU_THREADS = int(os.getenv("WHISPER_CPU_THREADS", str(min(os.cpu_count() or 4, 8))))

# Transcription backend.
#   "faster" — faster-whisper / CTranslate2 on CPU. The default, fully tested,
#              and the only real option on Intel Macs.
#   "mlx"    — Apple MLX on Apple-Silicon GPU + Neural Engine. Runs large models
#              at ~real time, so on the new Mac you get large-v3-turbo / a French
#              fine-tune WITHOUT the CPU latency. Needs `pip install mlx-whisper`.
# If "mlx" is requested but not importable we fall back to faster-whisper so the
# service never goes deaf. Full migration steps: docs/whisper-apple-silicon.md
WHISPER_BACKEND = os.getenv("WHISPER_BACKEND", "faster").lower()
# HF repo for the MLX model. Defaults to the MLX community mirror of WHISPER_MODEL;
# point it at e.g. a French fine-tune via WHISPER_MLX_REPO.
_MLX_REPO = os.getenv("WHISPER_MLX_REPO", f"mlx-community/whisper-{MODEL_SIZE}")

_use_mlx = False
_mlx = None
if WHISPER_BACKEND == "mlx":
    try:
        import mlx_whisper as _mlx  # type: ignore
        _use_mlx = True
    except Exception as _e:
        print(f"[whisper] mlx backend requested but unavailable ({_e}); "
              f"falling back to faster-whisper", flush=True)

if _use_mlx:
    print(f"[whisper] backend=mlx repo='{_MLX_REPO}' (Apple Silicon GPU/ANE)", flush=True)
    model = None  # mlx loads + caches the weights on first transcribe
else:
    print(f"[whisper] loading model '{MODEL_SIZE}' (beam={BEAM_SIZE}, threads={CPU_THREADS}) …", flush=True)
    model = WhisperModel(MODEL_SIZE, device="cpu", compute_type="int8", cpu_threads=CPU_THREADS)
_lock = threading.Lock()  # faster-whisper isn't meant for concurrent calls

# Per-language decoder primers — short, in-domain vocabulary that nudges Whisper
# toward the household's proper nouns and habits (kept brief to avoid the model
# echoing these words into silence). English left unprimed.
_PRIMERS = {
    # Hand-curated French seed: household proper nouns + the exact command
    # phrasings that were being mis-heard (e.g. "tenue de pluie" -> "police").
    # Kept to ~2 sentences so Whisper doesn't echo it back on silence. Words the
    # user explicitly corrects are added on top of this by self_eval (the
    # whisper_vocab_fr.txt layer), so this only needs the stable household core.
    "fr": ("Conversation familière en français avec Marion, à Veigy-Foncenex, "
           "en Haute-Savoie près de Genève. "
           "mon amour, Leyla, Aylin, Oz, Spotify, Phil Collins, la musique, "
           "le salon, la mezzanine, la cuisine, le portail, la voiture, "
           "les lumières, la météo, la surveillance. "
           "mets-toi en bikini, mets ta tenue de pluie, ta tenue normale, "
           "suis la météo, ouvre le portail, allume les lumières."),
    "tr": "Türkçe sohbet. Marion, müzik, Spotify, ışıklar, kapı, hava durumu.",
}

# Two data-driven vocab layers are merged into the primer live (each re-read only
# when its file changes, so no restart is needed):
#   • whisper_vocab_<lang>.txt — proper nouns self_eval.py learns from the user's
#     corrections (so Marion stops mis-hearing words he's corrected).
#   • music_vocab_<lang>.txt    — artist / band / song names (a curated seed plus
#     whatever tools/sync_music_vocab.py pulls from his Spotify) so music requests
#     are transcribed right the first time. Also primes English (no base primer).
_VOCAB_DIR = Path(__file__).resolve().parent / "data"
_vocab_cache: dict = {}  # (lang, kind) -> (mtime, "word, word, ...")


def _read_vocab(lang, kind, limit=None):
    """Read data/<kind>_<lang>.txt, re-reading only when the file changes.

    Entries may be comma-separated and/or one per line — both normalise to a
    single ", "-joined primer string, so the curated seed files stay readable.
    `limit` caps how many entries are kept (the primer must stay short or Whisper
    starts echoing it back on silence).
    """
    try:
        path = _VOCAB_DIR / f"{kind}_{lang}.txt"
        mtime = path.stat().st_mtime
        key = (lang, kind, limit)
        cached = _vocab_cache.get(key)
        if not cached or cached[0] != mtime:
            raw = path.read_text()
            parts = [p.strip() for line in raw.splitlines() for p in line.split(",")]
            parts = [p for p in parts if p and not p.startswith("#")]
            if limit:
                parts = parts[:limit]
            text = ", ".join(parts)
            _vocab_cache[key] = (mtime, text)
            return text
        return cached[1]
    except Exception:
        return ""


def _primer_for(lang):
    base = _PRIMERS.get(lang)
    if not lang:
        return base
    extras = [v for v in (_read_vocab(lang, "whisper_vocab"),
                          _read_vocab(lang, "music_vocab", limit=35)) if v]
    if extras:
        return f"{base or ''} {', '.join(extras)}".strip()
    return base
print(f"[whisper] ready on :{PORT}  langs={sorted(ALLOWED)}  beam={BEAM_SIZE}", flush=True)

# Whisper hallucinates these training-data artifacts on silence/noise/music —
# discard any transcript that's essentially one of them so JARVIS never "replies"
# to a subtitle credit it imagined.
_HALLUCINATIONS = (
    "amara.org", "soustitreur", "sous-titres réalisés", "sous-titrage",
    "merci d'avoir regardé", "merci d’avoir regardé", "thanks for watching",
    "thank you for watching", "abonnez-vous", "subscribe", "♪",
)


def _is_hallucination(text: str) -> bool:
    t = text.lower().strip()
    if not t:
        return False
    return any(h in t for h in _HALLUCINATIONS)


class _MlxSeg:
    """Adapter so MLX segments expose the same attributes the confidence filter
    reads off faster-whisper segments (.text / .avg_logprob / .no_speech_prob)."""
    __slots__ = ("text", "avg_logprob", "no_speech_prob")

    def __init__(self, d):
        self.text = d.get("text", "")
        self.avg_logprob = d.get("avg_logprob", 0.0)
        self.no_speech_prob = d.get("no_speech_prob", 0.0)


class _MlxInfo:
    __slots__ = ("language", "language_probability")

    def __init__(self, language, prob):
        self.language = language
        self.language_probability = prob


def _decode(audio, forced):
    """Run the active backend, returning (segments, info) shaped like
    faster-whisper's so everything downstream (confidence filter, language,
    hallucination check) is backend-agnostic."""
    if _use_mlx:
        res = _mlx.transcribe(
            audio, path_or_hf_repo=_MLX_REPO, language=forced,
            initial_prompt=_primer_for(forced), no_speech_threshold=0.6,
            condition_on_previous_text=False, word_timestamps=False,
        )
        segs = [_MlxSeg(s) for s in res.get("segments", [])]
        lang = res.get("language") or forced or "en"
        return segs, _MlxInfo(lang, 1.0 if forced else 0.5)
    # faster-whisper (CPU) — the default, fully-tested path. Params are exactly
    # those tuned in place above; only the call site moved here for dispatch.
    return model.transcribe(
        audio, language=forced, beam_size=BEAM_SIZE, vad_filter=True,
        no_speech_threshold=0.6, compression_ratio_threshold=2.2,
        temperature=[0.0, 0.2], condition_on_previous_text=False,
        without_timestamps=True, initial_prompt=_primer_for(forced),
    )


class Handler(BaseHTTPRequestHandler):
    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._json(200, {"status": "ok", "model": MODEL_SIZE})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path != "/transcribe":
            self._json(404, {"error": "not found"})
            return
        # Optional ?lang=tr forces the language instead of auto-detecting.
        forced = parse_qs(parsed.query).get("lang", [None])[0]
        forced = forced if forced in ALLOWED else None
        n = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(n) if n else b""
        if len(raw) < 64:
            self._json(400, {"error": "empty audio"})
            return
        # Write the encoded blob to a temp file; faster-whisper decodes it via av.
        with tempfile.NamedTemporaryFile(suffix=".webm", delete=False) as f:
            f.write(raw)
            path = f.name
        if os.getenv("WHISPER_DEBUG_DUMP"):
            with open("/tmp/whisper_last.webm", "wb") as d:
                d.write(raw)
            print(f"[whisper] dump: {len(raw)} bytes -> /tmp/whisper_last.webm", flush=True)
        try:
            # Decode to 16 kHz mono float. The browser mic level is unpredictable,
            # so we level it — BUT capping the gain: blasting a near-silent clip to
            # full scale just amplifies noise/music and makes Whisper hallucinate.
            audio = decode_audio(path, sampling_rate=16000)
            peak = float(np.abs(audio).max()) if audio.size else 0.0
            if os.getenv("WHISPER_DEBUG_DUMP"):
                print(f"[whisper] decoded {audio.size/16000:.2f}s peak={peak:.4f}", flush=True)
            text = ""
            lang = forced or "en"
            # Too quiet → it's silence/ambient, not speech. Don't amplify, skip it.
            if peak >= 0.02:
                audio = audio * min(0.95 / peak, 10.0)
                with _lock:
                    # Backend-agnostic decode (faster-whisper on CPU, or MLX on
                    # Apple Silicon). Params for the faster path live in _decode.
                    segments, info = _decode(audio, forced)
                    # Drop low-confidence segments: when music plays on an external
                    # speaker the mic catches it and Whisper "hears voices" (garbled
                    # lyrics). Real speech sits well above -1.1 avg_logprob; gibberish
                    # from music/noise falls below it. Conservative so it won't eat
                    # genuine quiet speech.
                    kept = []
                    for sg in segments:
                        lp = getattr(sg, "avg_logprob", 0.0)
                        nsp = getattr(sg, "no_speech_prob", 0.0)
                        if lp < -1.1 or nsp > 0.7:
                            print(f"[whisper] drop seg lp={lp:.2f} nsp={nsp:.2f} {sg.text.strip()!r}", flush=True)
                            continue
                        kept.append(sg.text.strip())
                    text = " ".join(kept).strip()
                lang = forced or (info.language if info.language in ALLOWED else "en")
                if _is_hallucination(text):
                    text = ""
                prob = round(float(info.language_probability), 3)
                detected = info.language
            else:
                prob, detected = 0.0, "silence"
            if text:
                print(f"[whisper] ({lang}) {text!r}", flush=True)
            self._json(200, {
                "text": text,
                "language": lang,
                "probability": prob,
                "detected": detected,
            })
        except Exception as exc:  # never crash the loop on a bad clip
            self._json(500, {"error": str(exc)})
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    def log_message(self, *args):  # silence default request logging
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
