#!/usr/bin/env python3
"""
Scanned PDF -> TurboOCR -> cleaned Markdown -> EPUB3, driven by <book_dir>/book.json.

  python3 pipeline.py <book_dir>                    # all steps
  python3 pipeline.py <book_dir> --steps build,verify
  python3 pipeline.py <book_dir> --steps ocr --pages 0-30   # resumable, batchable
  python3 pipeline.py <book_dir> --force-ocr        # re-OCR even if ocr/md/pNN.md exists
  python3 pipeline.py <book_dir> --init             # write a starter book.json
  python3 pipeline.py <book_dir> --steps build,verify   # md-source books: pandoc only

Source of truth: book.json "source" = "ocr" (default; book.md is generated from
ocr/md pages) or "book.md" (book.md is hand-edited and is never regenerated;
the build step only runs pandoc on it, and the ocr/images steps are skipped).

Steps: ocr, images, build, verify.  build also writes review.txt.

Layout written inside <book_dir>:
  ocr/md/pNN.md     raw TurboOCR markdown per page (inline base64 images)
  ocr/meta.json     pdf + OCR settings used
  epub_assets/      extracted images, cover.jpg (referenced by book.md)
  book.md           cleaned, chapter-split markdown
  <epub_name>       final EPUB
  review.txt        every join decision + unjoined hyphen breaks (human check)
  verify.json       automated checks; exit code 1 on failure

Everything book-specific lives in book.json. The code below is book-independent.
"""
import argparse, base64, collections, html, io, json, os, re, subprocess, sys, time, zipfile

LOG = []                 # audit trail of join decisions (written to review.txt)
HITS = collections.Counter()   # which keep_joins entries actually matched
KEEP = set()
TYPO = []
FOOTERS = []


# ---------------------------------------------------------------- config
def load_cfg(bd):
    path = os.path.join(bd, "book.json")
    if not os.path.exists(path):
        sys.exit(f"no book.json in {bd} (run with --init to create one)")
    cfg = json.load(open(path, encoding="utf-8"))
    for k in ("source_pdf", "ocr", "title", "epub_name", "chapters"):
        if not cfg.get(k):
            sys.exit(f"book.json: '{k}' is required")
    cfg.setdefault("lang", "id")
    cfg.setdefault("cover_page", 0)
    cfg.setdefault("skip_pages", [])
    cfg.setdefault("continuations", {})
    cfg.setdefault("front_matter", {"heading": "", "lines": [], "pages": []})
    cfg.setdefault("footer_patterns", [])
    cfg.setdefault("typo_fixes", [])
    cfg.setdefault("keep_joins", [])
    cfg.setdefault("verify", {"must_contain": [], "must_not_contain": []})
    cfg["ocr"].setdefault("mode", "auto_verified")
    cfg["ocr"].setdefault("dpi", 200)
    cfg["ocr"].setdefault("timeout_s", 170)
    cfg["ocr"].setdefault("retries", 3)
    return cfg


def configure(cfg):
    global KEEP, TYPO, FOOTERS
    KEEP = {tuple(p) for p in cfg["keep_joins"]}
    TYPO = [tuple(p) for p in cfg["typo_fixes"]]
    FOOTERS = [re.compile(p) for p in cfg["footer_patterns"]]


def init_book(bd):
    pdfs = [f for f in os.listdir(bd) if f.lower().endswith(".pdf")]
    if len(pdfs) != 1:
        sys.exit(f"--init: expected exactly one .pdf in {bd}, found {pdfs}")
    template = {
        "source_pdf": pdfs[0],
        "ocr": {"server": "http://HOST:8020", "mode": "auto_verified",
                "dpi": 200, "timeout_s": 170, "retries": 3},
        "title": "TODO book title",
        "author": "TODO author",
        "lang": "id",
        "epub_name": "TODO.epub",
        "cover_page": 0,
        "skip_pages": [],
        "front_matter": {"heading": "Halaman Judul", "lines": [], "pages": []},
        "chapters": [
            [0, "TODO chapter title", None]
        ],
        "footer_patterns": [],
        "typo_fixes": [],
        "keep_joins": [],
        "verify": {"must_contain": [], "must_not_contain": []},
    }
    out = os.path.join(bd, "book.json")
    if os.path.exists(out):
        sys.exit("book.json already exists; not overwriting")
    json.dump(template, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"wrote {out}\nFill in chapters (from the document's own TOC), footer_patterns, "
          f"and keep_joins after the first OCR run. The pipeline cannot infer these.")


