#!/usr/bin/env python3
"""Correction ledger for a hand-edited book.md (OCR -> book.md lineage).

The rule this tool enforces: every change from the OCR baseline to book.md is one entry in
`corrections.json`, written in the same step that makes the change. `corrections.md` is rendered from it.

    corrections.py init   <book_dir> [--baseline FILE]   freeze the OCR baseline, create an empty ledger
    corrections.py scan   <book_dir> [--current]         run the SymSpell checker (tools/ocrcheck) on the baseline and
                                                         list every candidate with its decision (PENDING = none yet)
    corrections.py apply  <book_dir> --find F --replace R --reason WHY [--tool T] [--count N]
                                                         edit book.md AND log it, in one step
    corrections.py keep   <book_dir> --word W --reason WHY   log a candidate that was reviewed and left as printed
    corrections.py sync   <book_dir> [--reasons FILE] [--reason WHY] [--logged reconstructed|at-edit-time]
                                                         log edits already made by hand (found by diffing book.md
                                                         against baseline + ledger)
    corrections.py check  <book_dir> [--scan]            exit 1 if book.md != replay(baseline, ledger), or (with
                                                         --scan) a SymSpell candidate has no decision
    corrections.py report <book_dir>                     re-render corrections.md

Ledger semantics. An `edit` entry is an exact string replacement: `find` (a unique context containing the changed
words) -> `replace`, matching `count` times. Replaying all edit entries on the baseline, in order, must give book.md
byte for byte; `check` and `pipeline.py --steps verify` test exactly that. `keep` entries record SymSpell candidates
that were reviewed and deliberately not changed.

Checker setup (once): `python3 tools/ocrcheck/build_merged_dict.py` (KBBI IV + KBBI VI + IndoLeX + Leipzig) and
`cargo build --release` in tools/ocrcheck.
"""
import argparse, datetime, difflib, hashlib, json, os, re, subprocess, sys

REPO = os.path.dirname(os.path.abspath(__file__))
OCRCHECK = os.environ.get("OCRCHECK_BIN", os.path.join(REPO, "tools/ocrcheck/target/release/ocrcheck"))
LEXDIR = os.environ.get("OCRCHECK_LEXICON", os.path.join(REPO, "lexicon"))
LEDGER, REPORT, BASE_DEFAULT = "corrections.json", "corrections.md", "book.ocr-original.md"


def die(msg):
    sys.exit("corrections: " + msg)


def sha(s):
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def rd(p):
    return open(p, encoding="utf-8").read()


def wr(p, s):
    open(p, "w", encoding="utf-8").write(s)


def paths(bd):
    return {k: os.path.join(bd, v) for k, v in
            (("md", "book.md"), ("ledger", LEDGER), ("report", REPORT), ("cfg", "book.json"))}


def load_ledger(bd):
    p = paths(bd)["ledger"]
    if not os.path.exists(p):
        die("no %s in %s. Run: corrections.py init <book_dir>" % (LEDGER, bd))
    return json.load(open(p, encoding="utf-8"))


def save_ledger(bd, led):
    wr(paths(bd)["ledger"], json.dumps(led, ensure_ascii=False, indent=2) + "\n")
    wr(paths(bd)["report"], render(led))


def baseline_text(bd, led):
    p = os.path.join(bd, led["baseline"]["file"])
    if not os.path.exists(p):
        die("baseline %s is missing" % p)
    t = rd(p)
    if sha(t) != led["baseline"]["sha256"]:
        die("baseline %s was modified (sha256 differs from the ledger); it must stay frozen." % led["baseline"]["file"])
    return t


# ------------------------------------------------------------------ SymSpell (tools/ocrcheck)
def alpha_tokens(text):
    t = re.sub(r"\(epub_assets[^)]*\)", "", text)
    t = re.sub(r"https?://\S+", "", t)
    return re.findall(r"[A-Za-z]+", t)


def checker_ready():
    return os.path.exists(OCRCHECK) and os.path.exists(os.path.join(LEXDIR, "dict.txt"))


