#!/usr/bin/env python3
"""Build the Indonesian lexicon used by corrections.py / ocrcheck from the `tutur` data dump.

    python3 build_merged_dict.py [--tutur DIR] [--out DIR]

Writes (default: <repo>/lexicon/, git-ignored):
    dict.txt    "word count" per line, for SymSpell (ocrcheck). Count = sum over KBBI IV, KBBI VI, IndoLeX and
                Leipzig frequencies, so a word in a trusted dictionary outranks a rare Leipzig web-text typo.
    strict.txt  words that appear in KBBI IV, KBBI VI or IndoLeX only (no Leipzig). Leipzig is web text and
                contains common OCR typos ("sebagain", "olek", "dai"), so membership there proves nothing.

Words are lowercased to a-z, hyphens removed, multiword entries dropped.
"""
import argparse, collections, html, json, os, re

ap = argparse.ArgumentParser()
ap.add_argument("--tutur", default=os.environ.get("TUTUR_DATA", "/Users/sf/Documents/workspaces/tutur/data"))
ap.add_argument("--out", default=os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "lexicon"))
a = ap.parse_args()
D = a.tutur.rstrip("/") + "/"


def norm(w):
    w = html.unescape(w).strip().lower()
    if " " in w:
        return None
    w = w.replace("-", "")
    return w if re.fullmatch("[a-z]+", w) else None


count, strict = collections.Counter(), set()
for r in json.load(open(D + "edisi-IV/dictionary__JSON.json"))["dictionary"]:
    w = norm(r["word"])
    if w:
        count[w] += 1; strict.add(w)
for r in json.load(open(D + "kbbi-v6/kbbi_v6__JSON.json")):
    for s in [r.get("kata")] + list(r.get("lema") or []):
        w = norm(s) if s else None
        if w:
            count[w] += 1; strict.add(w)
for r in json.load(open(D + "indolex/indolex__JSON.json"))["indolex"]:
    w = norm(r["word"])
    if w:
        count[w] += int(r.get("frequency") or 1); strict.add(w)
for r in json.load(open(D + "leipzig/leipzig_words__JSON.json"))["leipzig_words"]:
    w = norm(r["word"])
    if w:
        count[w] += int(r["frequency"])

os.makedirs(a.out, exist_ok=True)
open(os.path.join(a.out, "dict.txt"), "w").write("".join(f"{w} {n}\n" for w, n in count.most_common()))
open(os.path.join(a.out, "strict.txt"), "w").write("".join(w + "\n" for w in sorted(strict)))
print(f"{len(count)} word forms -> {a.out}/dict.txt; {len(strict)} dictionary-only forms -> strict.txt")
