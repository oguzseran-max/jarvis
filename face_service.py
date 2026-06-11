#!/usr/bin/env python3
"""
JARVIS Face Service — local face-recognition microservice.

Runs under face-venv (Python 3.12, insightface + onnxruntime), like the Whisper
service. The main backend (3.14) POSTs a JPEG frame; this returns who is in it
(Leyla / Aylin / nobody) using the enrolled embeddings — all on-device, nothing
stored.

Protocol:
  GET  /health                  -> {"status":"ok","identities":[...]}
  POST /recognize  (JPEG body)  -> {"name": "Leyla"|null, "score": 0.83, "second": 0.2}

Start with:  ./face-venv/bin/python face_service.py    (default port 8770)
"""

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np

import face_engine

PORT = int(os.getenv("FACE_PORT", "8770"))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/health"):
            db = face_engine._load_db()
            names = list(db[0]) if db else []
            self._json(200, {"status": "ok", "identities": names})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if not self.path.startswith("/recognize"):
            self._json(404, {"error": "not found"})
            return
        try:
            import cv2
            n = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(n) if n else b""
            if not raw:
                self._json(400, {"error": "empty body"})
                return
            arr = np.frombuffer(raw, dtype=np.uint8)
            img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if img is None:
                self._json(400, {"error": "bad image"})
                return
            name, score, second = face_engine.identify(img)
            self._json(200, {"name": name, "score": round(score, 3),
                             "second": round(second, 3)})
        except Exception as e:
            self._json(500, {"error": str(e)[:200]})


def main():
    print(f"[face] loading model + identities …", flush=True)
    face_engine._get_app()          # warm the model
    db = face_engine._load_db()
    ids = list(db[0]) if db else []
    if not ids:
        print("[face] WARNING: no enrolled identities — run `face_engine.py enroll`", flush=True)
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"[face] ready on :{PORT}  identities={ids}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    sys.exit(main())