def flagged(tokens):
    """{lowercase token: 'suggestion(count) ...'} for tokens the lexicon does not know; None if the checker is not built."""
    if not checker_ready():
        return None
    uniq = sorted({w.lower() for w in tokens if len(w) >= 3})
    p = subprocess.run([OCRCHECK, os.path.join(LEXDIR, "dict.txt")], input="\n".join(uniq), capture_output=True, text=True)
    out = {}
    for line in p.stdout.splitlines():
        w, _, s = line.partition("\t")
        out[w] = s
    return out


_strict = None


def in_dictionary(token):
    """True/False: token is in KBBI IV / KBBI VI / IndoLeX (Leipzig web text excluded); None if lexicon not built."""
    global _strict
    p = os.path.join(LEXDIR, "strict.txt")
    if _strict is None:
        _strict = set(rd(p).split()) if os.path.exists(p) else False
    return None if _strict is False else token.lower() in _strict


# ------------------------------------------------------------------ diff -> entries
def toks(s):
    return re.findall(r"\s+|\S+", s)


def classify(old, new):
    if re.fullmatch(r"\s*\d+ dari \d+ halaman\s*", old) and not new.strip():
        return "remove-scan-footer"
    if "\n\n" in old and new == " ":
        return "join-paragraph"
    if "epub_assets" in old + new and re.search(r"\.(jpg|webp)", old + new):
        return "image-reference"
    if old.strip() == new.strip():
        return "whitespace"
    if re.sub(r"\s+", "", old).lower() == re.sub(r"\s+", "", new).lower():
        return "split-words"
    return "spelling"


def show_ws(s):
    """Text as shown in the log: the words if there are any, else a visible marker for a whitespace-only side."""
    if s.strip():
        return s.strip()
    return {"": "", " ": "␣"}.get(s, "¶" if "\n" in s else "␣")


def locate_page(bd, old, left, right=""):
    """1-based PDF page whose OCR text holds the passage; list if ambiguous, None if not found."""
    d = os.path.join(bd, "ocr", "md")
    if not os.path.isdir(d):
        return None
    norm = lambda x: re.sub(r"\s+", " ", x)
    cands = []
    if old.strip():
        cands.append(norm(left + old + right).strip())          # word with its anchored context
    else:                                                       # whitespace-only edit: use the words on both sides
        cands.append((norm(left)[-30:] + " " + norm(right)[:30]).strip())
    pages = {}
    for f in sorted(os.listdir(d)):
        m = re.fullmatch(r"p(\d+)\.md", f)
        if m:
            t = re.sub(r"!\[[^\]]*\]\(data:[^)]*\)", "", rd(os.path.join(d, f)))
            pages[int(m.group(1)) + 1] = norm(t)
    for needle in cands:
        hits = [pg for pg, t in pages.items() if needle and needle in t]
        if hits:
            return hits[0] if len(hits) == 1 else hits
    # words may straddle a page break in the raw OCR (e.g. a paragraph that is cut): try each side alone
    for needle in (norm(left).strip()[-25:], norm(right).strip()[:25]):
        hits = [pg for pg, t in pages.items() if len(needle) >= 8 and needle in t]
        if len(hits) == 1:
            return hits[0]
    return None


def expand_unique(cur, a, b):
    """Grow [a,b) by whole words on both sides until cur[a:b] occurs exactly once."""
    for _ in range(14):
        if cur.count(cur[a:b]) == 1 and cur[a:b].strip():
            return a, b
        m = re.search(r"\S+\s*$", cur[:a])
        a = m.start() if m else a
        m = re.match(r"\s*\S+", cur[b:])
        b = b + m.end() if m else b
    return a, b