def md_path(bd, p):
    return os.path.join(bd, "ocr", "md", f"p{p:02d}.md")


def page_count(bd):
    d = os.path.join(bd, "ocr", "md")
    if not os.path.isdir(d):
        sys.exit("no ocr/md yet; run the ocr step first")
    n = len([f for f in os.listdir(d) if re.fullmatch(r"p\d{2}\.md", f)])
    missing = [p for p in range(n) if not os.path.exists(md_path(bd, p))]
    if missing:
        sys.exit(f"OCR incomplete, missing pages: {missing[:10]}")
    return n


# ---------------------------------------------------------------- OCR
def ocr(bd, cfg, pages_arg, force):
    import pymupdf
    src_path = os.path.join(bd, cfg["source_pdf"])
    src = pymupdf.open(src_path)
    n = len(src)
    oc = cfg["ocr"]
    os.makedirs(os.path.join(bd, "ocr", "md"), exist_ok=True)
    if pages_arg:
        a, _, b = pages_arg.partition("-")
        pages = range(int(a), int(b or a) + 1)
    else:
        pages = range(n)
    url = f"{oc['server'].rstrip('/')}/ocr/pdf?mode={oc['mode']}&markdown=1&dpi={oc['dpi']}"
    tmp = os.path.join(bd, "ocr", "md", "_one_page.pdf")
    failed = []
    for p in pages:
        out = md_path(bd, p)
        if not force and os.path.exists(out) and os.path.getsize(out) > 0:
            continue
        one = pymupdf.open()
        one.insert_pdf(src, from_page=p, to_page=p)
        one.save(tmp)
        one.close()
        text, err = None, ""
        for attempt in range(1, oc["retries"] + 1):
            r = subprocess.run(["curl", "-s", "-m", str(oc["timeout_s"]), "-X", "POST",
                                "-F", f"file=@{tmp}", url], capture_output=True, text=True)
            body = r.stdout
            # TurboOCR returns JSON only on error; success is markdown text
            if r.returncode == 0 and not body.lstrip().startswith("{"):
                if body.strip() or attempt == oc["retries"]:   # blank pages are legal
                    text = body
                    break
            err = f"rc={r.returncode} body={body[:200]!r}"
            time.sleep(2 * attempt)
        if text is None:
            failed.append(p)
            print(f"  p{p:02d} FAILED: {err}", flush=True)
            continue
        open(out, "w", encoding="utf-8").write(text)
        print(f"  p{p:02d} ok ({len(text)} bytes)", flush=True)
    if os.path.exists(tmp):
        os.remove(tmp)
    meta = {"source_pdf": cfg["source_pdf"], "num_pages": n, "ocr": oc,
            "pdf_sha256": sha256(src_path)}
    json.dump(meta, open(os.path.join(bd, "ocr", "meta.json"), "w", encoding="utf-8"), indent=2)
    if failed:
        print(f"OCR failed on pages {failed}; re-run the ocr step to retry them (resumable)")
        return False
    return True


def sha256(path):
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------- images
IMG_RE = r'data:image/(\w+);base64,([A-Za-z0-9+/=\s]+)'


