"""
JARVIS Face Engine — local face recognition for the family (insightface).

100% on-device: no cloud, no video stored. It enrols a few reference photos of
each person into a small embeddings file (numbers, not images) and then matches
a live frame against them. Used by the "surveillance" mode so Marion can greet
Leyla / Aylin by name.

CLI:
  ./face-venv/bin/python face_engine.py enroll      # build embeddings from faces/<Name>/*
  ./face-venv/bin/python face_engine.py test        # sanity-check it separates the people
  ./face-venv/bin/python face_engine.py id <image>  # identify one image
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import numpy as np

_REPO = Path(__file__).resolve().parent
_FACES_DIR = _REPO / "faces"
_DB_PATH = _REPO / "data" / "face_db.npz"

# Cosine-similarity threshold for a confident match, plus a margin the winner
# must beat the runner-up by (so a borderline frame isn't mislabelled).
MATCH_THRESHOLD = float(os.getenv("FACE_THRESHOLD", "0.45"))
MATCH_MARGIN = float(os.getenv("FACE_MARGIN", "0.06"))

_app = None  # lazy insightface FaceAnalysis


def _get_app():
    global _app
    if _app is None:
        from insightface.app import FaceAnalysis
        app = FaceAnalysis(name="buffalo_l", providers=["CPUExecutionProvider"])
        app.prepare(ctx_id=-1, det_size=(640, 640))
        _app = app
    return _app


def _largest_face(img):
    faces = _get_app().get(img)
    if not faces:
        return None
    return max(faces, key=lambda f: (f.bbox[2] - f.bbox[0]) * (f.bbox[3] - f.bbox[1]))


def _norm(v):
    n = np.linalg.norm(v)
    return v / n if n > 0 else v


# ---------------------------------------------------------------------------
# Enrolment
# ---------------------------------------------------------------------------

def enroll() -> dict:
    """Build a mean normalised embedding per person from faces/<Name>/*.jpg."""
    import cv2
    names, embeds = [], []
    summary = {}
    for person_dir in sorted(p for p in _FACES_DIR.iterdir() if p.is_dir()):
        name = person_dir.name
        vecs = []
        for img_path in sorted(person_dir.glob("*")):
            if img_path.suffix.lower() not in (".jpg", ".jpeg", ".png"):
                continue
            img = cv2.imread(str(img_path))
            if img is None:
                continue
            face = _largest_face(img)
            if face is None:
                print(f"  ! no face found in {img_path.name}")
                continue
            vecs.append(_norm(face.normed_embedding))
        if vecs:
            mean = _norm(np.mean(vecs, axis=0))
            names.append(name)
            embeds.append(mean)
            summary[name] = len(vecs)
            print(f"  {name}: enrolled from {len(vecs)} photo(s)")
        else:
            print(f"  {name}: NO usable faces — skipped")
    if not names:
        raise SystemExit("No faces enrolled. Put reference photos in faces/<Name>/")
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    np.savez(_DB_PATH, names=np.array(names), embeds=np.array(embeds))
    print(f"\nSaved {len(names)} identities -> {_DB_PATH}")
    return summary


# ---------------------------------------------------------------------------
# Recognition
# ---------------------------------------------------------------------------

_db_cache = None


def _load_db():
    global _db_cache
    if _db_cache is None:
        if not _DB_PATH.exists():
            return None
        d = np.load(_DB_PATH, allow_pickle=True)
        _db_cache = (list(d["names"]), d["embeds"])
    return _db_cache


def identify(img) -> tuple[str | None, float, float]:
    """Return (name|None, score, runner_up_score) for the largest face in `img`
    (a BGR numpy array). name is None if no confident match."""
    db = _load_db()
    if db is None:
        return None, 0.0, 0.0
    names, embeds = db
    face = _largest_face(img)
    if face is None:
        return None, 0.0, 0.0
    emb = _norm(face.normed_embedding)
    sims = embeds @ emb  # cosine (all normalised)
    order = np.argsort(sims)[::-1]
    top = float(sims[order[0]])
    second = float(sims[order[1]]) if len(order) > 1 else 0.0
    if top >= MATCH_THRESHOLD and (top - second) >= MATCH_MARGIN:
        return names[order[0]], top, second
    return None, top, second


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _test():
    """Leave-this-photo-in cross check: every reference photo should be
    identified as its own person, and the two people must be well separated."""
    import cv2
    db = _load_db()
    if db is None:
        print("No DB — run `enroll` first."); return
    total = ok = 0
    for person_dir in sorted(p for p in _FACES_DIR.iterdir() if p.is_dir()):
        for img_path in sorted(person_dir.glob("*")):
            if img_path.suffix.lower() not in (".jpg", ".jpeg", ".png"):
                continue
            img = cv2.imread(str(img_path))
            if img is None:
                continue
            name, score, second = identify(img)
            total += 1
            hit = name == person_dir.name
            ok += hit
            mark = "OK " if hit else "XX "
            print(f"  {mark} {person_dir.name:8s} <- {img_path.name[:18]:18s} "
                  f"=> {str(name):8s} (top={score:.2f}, 2nd={second:.2f})")
    print(f"\n{ok}/{total} correctly identified.")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "enroll"
    if cmd == "enroll":
        enroll()
    elif cmd == "test":
        _test()
    elif cmd == "id" and len(sys.argv) > 2:
        import cv2
        img = cv2.imread(sys.argv[2])
        print(identify(img))
    else:
        print(__doc__)