def diff_entries(bd, state, target, reasons=None, logged="at-edit-time", date=None, forced_tool=None, forced_reason=None):
    """Edit entries that turn `state` into `target` exactly, in replay order."""
    A, B = toks(state), toks(target)
    ops = [o for o in difflib.SequenceMatcher(None, A, B, autojunk=False).get_opcodes() if o[0] != "equal"]
    offs = [0]
    for t in A:
        offs.append(offs[-1] + len(t))
    cur, delta, out = state, 0, []
    fl = flagged(alpha_tokens(state)) or {}
    for _op, i1, i2, j1, j2 in ops:
        old, new = state[offs[i1]:offs[i2]], "".join(B[j1:j2])
        pos = offs[i1] + delta
        a, b = expand_unique(cur, pos, pos + len(old))
        left, right = cur[a:pos], cur[pos + len(old):b]
        ctx = "…" + re.sub(r"\s+", " ", cur[max(0, a - 35):pos] + "«" + old + "→" + new + "»" + cur[pos + len(old):b + 35]).strip() + "…"
        ctx = ctx.replace("«→", "«∅→").replace("→»", "→∅»")
        words_old = [w for w in alpha_tokens(old) if len(w) >= 2]
        hit = [w.lower() for w in words_old if w.lower() in fl]
        kind = classify(old, new)
        word_key = re.sub(r"[^A-Za-z]", "", old).lower()
        dic = None
        if words_old and kind in ("spelling",):
            vals = [in_dictionary(w) for w in words_old]
            dic = None if None in vals else all(vals)
        out.append({
            "date": date or datetime.date.today().isoformat(), "logged": logged,
            "tool": forced_tool or ("symspell" if hit else "manual"), "kind": kind,
            "before": show_ws(old), "after": show_ws(new),
            "page": locate_page(bd, old, left, right), "context": ctx,
            "find": left + old + right, "replace": left + new + right, "count": 1,
            "symspell_flagged": bool(hit), "symspell_suggestions": ({w: fl[w] for w in hit} if hit else None),
            "before_in_dictionary": dic,
            "reason": (reasons or {}).get(old.strip()) or (reasons or {}).get(word_key) or forced_reason or "",
        })
        cur = cur[:a] + out[-1]["replace"] + cur[b:]
        delta += len(out[-1]["replace"]) - (b - a)
    assert cur == target, "internal error: diff entries do not reproduce the target"
    return out


def replay(base, led):
    cur, problems = base, []
    for e in led["entries"]:
        if e["type"] != "edit":
            continue
        n = cur.count(e["find"])
        if n != e["count"]:
            problems.append("%s: 'find' occurs %d times (expected %d): %r" % (e["id"], n, e["count"], e["find"][:60]))
        cur = cur.replace(e["find"], e["replace"])
    return cur, problems


def next_id(led, kind):
    pre = "C" if kind == "edit" else "K"
    return "%s%03d" % (pre, 1 + sum(1 for e in led["entries"] if e["type"] == kind))


# ------------------------------------------------------------------ commands
def cmd_init(a):
    bd = os.path.abspath(a.book_dir)
    p = paths(bd)
    if os.path.exists(p["ledger"]):
        die("%s already exists; refusing to overwrite" % LEDGER)
    bfile = a.baseline or BASE_DEFAULT
    bpath = os.path.join(bd, bfile)
    if os.path.exists(bpath):
        base = rd(bpath)
    else:
        if not os.path.exists(p["md"]):
            die("book.md missing")
        base = rd(p["md"])
        wr(bpath, base)
        print("baseline frozen: %s (copy of the current book.md; do this BEFORE the first edit)" % bfile)
    led = {"version": 1,
           "baseline": {"file": bfile, "sha256": sha(base),
                        "what": "book.md as generated by pipeline.py build from the OCR pages, before any hand or SymSpell edit"},
           "checker": {"tool": "tools/ocrcheck (symspell_rs 7.0.1, edit distance 1)",
                       "lexicon": "KBBI IV + KBBI VI + IndoLeX + Leipzig (tools/ocrcheck/build_merged_dict.py)"},
           "verified_against_scan": False, "entries": []}
    save_ledger(bd, led)
    cfg = json.load(open(p["cfg"], encoding="utf-8"))
    cfg["corrections"] = {"baseline": bfile, "enforce": True}
    wr(p["cfg"], json.dumps(cfg, ensure_ascii=False, indent=2))
    print("created %s and %s; book.json corrections.enforce=true" % (LEDGER, REPORT))


def decisions(led):
    fixed, kept = {}, {}
    for e in led["entries"]:
        if e["type"] == "edit":
            for w in alpha_tokens(e["before"]):
                fixed.setdefault(w.lower(), e["id"])
        elif e["type"] == "keep":
            kept[e["word"].lower()] = e["id"]
    return fixed, kept