def extract_images(bd, cfg, n):
    import pymupdf
    from PIL import Image, ImageChops
    assets = os.path.join(bd, "epub_assets")
    os.makedirs(assets, exist_ok=True)
    count = 0
    for p in range(n):
        t = open(md_path(bd, p), encoding="utf-8").read()
        for k, m in enumerate(re.finditer(IMG_RE, t)):
            raw = base64.b64decode(re.sub(r"\s", "", m.group(2)))
            im = Image.open(io.BytesIO(raw))
            if im.mode != "RGB":
                im = im.convert("RGB")
            r, g, b = im.split()
            dr = ImageChops.difference(r, g).getextrema()
            db = ImageChops.difference(b, g).getextrema()
            if dr[1] <= 6 and db[1] <= 6:          # scan is effectively grayscale
                im = im.convert("L")
            if im.width > 1600:
                im = im.resize((1600, int(im.height * 1600 / im.width)), Image.LANCZOS)
            im.save(os.path.join(assets, f"p{p:02d}_{k}.jpg"), quality=82, optimize=True)
            count += 1
    src = pymupdf.open(os.path.join(bd, cfg["source_pdf"]))
    cpage = src[cfg["cover_page"]]
    cdpi = int(max(10, min(160, 2400 * 72 / max(cpage.rect.width, cpage.rect.height))))   # cap long side at ~2400 px (huge scans)
    pix = cpage.get_pixmap(dpi=cdpi)
    Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB").save(
        os.path.join(assets, "cover.jpg"), quality=85)
    print(f"images: {count} page images + cover.jpg -> epub_assets/")


# ---------------------------------------------------------------- cleaning (book-independent)
_RPREF = ("se", "su", "ber", "ter", "per", "di", "ke", "me", "mem", "men",
          "meng", "meny", "pen", "pem", "peng", "peny", "pe", "te")
_RSUF = ("nya", "kan", "kah", "lah", "pun", "an", "ku", "i")


def _nrm_a(a):
    al = a.lower()
    for pr in sorted(_RPREF, key=len, reverse=True):
        if al.startswith(pr) and len(al) - len(pr) >= 3:
            return al[len(pr):]
    return al


def _nrm_b(b):
    bl = b.lower()
    for sf in sorted(_RSUF, key=len, reverse=True):
        if bl.endswith(sf) and len(bl) - len(sf) >= 3:
            return bl[:-len(sf)]
    return bl


def is_redup(a, b):
    """True when 'a- b' is a wrapped compound (kupu-kupu, sedalam-dalamnya), not a syllable split."""
    al, bl = a.lower(), b.lower()
    return al == bl or _nrm_a(a) == _nrm_b(b)


def inline_join(m):
    a, b = m.group(1), m.group(2)
    if is_redup(a, b):
        LOG.append(("inline", "compound-kept", a, b))
        return a + "-" + b
    if a.isupper() and b.islower():
        LOG.append(("inline", "joined-upper", a, b))
        return a + b.upper()
    LOG.append(("inline", "joined", a, b))
    return a + b


def para_join(m):
    a, b = m.group(1), m.group(2)
    if (a, b) in KEEP:
        HITS[(a, b)] += 1
        LOG.append(("para", "joined-verified", a, b))
    elif is_redup(a, b):
        LOG.append(("para", "compound-kept", a, b))
        return a + "-" + b
    else:
        LOG.append(("para", "UNJOINED", a, b))
        return m.group(0)
    if a.isupper() and b.islower():
        b = b.upper()
    return a + b


def join_paras(t):
    return re.sub(r"([A-Za-z0-9]+)-\n\n([A-Za-z0-9]+)", para_join, t)


def dehyphen(t):
    t = re.sub(r"\n{3,}", "\n\n", t)
    t = re.sub(r"([A-Za-z]+)- +([A-Za-z]\w*)", inline_join, t)
    t = re.sub(r"(\d)- +(\d)", r"\1-\2", t)          # digit ranges: "13- 14"
    t = join_paras(t)
    return t


def load_page(bd, i):
    t = open(md_path(bd, i), encoding="utf-8").read()
    n = [0]

    def img(m):
        name = f"p{i:02d}_{n[0]}.jpg"
        n[0] += 1
        return f"epub_assets/{name}"
    t = re.sub(r"data:image/\w+;base64,[A-Za-z0-9+/=\s]+", img, t)
    t = re.sub(r"!\[[^\]]*\]\(", "![](", t)           # OCR alt text is noise; drop it
    t = re.sub(r"<!--.*?-->", "", t)
    return t


def is_footer(line):
    return any(p.search(line) for p in FOOTERS)


