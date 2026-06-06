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

# Decoding cost knobs (the voice loop is latency-bound — see perf_monitor "slow
# transcription" flags). beam_size=5 runs the decoder 5× per step; on a CPU int8
# model that dominates a multi-second utterance. Greedy decoding (beam_size=1) is
# the standard real-time setting and, with VAD + the in-domain primer, costs
# almost nothing in accuracy on short conversational speech. Both overridable.
BEAM_SIZE = int(os.getenv("WHISPER_BEAM_SIZE", "1"))
# ctranslate2 defaults to only 4 CPU threads regardless of cores; let it use them.
CPU_THREADS = int(os.getenv("WHISPER_CPU_THREADS", str(os.cpu_count() or 4)))

print(f"[whisper] loading model '{MODEL_SIZE}' (beam={BEAM_SIZE}, threads={CPU_THREADS}) …", flush=True)
model = WhisperModel(MODEL_SIZE, device="cpu", compute_type="int8", cpu_threads=CPU_THREADS)
_lock = threading.Lock()  # faster-whisper isn't meant for concurrent calls

# Per-language decoder primers — short, in-domain vocabulary that nudges Whisper
# toward the household's proper nouns and habits (kept brief to avoid the model
# echoing these words into silence). English left unprimed.
_PRIMERS = {
    "fr": ("Conversation familière en français avec Marion. "
           "mon amour, Leyla, Aylin, Spotify, Phil Collins, la musique, "
           "le salon, la mezzanine, la cuisine, le portail, les lumières, la météo."),
    "tr": "Türkçe sohbet. Marion, müzik, Spotify, ışıklar, kapı, hava durumu.",
}

# self_eval.py writes learned proper nouns here; we merge them into the primer
# live (re-read only when the file changes), so Marion stops mis-hearing words
# the user has corrected — without restarting this service.
_VOCAB_DIR = Path(__file__).resolve().parent / "data"
_vocab_cache: dict = {}  # lang -> (mtime, "word, word, ...")


def _primer_for(lang):
    base = _PRIMERS.get(lang)
    if not lang:
        return base
    try:
        path = _VOCAB_DIR / f"whisper_vocab_{lang}.txt"
        mtime = path.stat().st_mtime
        cached = _vocab_cache.get(lang)
        if not cached or cached[0] != mtime:
            learned = path.read_text().strip()
            _vocab_cache[lang] = (mtime, learned)
        else:
            learned = cached[1]
    except Exception:
        learned = ""
    if learned:
        return f"{base or ''} {learned}".strip()
    return base
print(f"[whisper] ready on :{PORT}  langs={sorted(ALLOWED)}", flush=True)

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
                    segments, info = model.transcribe(
                        audio, language=forced, beam_size=BEAM_SIZE,
                        vad_filter=True,
                        no_speech_threshold=0.6,
                        compression_ratio_threshold=2.2,
                        condition_on_previous_text=False,
                        # Prime the decoder with in-domain French/Turkish words so
                        # proper nouns and household vocabulary are recognised
                        # correctly (costs ~nothing, improves accuracy). Includes
                        # words self_eval has learned from the user's corrections.
                        initial_prompt=_primer_for(forced),
                    )
                    text = " ".join(s.text.strip() for s in segments).strip()
                lang = forced or (info.language if info.language in ALLOWED else "en")
                if _is_hallucination(text):
                    text = ""
                prob = round(float(info.language_probability), 3)
                detected = info.language
            else:
                prob, detected = 0.0, "silence"
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