def occurrences(text, w):
    plain = re.sub(r"\(epub_assets[^)]*\)", "", text)
    rx = r"(?i)(?<![A-Za-z])%s(?![A-Za-z])" % re.escape(w)
    m = re.search(rx, plain)
    ctx = re.sub(r"\s+", " ", plain[max(0, m.start() - 35):m.end() + 35]).strip() if m else ""
    return len(re.findall(rx, plain)), ctx


def cmd_scan(a):
    bd = os.path.abspath(a.book_dir)
    led = load_ledger(bd)
    text = rd(paths(bd)["md"]) if a.current else baseline_text(bd, led)
    fl = flagged(alpha_tokens(text))
    if fl is None:
        die("SymSpell checker not ready: (cd tools/ocrcheck && cargo build --release) and python3 tools/ocrcheck/build_merged_dict.py")
    fixed, kept = decisions(led)
    pend = 0
    print("%d SymSpell candidates in %s" % (len(fl), "current book.md" if a.current else led["baseline"]["file"]))
    for w, s in sorted(fl.items()):
        n, ctx = occurrences(text, w)
        st = ("fixed " + fixed[w]) if w in fixed else ("kept  " + kept[w]) if w in kept else "PENDING"
        pend += st == "PENDING"
        print("  %-10s %-22s x%-2d %-42s …%s…" % (st, w, n, s or "(no suggestion)", ctx))
    print("PENDING: %d" % pend)
    sys.exit(1 if pend and not a.current else 0)


def cmd_apply(a):
    bd = os.path.abspath(a.book_dir)
    led, p = load_ledger(bd), paths(bd)
    base = baseline_text(bd, led)
    expected, probs = replay(base, led)
    book = rd(p["md"])
    if probs or expected != book:
        die("book.md has edits that are not in the ledger yet. Run `corrections.py sync` first.")
    n = book.count(a.find)
    if n != a.count:
        die("--find occurs %d times, expected %d (add context or pass --count). Nothing changed." % (n, a.count))
    new_book = book.replace(a.find, a.replace)
    if a.count == 1:
        ents = diff_entries(bd, book, new_book, forced_tool=a.tool, forced_reason=a.reason)
    else:  # same replacement at several places: one entry with count, tokens taken from the find/replace pair
        e = diff_entries(bd, a.find, a.replace, forced_tool=a.tool, forced_reason=a.reason)[0]
        e.update({"find": a.find, "replace": a.replace, "count": a.count, "page": None})
        ents = [e]
    for e in ents:
        e["id"], e["type"] = next_id(led, "edit"), "edit"
        if a.kind:
            e["kind"] = a.kind
        led["entries"].append(e)
        print("%s  %-8s %r -> %r  %s" % (e["id"], e["tool"], e["before"], e["after"], e["context"]))
    back, probs = replay(base, led)
    if back != new_book or probs:
        die("replay mismatch; nothing written: %s" % probs)
    save_ledger(bd, led)  # ledger first, then the book: a crash in between is caught by `check`
    wr(p["md"], new_book)


def cmd_keep(a):
    bd = os.path.abspath(a.book_dir)
    led = load_ledger(bd)
    fl = flagged(alpha_tokens(baseline_text(bd, led))) or {}
    w = a.word.lower()
    n, ctx = occurrences(rd(paths(bd)["md"]), w)
    if n == 0:
        die("%r does not occur in book.md" % a.word)
    e = {"id": next_id(led, "keep"), "type": "keep", "date": datetime.date.today().isoformat(), "logged": a.logged,
         "tool": "symspell" if w in fl else "manual", "word": a.word, "occurrences_in_book": n,
         "context": "…" + ctx + "…", "symspell_suggestions": fl.get(w) or None, "reason": a.reason}
    led["entries"].append(e)
    save_ledger(bd, led)
    print(e["id"], "kept as printed:", a.word, "-", a.reason)