def strip_boilerplate(t, drop_lines=()):
    drop = set()
    for d in drop_lines:
        drop.add(d)
        drop.add("# " + d)
    lines = t.split("\n")
    kept = [l for l in lines if l.strip() not in drop and not is_footer(l)]
    idx = [k for k, l in enumerate(kept) if l.strip()]
    for k in (idx[:1] + idx[-1:]):                   # bare page number at page edge
        if re.match(r"^\s*\d{1,3}\s*$", kept[k]):
            kept[k] = ""
    return "\n".join(kept)


def fix_list(t):
    lines = t.split("\n")
    out = []
    for k, l in enumerate(lines):
        s = l.strip()
        if re.match(r"^\d{1,2}[\.\)] +\S", s):
            out.append(re.sub(r"^(\d{1,2})[\.\)] +", r"\1. ", s))
        elif re.match(r"^\d{1,2}$", s):
            j = k + 1
            while j < len(lines) and not lines[j].strip():
                j += 1
            if j < len(lines) and lines[j].strip() and not lines[j].strip().startswith(("-", "#", "!")):
                lines[j] = f"{s}. {lines[j].strip()}"
                out.append("")
                continue
            out.append(l)
        else:
            out.append(l)
    return "\n".join(out)


def tidy(t):
    return re.sub(r"\n{3,}", "\n\n", t).strip()


def clean(bd, i, drop_lines=()):
    t = load_page(bd, i)
    t = strip_boilerplate(t, drop_lines)
    t = dehyphen(t)
    t = fix_list(t)
    for a, b in TYPO:
        t = t.replace(a, b)
    if re.search(r"^# ", t, re.M):
        print(f"  [warn] stray '#' heading on p{i:02d}; demoted to ##")
        t = re.sub(r"^# ", "## ", t, flags=re.M)
    return tidy(t)


def hoist_join(m):
    """Word split across a page break with an image between: move the image first."""
    a, img, b = m.group(1), m.group(2), m.group(3)
    if (a, b) not in KEEP:
        return m.group(0)
    HITS[(a, b)] += 1
    LOG.append(("para", "joined-verified(image-hoisted)", a, b))
    joined = a + (b.upper() if a.isupper() and b.islower() else b)
    return f"{img}\n\n{joined}"


# ---------------------------------------------------------------- build
def build(bd, cfg, n):
    global LOG
    LOG = []
    HITS.clear()
    if cfg.get("source", "ocr") == "book.md":
        # book.md is the hand-edited source: do not regenerate it, just validate
        # the file exists and run the EPUB step below.
        md_p = os.path.join(bd, "book.md")
        if not os.path.exists(md_p):
            sys.exit("source=book.md but book.md is missing")
        md = open(md_p, encoding="utf-8").read()
        print(f"book.md (hand-edited source, not regenerated): {len(md)} chars")
        return build_epub(bd, cfg)
    chapters_cfg = cfg["chapters"]
    skip = set(cfg["skip_pages"])
    fm = cfg["front_matter"]
    parts = []
    if fm.get("heading"):
        parts.append(f"# {fm['heading']}")
    parts += fm.get("lines", [])
    parts += [clean(bd, p) for p in fm.get("pages", [])]
    chapters = ["\n\n".join(parts)]

    for k, (s, title, eyebrow) in enumerate(chapters_cfg):
        end = chapters_cfg[k + 1][0] if k + 1 < len(chapters_cfg) else n
        cont = {int(a): int(b) for a, b in cfg.get("continuations", {}).items()}   # page -> start page of the article it continues
        pages = [p for p in range(s, end) if p not in skip and p not in cont]
        pages += sorted(p for p, tgt in cont.items() if tgt == s and p not in skip)
        drop = [title] + ([eyebrow] if eyebrow else [])
        body = [clean(bd, p, drop_lines=tuple(drop) if p == s else ()) for p in pages]
        head = f"# {title}\n"
        if eyebrow:
            head += f"\n*{eyebrow}*\n"
        body_txt = "\n\n".join(x for x in body if x)
        body_txt = re.sub(r"([A-Za-z]+)-\n\n(!\[[^\]]*\]\([^)]*\))\n\n([A-Za-z]+)", hoist_join, body_txt)
        body_txt = join_paras(body_txt)
        chapters.append(head + "\n\n" + body_txt)

    md = "\n\n---\n\n".join(chapters)
    for a, b in TYPO:
        md = md.replace(a, b)
    assert "data:image" not in md, "unreplaced data URI"
    assert "<!-- page" not in md, "page comment leaked"
    out_md = os.path.join(bd, "book.md")
    open(out_md, "w", encoding="utf-8").write(md)
    h1 = len(re.findall(r"^# ", md, re.M))
    print(f"book.md: {len(md)} chars, h1={h1} (expected {1 + len(chapters_cfg)})")
    write_review(bd, cfg, md)
    return build_epub(bd, cfg)