def cmd_sync(a):
    bd = os.path.abspath(a.book_dir)
    led, p = load_ledger(bd), paths(bd)
    base = baseline_text(bd, led)
    expected, probs = replay(base, led)
    if probs:
        die("ledger does not replay cleanly: %s" % probs)
    book = rd(p["md"])
    if expected == book:
        print("nothing to sync: book.md == baseline + ledger")
        return
    reasons = json.load(open(a.reasons, encoding="utf-8")) if a.reasons else {}
    ents = diff_entries(bd, expected, book, reasons=reasons, logged=a.logged, date=a.date, forced_reason=a.reason)
    for e in ents:
        e["id"], e["type"] = next_id(led, "edit"), "edit"
        led["entries"].append(e)
        print("%s  %-8s %-18s %r -> %r  p.%s" % (e["id"], e["tool"], e["kind"], e["before"], e["after"], e["page"]))
    save_ledger(bd, led)
    print("logged %d edit(s)" % len(ents))


def check(bd, scan=False):
    """List of problems; empty means the ledger explains book.md exactly."""
    led = load_ledger(bd)
    base = baseline_text(bd, led)
    got, problems = replay(base, led)
    book = rd(paths(bd)["md"])
    if got != book:
        A, B = toks(got), toks(book)
        hs = [("".join(A[i1:i2]), "".join(B[j1:j2])) for op, i1, i2, j1, j2 in
              difflib.SequenceMatcher(None, A, B, autojunk=False).get_opcodes() if op != "equal"]
        problems.append("book.md differs from baseline+ledger in %d place(s), e.g. %s. Log them: corrections.py sync <book_dir>"
                        % (len(hs), "; ".join("%r -> %r" % h for h in hs[:3])))
    if scan:
        fl = flagged(alpha_tokens(base))
        if fl is None:
            print("corrections: SymSpell checker not built; scan completeness not checked")
        else:
            fixed, kept = decisions(led)
            pend = sorted(w for w in fl if w not in fixed and w not in kept)
            if pend:
                problems.append("SymSpell candidates with no logged decision: %s (corrections.py scan <book_dir>)" % ", ".join(pend))
    ids = [e["id"] for e in led["entries"]]
    if len(ids) != len(set(ids)):
        problems.append("duplicate ids in ledger")
    problems += ["%s has no reason" % e["id"] for e in led["entries"] if not e.get("reason")]
    if not os.path.exists(paths(bd)["report"]) or rd(paths(bd)["report"]) != render(led):
        problems.append("corrections.md is stale; run corrections.py report <book_dir>")
    return problems


def cmd_check(a):
    bd = os.path.abspath(a.book_dir)
    pr = check(bd, scan=a.scan)
    led = load_ledger(bd)
    ne = sum(1 for e in led["entries"] if e["type"] == "edit")
    nk = sum(1 for e in led["entries"] if e["type"] == "keep")
    print("CORRECTIONS:", "PASS" if not pr else "FAIL", "- %d edits, %d kept as printed" % (ne, nk))
    for x in pr:
        print("  -", x)
    sys.exit(1 if pr else 0)


# ------------------------------------------------------------------ report
def cell(s):
    return ("" if s in (None, "") else str(s)).replace("|", "\\|").replace("\n", " ")