def build_epub(bd, cfg):
    epub = os.path.join(bd, cfg["epub_name"])
    cmd = ["pandoc", "book.md", "-o", epub, "--from", "markdown+pipe_tables", "--to", "epub3",
           "--epub-cover-image=epub_assets/cover.jpg",
           "--metadata", f"title={cfg['title']}",
           *(["--metadata", f"author={cfg['author']}"] if cfg.get("author") else []),
           "--metadata", f"lang={cfg['lang']}",
           "--split-level=1", "--resource-path=.", "--toc", "--toc-depth=1"]
    for key in ("publisher", "date", "description", "subject"):   # optional, from book.json
        if cfg.get(key):
            cmd += ["--metadata", f"{key}={cfg[key]}"]
    r = subprocess.run(cmd, cwd=bd, capture_output=True, text=True)
    if r.returncode != 0 or "PandocResourceNotFound" in r.stderr:
        print("pandoc problems:\n" + r.stderr[:3000])
        sys.exit("build failed")
    print(f"EPUB: {epub} ({os.path.getsize(epub) // 1024} KB)")


def write_review(bd, cfg, md):
    lines = ["REVIEW REPORT (generated by pipeline.py build)", ""]
    unjoined = [l for l in LOG if l[1] == "UNJOINED"]
    ctx = []
    for m in re.finditer(r"[A-Za-z0-9]+-\n\n", md):
        ctx.append(md[max(0, m.start() - 50):m.end() + 40].replace("\n", "\\n"))
    lines.append(f"A. Paragraph/page hyphen breaks NOT joined ({len(ctx)}). Each is either a")
    lines.append("   scrambled block order (leave alone) or a real word split to add to keep_joins:")
    for c in ctx:
        lines.append(f"   ...{c}...")
    lines.append("")
    lines.append("B. keep_joins entries that never matched (stale or typo?):")
    stale = [p for p in KEEP if HITS[p] == 0]
    for a, b in sorted(stale):
        lines.append(f"   {a!r} + {b!r}")
    if not stale:
        lines.append("   none")
    lines.append("")
    lines.append("C. Join decisions (count, kind, a, b):")
    counts = collections.Counter(LOG)
    for (kind, dec, a, b), c in sorted(counts.items(), key=lambda x: (x[0][1], -x[1])):
        lines.append(f"   x{c:<4} {dec:<32} {kind:<7} {a!r:>18} + {b!r}")
    open(os.path.join(bd, "review.txt"), "w", encoding="utf-8").write("\n".join(lines) + "\n")
    print(f"review.txt: {len(ctx)} unjoined breaks, {len(stale)} stale keep_joins, "
          f"{len(counts)} distinct decisions")