def render(led):
    E = [e for e in led["entries"] if e["type"] == "edit"]
    K = [e for e in led["entries"] if e["type"] == "keep"]
    by = {}
    for e in E:
        by[e["tool"]] = by.get(e["tool"], 0) + 1
    L = ["# Log koreksi", "",
         "Dibuat otomatis dari `corrections.json`; jangan disunting tangan. Sunting `book.md`, lalu catat dengan "
         "`corrections.py apply` atau `corrections.py sync`.", "",
         "## Silsilah teks", "",
         "1. `ocr/md/pNN.md`: keluaran mentah TurboOCR per halaman PDF.",
         "2. `%s`: hasil `pipeline.py --steps build` dari halaman-halaman itu (kata putus di akhir baris disambung, footer "
         "berulang dan komentar halaman dibuang oleh pipeline). **Dibekukan** (sha256 `%s…`), tidak pernah disunting."
         % (led["baseline"]["file"], led["baseline"]["sha256"][:12]),
         "3. Setiap perubahan dari (2) ke `book.md` ada di tabel di bawah. Memutar ulang semua entri pada (2) harus "
         "menghasilkan `book.md` persis; itu diperiksa oleh `corrections.py check` dan `pipeline.py --steps verify`.",
         "4. `book.md` adalah sumber EPUB; EPUB biasa dan edisi Standard Ebooks dibangun darinya.", "",
         "Pemeriksa ejaan: %s. Leksikon: %s." % (led["checker"]["tool"], led["checker"]["lexicon"]),
         "Dibandingkan dengan pindaian: **%s**." % ("ya" if led.get("verified_against_scan") else
                                                   "belum (koreksi berdasar saran SymSpell, kamus, dan konteks kalimat)"), "",
         "## Ringkasan", "",
         "- %d suntingan (%s)" % (len(E), ", ".join("%s %d" % kv for kv in sorted(by.items())) or "-"),
         "- %d kandidat ditinjau dan sengaja dibiarkan" % len(K), "",
         "## Suntingan", "",
         "| ID | Hal. PDF | Alat | Jenis | Sebelum | Sesudah | Konteks | Saran SymSpell | Alasan | Dicatat |",
         "|---|---|---|---|---|---|---|---|---|---|"]
    for e in E:
        sug = "; ".join("%s: %s" % kv for kv in (e.get("symspell_suggestions") or {}).items())
        if e.get("before_in_dictionary") is True:
            sug = (sug + " " if sug else "") + "(kata ada di kamus KBBI/IndoLeX, tak terdeteksi alat)"
        pg = e.get("page")
        pg = "/".join(map(str, pg)) if isinstance(pg, list) else pg
        L.append("| %s | %s | %s | %s | `%s` | `%s` | %s | %s | %s | %s%s |" % (
            e["id"], cell(pg), e["tool"], e["kind"], cell(e["before"]).replace("\n", "↵") or "∅", cell(e["after"]) or "∅",
            cell(e["context"]), cell(sug), cell(e["reason"]), e["date"], " (direkonstruksi)" if e["logged"] == "reconstructed" else ""))
    L += ["", "## Ditinjau dan dibiarkan", "",
          "| ID | Kata | Jumlah | Alat | Konteks | Saran SymSpell | Alasan |", "|---|---|---|---|---|---|---|"]
    for e in K:
        L.append("| %s | `%s` | %s | %s | %s | %s | %s |" % (e["id"], e["word"], e["occurrences_in_book"], e["tool"],
                                                              cell(e["context"]), cell(e["symspell_suggestions"]), cell(e["reason"])))
    if any(e["logged"] == "reconstructed" for e in led["entries"]):
        L += ["", "Catatan: entri bertanda *(direkonstruksi)* dibuat dengan membandingkan berkas cadangan, bukan dicatat saat "
              "penyuntingan. Perubahannya identik dengan yang sebenarnya terjadi; alasan dan alat ditulis ulang setelah kejadian."]
    return "\n".join(L) + "\n"


def cmd_report(a):
    bd = os.path.abspath(a.book_dir)
    wr(paths(bd)["report"], render(load_ledger(bd)))
    print("wrote", paths(bd)["report"])


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    for name in ("init", "scan", "apply", "keep", "sync", "check", "report"):
        s = sp.add_parser(name)
        s.add_argument("book_dir")
        if name == "init":
            s.add_argument("--baseline")
        if name == "scan":
            s.add_argument("--current", action="store_true")
        if name == "apply":
            s.add_argument("--find", required=True)
            s.add_argument("--replace", required=True)
            s.add_argument("--reason", required=True)
            s.add_argument("--tool", choices=["symspell", "manual", "strict-lexicon"])
            s.add_argument("--kind")
            s.add_argument("--count", type=int, default=1)
        if name == "keep":
            s.add_argument("--word", required=True)
            s.add_argument("--reason", required=True)
            s.add_argument("--logged", default="at-edit-time", choices=["at-edit-time", "reconstructed"])
        if name == "sync":
            s.add_argument("--reasons", help="JSON {word_before: reason}")
            s.add_argument("--reason", help="reason for every hunk not covered by --reasons")
            s.add_argument("--logged", default="at-edit-time", choices=["at-edit-time", "reconstructed"])
            s.add_argument("--date")
        if name == "check":
            s.add_argument("--scan", action="store_true")
    a = ap.parse_args()
    globals()["cmd_" + a.cmd](a)


if __name__ == "__main__":
    main()