# ---------------------------------------------------------------- verify
def verify(bd, cfg, n):
    fails, stats = [], {}
    md_p = os.path.join(bd, "book.md")
    epub_p = os.path.join(bd, cfg["epub_name"])
    md = open(md_p, encoding="utf-8").read() if os.path.exists(md_p) else ""
    if not md:
        fails.append("book.md missing")
    if "<!-- page" in md:
        fails.append("page comments leaked")
    if "data:image" in md or "![](![]" in md:
        fails.append("base64 / double image wrap leaked")
    if re.search(r"!\[[^\]\n]+\]\(epub_assets", md):
        fails.append("image with alt text (alt-form) left in markdown")
    nf = sum(1 for l in md.split("\n") if is_footer(l))
    if nf:
        fails.append(f"running footers left: {nf}")
    refs = set(re.findall(r"epub_assets/([\w.-]+\.jpg)", md)) | {"cover.jpg"}
    missing = sorted(r for r in refs if not os.path.exists(os.path.join(bd, "epub_assets", r)))
    if missing:
        fails.append(f"asset files missing: {missing}")
    expect_h1 = 1 + len(cfg["chapters"]) if cfg["front_matter"].get("heading") else len(cfg["chapters"])
    h1 = len(re.findall(r"^# ", md, re.M))
    if h1 != expect_h1:
        fails.append(f"h1 count {h1} != expected {expect_h1}")

    if not os.path.exists(epub_p):
        fails.append("epub missing")
    else:
        z = zipfile.ZipFile(epub_p)
        names = z.namelist()
        xh = [x for x in names if x.endswith(".xhtml")]
        allx = "".join(z.read(x).decode("utf8", "ignore") for x in xh)
        plain = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", allx))
        media = {x.split("/")[-1] for x in names if x.lower().endswith((".jpg", ".jpeg", ".png"))}
        erefs = set(re.findall(r'(?:src|xlink:href)="\.\./media/([^"]+)"', allx))
        if media != erefs:
            fails.append(f"media/reference mismatch: unreferenced={sorted(media - erefs)} "
                         f"broken={sorted(erefs - media)}")
        for probe in cfg["verify"]["must_contain"]:
            if probe not in plain:
                fails.append(f"missing expected text: {probe!r}")
        for probe in cfg["verify"]["must_not_contain"]:
            if probe in plain:
                fails.append(f"forbidden text present: {probe!r}")
        navs = [x for x in names if x.endswith("nav.xhtml")]
        toc = []
        if navs:
            toc = [html.unescape(re.sub(r"<[^>]+>", "", t))
                   for t in re.findall(r"<a[^>]*>([^<]+)</a>", z.read(navs[0]).decode())]
        norm = lambda x: re.sub(r"\s+", " ", x.replace("\u00a0", " ")).strip()
        toc_n = {norm(t) for t in toc}
        for _, title, _ in cfg["chapters"]:
            if norm(title) not in toc_n:
                fails.append(f"chapter missing from TOC: {title!r}")
        stats = {"epub_kb": os.path.getsize(epub_p) // 1024, "words": len(plain.split()),
                 "images": len(media), "toc_entries": len(toc), "pages": n}
    report = {"ok": not fails, "failures": fails, "stats": stats}
    json.dump(report, open(os.path.join(bd, "verify.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    print("VERIFY:", "PASS" if not fails else "FAIL", json.dumps(stats))
    for f in fails:
        print("  -", f)
    return not fails


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("book_dir")
    ap.add_argument("--steps", default="ocr,images,build,verify")
    ap.add_argument("--pages", help="OCR only this range, e.g. 0-30 (inclusive)")
    ap.add_argument("--force-ocr", action="store_true")
    ap.add_argument("--init", action="store_true")
    a = ap.parse_args()
    bd = os.path.abspath(a.book_dir)
    if a.init:
        init_book(bd)
        return
    cfg = load_cfg(bd)
    configure(cfg)
    steps = [s.strip() for s in a.steps.split(",") if s.strip()]
    if cfg.get("source", "ocr") == "book.md":
        if "ocr" in steps or "images" in steps:
            print("source=book.md: skipping ocr/images steps (book.md is the source)")
        steps = [s for s in steps if s not in ("ocr", "images")]
    ok = True
    if "ocr" in steps:
        ok = ocr(bd, cfg, a.pages, a.force_ocr) and ok
        if not ok:
            sys.exit(1)
    if "images" in steps or "build" in steps or "verify" in steps:
        # md-source books don't need the OCR page files; verify() ignores n
        n = 0 if cfg.get("source", "ocr") == "book.md" else page_count(bd)
    if "images" in steps:
        extract_images(bd, cfg, n)
    if "build" in steps:
        build(bd, cfg, n)
    if "verify" in steps:
        ok = verify(bd, cfg, n) and ok
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
