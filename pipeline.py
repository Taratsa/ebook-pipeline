#!/usr/bin/env python3
"""
Scanned PDF -> TurboOCR -> cleaned Markdown -> EPUB3, driven by <book_dir>/book.json.

  python3 pipeline.py <book_dir>                    # all steps
  python3 pipeline.py <book_dir> --steps build,verify
  python3 pipeline.py <book_dir> --steps ocr --pages 0-30   # resumable, batchable
  python3 pipeline.py <book_dir> --force-ocr        # re-OCR even if ocr/md/pNN.md exists
  python3 pipeline.py <book_dir> --init             # write a starter book.json
  python3 pipeline.py <book_dir> --steps build,verify   # md-source books: pandoc only

Finished EPUB: book.json "finish" {...} (colophon.html / license.html / titlepage.html / metadata.yaml in the book folder, if present, replace the generated pages) switches build to the MasasilaM-style
edition (title page, colophon, nested TOC, poem/figure markup, templates/masasilam.css).

Source of truth: book.json "source" = "ocr" (default; book.md is generated from
ocr/md pages) or "book.md" (book.md is hand-edited and is never regenerated;
the build step only runs pandoc on it, and the ocr step is skipped; the images step still
works and re-extracts epub_assets/ from ocr/md and the PDF).
book.json "image_format": "jpg" (default) | "webp" (+ "webp_quality", default 75) selects the page-image
format; the cover stays cover.jpg unless book.json "cover_image" points at another file (e.g.
"epub_assets/cover.webp").

Steps: ocr, images, build, verify, se.  build also writes review.txt.
Correction ledger: for a hand-edited book.md use corrections.py (every edit logged in corrections.json); with book.json "corrections": {"enforce": true}, verify fails on any unlogged edit.
Standard Ebooks edition: `--steps se` (book.json "se" block; see se_edition.py and README) builds a second,
SE-manual-1.9.1 EPUB "<epub_name> (SE).epub" from book.md and gates it with se lint, a word-parity check and epubcheck.

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
import argparse, base64, collections, glob, html, io, json, os, re, subprocess, sys, time, zipfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

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


def img_ext(cfg):
    """Extension of page images in epub_assets/ (book.json "image_format": "jpg" | "webp").
    The cover stays JPEG (cover.jpg) for reader/library compatibility, as in the MasasilaM reference."""
    fmt = cfg.get("image_format", "jpg").lower().lstrip(".")
    if fmt not in ("jpg", "webp"):
        sys.exit(f'image_format must be "jpg" or "webp", got {fmt!r}')
    return fmt


IMG_EXT = "jpg"


def cover_path(cfg):
    """EPUB cover, relative to the book folder. book.json "cover_image" (e.g. a hand-made
    epub_assets/cover.webp) wins; otherwise epub_assets/cover.jpg rendered from the PDF cover page."""
    return cfg.get("cover_image") or "epub_assets/cover.jpg"


def configure(cfg):
    global KEEP, TYPO, FOOTERS, IMG_EXT
    IMG_EXT = img_ext(cfg)
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
    # The server address is private: keep it out of book.json (which is committed). Set TURBOOCR_SERVER in the shell.
    server = (os.environ.get("TURBOOCR_SERVER") or oc["server"]).rstrip("/")
    if "HOST" in server:
        sys.exit("ocr: no server. Run: export TURBOOCR_SERVER=http://<host>:8020  (book.json keeps the placeholder on purpose)")
    url = f"{server}/ocr/pdf?mode={oc['mode']}&markdown=1&dpi={oc['dpi']}"
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
    ext = img_ext(cfg)
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
            out = os.path.join(assets, f"p{p:02d}_{k}.{ext}")
            if ext == "webp":
                im.save(out, "WEBP", quality=int(cfg.get("webp_quality", 75)), method=6)
            else:
                im.save(out, quality=82, optimize=True)
            count += 1
    if cfg.get("cover_image"):
        print(f"images: {count} page images; cover = {cfg['cover_image']} (cover_image set, PDF cover not rendered)")
        return
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
        name = f"p{i:02d}_{n[0]}.{IMG_EXT}"
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



# ------------------------------------------------- finished-EPUB (MasasilaM style)
# Structure and styling follow the MasasilaM reference edition
# ("Menyambut Kongres Kebudayaan", masasilam.com): title page, colophon,
# nested TOC, div.poem verse, div.image-with-caption, full OPF metadata.
# Enabled by a "finish" object in book.json; book.md itself is never modified.
def _he(t):
    return html.escape(str(t), quote=True)


def finish_colophon(cfg, fin):
    title = cfg["title"]
    credits = fin.get("credits", [])
    rows = []
    for label, name in credits:
        rows += [f'<p class="credit-label">{_he(label)}</p>', f'<p class="credit-name">{_he(name)}</p>']
    box = [f'<p>{_he(x)}</p>' for x in fin.get("rights_lines", [])]
    if cfg.get("source_url"):
        box.append(f'<p>Sumber pindaian: <a href="{_he(cfg["source_url"])}"><em>'
                   f'{_he(fin.get("source_label", cfg["source_url"]))}</em></a></p>')
    out = ['<p class="colophon-title"><strong>%s</strong></p>' % _he(title.upper()),
           '<p class="byline">oleh</p>',
           '<p class="author-name"><strong>%s</strong></p>' % _he(cfg.get("author", "").upper()),
           '<hr class="credit-divider"/>', *rows,
           '<div class="info-box copyright-box">', *box, '</div>']
    if fin.get("contact"):
        out.append('<p class="contribution-note">Kesalahan ketik dan koreksi dapat dikirimkan ke '
                   f'<a href="mailto:{_he(fin["contact"])}">{_he(fin["contact"])}</a>.</p>')
    out.append('<p class="colophon-footer">%s</p>' % fin.get(
        "attribution_html",
        'Tata letak, gaya, dan kolofon mengikuti edisi <a href="https://masasilam.com">'
        '<em>MasasilaM</em></a>.'))
    return "\n".join(out)


LICENSE_DEFAULT = {
    "name": "CC BY-SA 4.0",
    "full_name": "Creative Commons Atribusi-BerbagiSerupa 4.0 Internasional",
    "url": "https://creativecommons.org/licenses/by-sa/4.0/deed.id",
}


def license_info(fin):
    lic = fin.get("license", True)
    if lic is False:
        return None
    return {**LICENSE_DEFAULT, **(lic if isinstance(lic, dict) else {})}


def finish_license(cfg, fin):
    """Back-matter license page, modelled on the MasasilaM edition of 'Tahun yang Tak Pernah
    Berakhir' (in-copyright material): the page is titled with the license name, the full name
    is a subtitle, and only the digital-edition work is licensed; rights in the original
    material are not claimed. Returns the HTML that follows the h1."""
    L = license_info(fin)
    n, full, url = _he(L["name"]), _he(L["full_name"]), _he(L["url"])
    contact = fin.get("contact")
    who = (f'<a href="mailto:{_he(contact)}"><i>{_he(contact)}</i></a>' if contact
           else "pengelola edisi digital ini")
    return "\n".join([
        f'<div class="subtitle">\n{full}\n</div>',
        f'<p>Halaman hak cipta biasanya memberi tahu apa yang tidak boleh dilakukan. Sebaliknya, '
        f'pernyataan <i>{n}</i> ini hadir untuk membuka kemungkinan.</p>',
        '<p><i>E-book</i> ini merupakan hasil digitalisasi dan penyuntingan, tanpa klaim kepemilikan '
        'atas materi aslinya.</p>',
        f'<p>Seluruh kontribusi dalam edisi ini\u2014termasuk digitalisasi, penyuntingan, atau penataan '
        f'tipografi\u2014dilepaskan di bawah lisensi <a href="{url}"><i>{full} ({n})</i></a>.</p>',
        f'<p>Pemegang hak cipta atas materi dalam <i>e-book</i> ini yang keberatan atas penggunaannya '
        f'dipersilakan menghubungi {who} untuk peninjauan lebih lanjut.</p>',
        '<p>Upaya ini dilakukan semata-mata demi memperkaya khazanah literasi, mendukung pelestarian, '
        'dan membuka akses seluas-luasnya agar isi dalam <i>e-book</i> ini dapat dibaca, dinikmati, '
        'sekaligus dipelajari.</p>',
    ])


def finish_markdown(md, cfg, fin, colophon_html=None, license_html=None, titlepage_html=None):
    """book.md -> pandoc input for the finished EPUB (title page, colophon, poems, figures)."""
    paras = md.split("\n\n")
    out, i = [], 0
    is_poem = lambda p: (p.startswith("*") and p.endswith("*") and "\\\n" in p and "**" not in p)
    while i < len(paras):
        p = paras[i]
        if is_poem(p):
            stanzas = []
            while i < len(paras) and is_poem(paras[i]):
                lines = [l.strip().rstrip("\\").strip("*").strip() for l in paras[i].split("\n")]
                stanzas.append("<div>" + " ".join(f"<span>{_he(l)}</span>" for l in lines if l) + "</div>")
                i += 1
            out.append('```{=html}\n<div class="poem">\n' + "\n".join(stanzas) + "\n</div>\n```")
            continue
        if re.fullmatch(r"!\[\]\(epub_assets/[^)]+\)(\{[^}]*\})?", p):
            out.append("::: image-with-caption\n" + p + "\n:::")
        else:
            out.append(p)
        i += 1
    body = "\n\n".join(out)
    title = cfg["title"]
    lines = fin.get("title_lines")
    tp_label = fin.get("titlepage_toc")           # e.g. "Judul": list the title page in the TOC under this label
    tp_text = "<br/>".join(_he(l) for l in lines) if lines else title
    tp_attrs = ('{#judul .titlepage epub:type="titlepage" title="%s"}' % tp_label) if tp_label \
        else '{.titlepage .unlisted epub:type="titlepage"}'
    # the reference colophon has an EMPTY h1 whose title attribute carries the label; the build empties the
    # visible text after pandoc (class hidden-heading, see postprocess_epub) so the TOC keeps "Kolofon"
    col_attrs = '{.colophon .hidden-heading epub:type="colophon" title="Kolofon"}' if fin.get("hide_colophon_heading", True) \
        else '{.colophon epub:type="colophon"}'
    head = [f'# {tp_text} {tp_attrs}',
            ("```{=html}\n" + titlepage_html + "\n```") if titlepage_html is not None
            else f'### {cfg.get("author", "")} {{.unlisted}}',
            f'# Kolofon {col_attrs}',
            "```{=html}\n" + (colophon_html if colophon_html is not None else finish_colophon(cfg, fin)) + "\n```"]
    tail = ""
    L = license_info(fin)
    if L:
        lic = license_html if license_html is not None else finish_license(cfg, fin)
        tail = (f'\n\n# {L.get("title") or L["name"]} {{.uncopyright epub:type="uncopyright"}}\n\n```{{=html}}\n' + lic + "\n```\n")
    return "\n\n".join(head) + "\n\n" + body + tail


def metadata_file_args(bd, cfg):
    """Hand-edited EPUB metadata (book folder metadata.yaml). Passed to pandoc AFTER the generated
    metadata, so any key set there overrides the value derived from book.json."""
    name = cfg.get("metadata_file", "metadata.yaml")
    if os.path.exists(os.path.join(bd, name)):
        print(f"metadata: using hand-edited {name}")
        return ["--metadata-file", name]
    return []


def _plain(node):
    """pandoc JSON inline/meta node -> plain text."""
    t = node.get("t") if isinstance(node, dict) else None
    c = node.get("c") if isinstance(node, dict) else None
    if t in ("MetaString",):
        return c
    if t in ("MetaInlines", "MetaBlocks", "MetaList"):
        return "".join(_plain(x) for x in c)
    if t == "Str":
        return c
    if t in ("Space", "SoftBreak", "LineBreak"):
        return " "
    if t in ("Emph", "Strong", "Span", "Quoted", "SmallCaps", "Underline", "Strikeout", "Plain", "Para"):
        inner = c[-1] if t in ("Span", "Quoted") else c
        return "".join(_plain(x) for x in inner)
    return ""


def metadata_overrides(bd, cfg):
    """title/author actually used by the book: metadata.yaml (if present) wins over book.json, so the
    title page and generated colophon stay consistent with the OPF. Parsed by pandoc (no PyYAML needed)."""
    out = {"title": cfg["title"], "author": cfg.get("author", "")}
    name = cfg.get("metadata_file", "metadata.yaml")
    if not os.path.exists(os.path.join(bd, name)):
        return out
    r = subprocess.run(["pandoc", "-f", "markdown", "-t", "json", "--metadata-file", name],
                       cwd=bd, input="", capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"{name}: invalid YAML for pandoc:\n{r.stderr[:800]}")
    meta = json.loads(r.stdout).get("meta", {})
    for k in ("first-publisher", "first-published"):      # OPF-only: pandoc ignores them, we inject them
        if k in meta and _plain(meta[k]).strip():
            out[k] = _plain(meta[k]).strip()
    if "title" in meta:
        out["title"] = _plain(meta["title"]).strip() or out["title"]
    cr = meta.get("creator")
    if cr:
        items = cr["c"] if cr.get("t") == "MetaList" else [cr]
        names = []
        for it in items:
            if it.get("t") == "MetaMap":
                role = _plain(it["c"].get("role", {"t": "MetaString", "c": "aut"})) if "role" in it["c"] else "aut"
                if role in ("aut", "") and "text" in it["c"]:
                    names.append(_plain(it["c"]["text"]).strip())
            else:
                names.append(_plain(it).strip())
        if names:
            out["author"] = " \u00b7 ".join(n for n in names if n)
    return out


def read_metadata(bd, cfg):
    """metadata.yaml as plain python: str, [str] or [{role,text}] per key (parsed by pandoc, no PyYAML). {} if absent."""
    name = cfg.get("metadata_file", "metadata.yaml")
    if not os.path.exists(os.path.join(bd, name)):
        return {}
    r = subprocess.run(["pandoc", "-f", "markdown", "-t", "json", "--metadata-file", name],
                       cwd=bd, input="", capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"{name}: invalid YAML for pandoc:\n{r.stderr[:800]}")
    def val(n):
        if n.get("t") == "MetaList":
            return [val(x) for x in n["c"]]
        if n.get("t") == "MetaMap":
            return {k: _plain(v).strip() for k, v in n["c"].items()}
        return _plain(n).strip()
    return {k: val(v) for k, v in json.loads(r.stdout).get("meta", {}).items()}


def _yq(v):
    return json.dumps(v, ensure_ascii=False)      # JSON scalars are valid YAML


def _current_identifier(bd, cfg):
    """dc:identifier of the EPUB built last (so pinning it does not change the book's identity)."""
    f = os.path.join(bd, cfg.get("epub_name", "") + ".epub") if cfg.get("epub_name") else None
    for cand in ([f] if f else []) + [x for x in glob.glob(os.path.join(bd, "*.epub")) if "(SE)" not in x]:
        try:
            with zipfile.ZipFile(cand) as z:
                opf = next(n for n in z.namelist() if n.endswith("content.opf"))
                m = re.search(r'<dc:identifier[^>]*>([^<]+)</dc:identifier>', z.read(opf).decode("utf-8"))
                if m and m.group(1).startswith("urn:uuid:"):
                    return m.group(1)
        except (OSError, StopIteration, KeyError, zipfile.BadZipFile):
            continue
    return None


def export_metadata(bd, cfg):
    """Write metadata.yaml pre-filled with the metadata the build currently generates (never overwrites)."""
    name = cfg.get("metadata_file", "metadata.yaml")
    path = os.path.join(bd, name)
    if os.path.exists(path):
        sys.exit(f"{name} already exists; edit it directly (delete it to re-export)")
    fin = cfg.get("finish") or {}
    m = finish_metadata(cfg, fin)
    import uuid
    L = ["# EPUB metadata, hand-editable. Any key set here overrides the value generated from book.json.",
         "# Remove a key to fall back to the generated value. Rebuild with: pipeline.py <book> --steps build,verify",
         "# Keys: title, subtitle, creator, contributor, publisher, subject, description, rights, source, date,",
         "#       identifier, lang. Roles are MARC relators (aut, edt, trl, ill, trc, ...).",
         "",
         "# Pinned so readers keep reading position/annotations across rebuilds (a new random one is made",
         "# on every build otherwise). Do not change after publishing.",
         f"identifier: {_yq(_current_identifier(bd, cfg) or 'urn:uuid:' + str(uuid.uuid4()))}",
         "# Publication date of this edition (YYYY-MM-DD); without it the build timestamp is used.",
         "# date: 2026-01-01", "",
         "# OPF-only keys (injected after the build; pandoc has no field for them), as in the MasasilaM editions:",
         "#   first-publisher -> <meta property=\"schema:firstPublisher\">  who first published the work",
         "#   first-published -> <meta property=\"schema:firstPublished\">  year of first publication",
         "# first-publisher: \"\"",
         "# first-published: \"\"",
         "# publisher and source must be ONE string for the pandoc EPUB (a YAML list makes pandoc write an empty element);",
         "# join several publishers with \", \". The Standard Ebooks edition also accepts a list for source (one dc:source each).", ""]
    for k in ("title", "lang", "publisher", "rights", "source", "description"):
        if m.get(k):
            L.append(f"{k}: {_yq(m[k])}")
    pr = (cfg.get("se") or {}).get("producer", {}).get("name")
    L += ["", "# Standard Ebooks edition (--steps se) reads the same file: publisher, source (one or a list), creator (aut),",
          "# producer (dc:contributor, role bkp). Title/subtitle/abstract/subjects of the SE edition stay in book.json 'se'.",
          f"producer: {_yq(pr)}" if pr else "# producer: \"\"", ""]
    for k in ("creator", "contributor"):
        if m.get(k):
            L.append(f"{k}:")
            for e in m[k]:
                L += [f"  - role: {_yq(e['role'])}", f"    text: {_yq(e['text'])}"]
    if m.get("subject"):
        L.append("subject:")
        L += [f"  - {_yq(x)}" for x in m["subject"]]
    open(path, "w", encoding="utf-8").write("\n".join(L) + "\n")
    print(f"wrote {path}")


def finish_metadata(cfg, fin):
    meta = {"lang": cfg["lang"], "toc-title": fin.get("toc_title", "Daftar Isi")}
    meta["title"] = cfg["title"]
    if cfg.get("author"):
        meta["creator"] = [{"role": "aut", "text": cfg["author"]}]
    contrib = [{"role": r, "text": n} for r, n in fin.get("contributors", [])]
    if contrib:
        meta["contributor"] = contrib
    pub = fin.get("publisher") or cfg.get("publisher")
    if pub:
        meta["publisher"] = pub
    if fin.get("subjects") or cfg.get("subject"):
        meta["subject"] = fin.get("subjects") or [cfg["subject"]]
    if cfg.get("description") or fin.get("description"):
        meta["description"] = fin.get("description") or cfg["description"]
    L = license_info(fin)
    if fin.get("rights") or L:
        meta["rights"] = fin.get("rights") or L["name"]
    if cfg.get("source_url"):
        meta["source"] = cfg["source_url"]
    return meta


LANDMARK_ID = {"Title Page": "Halaman Judul", "Cover": "Sampul", "Table of Contents": "Daftar Isi"}


def _sections_titles(zi):
    """section id -> title attribute, for every text/*.xhtml (Sigil convention: title attr = TOC label)."""
    out = {}
    for n in zi.namelist():
        if re.search(r"/text/[^/]+\.xhtml$", n):
            for tag in re.findall(r"<section\b[^>]*>", zi.read(n).decode("utf-8")):
                i, t = re.search(r'\bid="([^"]+)"', tag), re.search(r'\btitle="([^"]*)"', tag)
                if i and t:
                    out[i.group(1)] = t.group(1)
    return out


def postprocess_epub(epub, cfg, finished=False, bd=None):
    """Fix-ups pandoc cannot express (rewrites the zip in place):
    - cover.xhtml and nav.xhtml are taken OUT OF THE SPINE (not just linear="no": KOReader ignores linear="no"
      by default, so the cover page and the multi-page nav counted as pages 1-5 and could be paged back to).
      nav.xhtml stays in the package (readers build their TOC menu from it) and the cover image stays the
      cover-image in the manifest. The landmarks that pointed at those two documents are dropped, since
      epubcheck rejects landmarks to non-spine documents (RSC-011);
    - Indonesian landmark labels when lang is id;
    - finished builds: colophon -> frontmatter, license page -> backmatter, `hidden-heading` h1 emptied (its text
      moves to a title attribute), a trailing bare <hr/> at the end of a chapter removed, and a section's
      `title` attribute becomes its label in nav.xhtml and toc.ncx."""
    src, tmp = epub, epub + ".tmp"
    ov = metadata_overrides(bd, cfg) if bd else {}
    schema_meta = [(f"schema:{prop}", ov[key]) for key, prop in
                   (("first-published", "firstPublished"), ("first-publisher", "firstPublisher")) if ov.get(key)]
    with zipfile.ZipFile(src) as zi, zipfile.ZipFile(tmp, "w") as zo:
        titles = _sections_titles(zi) if finished else {}
        for item in zi.infolist():
            data = zi.read(item.filename)
            name = item.filename
            if name.endswith("content.opf"):
                if schema_meta:
                    xml = "".join(f'    <meta property="{k}">{_he(v)}</meta>\n' for k, v in schema_meta).encode("utf-8")
                    m = re.search(rb"</dc:publisher>[ \t]*\r?\n", data)
                    if m:
                        data = data[:m.end()] + xml + data[m.end():]
                    else:
                        data = data.replace(b"</metadata>", xml + b"</metadata>", 1)
                data, k = re.subn(rb'[ \t]*<itemref idref="nav"[^>]*/>[ \t]*\r?\n?', b"", data, count=1)
                if not k and b'idref="nav"' in data:
                    sys.exit("postprocess: could not remove nav itemref from content.opf")
                data = re.sub(rb'[ \t]*<itemref idref="cover_xhtml"[^>]*/>[ \t]*\r?\n?', b"", data, count=1)
            elif name.endswith("/nav.xhtml") or name == "nav.xhtml":
                if finished and b'epub:type="titlepage"' not in data.split(b'<nav epub:type="landmarks"')[-1]:
                    # pandoc's own title page is disabled in finished builds; point the landmark at our ch001
                    data = data.replace(b'<ol>\n    <li>\n      <a href="text/cover.xhtml"',
                                        b'<ol>\n    <li>\n      <a href="text/ch001.xhtml" epub:type="titlepage">Title Page</a>\n    </li>\n    <li>\n      <a href="text/cover.xhtml"', 1)
                # landmarks to cover.xhtml / the nav itself point at non-spine documents -> drop
                head, sep, lm = data.partition(b'<nav epub:type="landmarks"')
                lm = re.sub(rb'\s*<li>\s*<a href="(?:text/cover\.xhtml|#toc)"[^>]*>[^<]*</a>\s*</li>', b"", lm)
                data = head + sep + lm
                if cfg.get("lang", "").startswith("id"):
                    for en, idn in LANDMARK_ID.items():
                        data = data.replace((">%s</a>" % en).encode(), (">%s</a>" % idn).encode())
                if titles:
                    t = data.decode("utf-8")
                    for sid, label in titles.items():
                        t = re.sub(r'(<a href="text/[^"#]+#%s">)(.*?)(</a>)' % re.escape(sid),
                                   lambda m: m.group(1) + label + m.group(3), t, flags=re.S)
                    data = t.encode("utf-8")
            elif name.endswith("toc.ncx") and titles:
                t = data.decode("utf-8")
                for sid, label in titles.items():
                    t = re.sub(r'(<text>)[^<]*(</text>\s*</navLabel>\s*<content src="text/[^"#]+#%s")' % re.escape(sid),
                               lambda m: m.group(1) + label + m.group(2), t)
                data = t.encode("utf-8")
            elif finished and name.endswith(".xhtml"):
                if b'epub:type="colophon"' in data:
                    data = data.replace(b'<body epub:type="backmatter">', b'<body epub:type="frontmatter">', 1)
                elif b'epub:type="uncopyright"' in data:
                    data = data.replace(b'<body epub:type="bodymatter">', b'<body epub:type="backmatter">', 1)
                if b"hidden-heading" in data or b"<hr />" in data:
                    t = data.decode("utf-8")

                    def hide(m):
                        attrs, text = m.group(1), m.group(2)
                        attrs = re.sub(r"\s*\bhidden-heading\b", "", attrs)
                        if "title=" not in attrs:
                            plain = html.unescape(re.sub(r"<[^>]+>", " ", text))
                            attrs += ' title="%s"' % _he(re.sub(r"\s+", " ", plain).strip())
                        return "<h1%s></h1>" % attrs
                    t = re.sub(r'<h1(\b[^>]*\bhidden-heading\b[^>]*)>(.*?)</h1>', hide, t, flags=re.S)
                    t = re.sub(r"<hr />\s*((?:</section>\s*)+</body>)", r"\1", t)     # chapter separators
                    data = t.encode("utf-8")
            zo.writestr(item, data, compress_type=item.compress_type)
    os.replace(tmp, src)


def build_epub_finished(bd, cfg, fin):
    md = open(os.path.join(bd, "book.md"), encoding="utf-8").read()
    tmp_md = os.path.join(bd, ".book.finish.md")
    tmp_meta = os.path.join(bd, ".book.finish.json")
    css = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates", "masasilam.css")
    if not os.path.exists(css):
        sys.exit(f"finish: stylesheet missing: {css}")
    col_p = os.path.join(bd, fin.get("colophon_file", "colophon.html"))
    col = None
    if os.path.exists(col_p):
        col = open(col_p, encoding="utf-8").read().strip()
        print(f"colophon: using hand-edited {os.path.basename(col_p)}")
    lic_p = os.path.join(bd, fin.get("license_file", "license.html"))
    lic = None
    if os.path.exists(lic_p):
        lic = open(lic_p, encoding="utf-8").read().strip()
        print(f"license: using hand-edited {os.path.basename(lic_p)}")
    tp_p = os.path.join(bd, fin.get("titlepage_file", "titlepage.html"))
    tp = None
    if os.path.exists(tp_p):
        tp = open(tp_p, encoding="utf-8").read().strip()
        print(f"titlepage: using hand-edited {os.path.basename(tp_p)}")
    cfg_eff = {**cfg, **metadata_overrides(bd, cfg)}      # title/author from metadata.yaml if set
    open(tmp_md, "w", encoding="utf-8").write(finish_markdown(md, cfg_eff, fin, col, lic, tp))
    json.dump(finish_metadata(cfg, fin), open(tmp_meta, "w", encoding="utf-8"), ensure_ascii=False)
    epub = os.path.join(bd, cfg["epub_name"])
    cmd = ["pandoc", ".book.finish.md", "-o", epub, "--from", "markdown+pipe_tables-implicit_figures",
           "--to", "epub3", "--epub-cover-image=" + cover_path(cfg),
           "--metadata-file", ".book.finish.json", *metadata_file_args(bd, cfg), "--css", css,
           "--split-level=1", "--resource-path=.", "--toc", "--toc-depth=" + str(fin.get("toc_depth", 2)),
           # the finished edition has its own title page (ch001); drop pandoc's duplicate title_page.xhtml
           "--epub-title-page=false"]
    try:
        r = subprocess.run(cmd, cwd=bd, capture_output=True, text=True)
    finally:
        for f in (tmp_md, tmp_meta):
            if os.path.exists(f):
                os.remove(f)
    if r.returncode != 0 or "PandocResourceNotFound" in r.stderr:
        print("pandoc problems:\n" + r.stderr[:3000])
        sys.exit("build failed")
    postprocess_epub(epub, cfg, finished=True, bd=bd)
    print(f"EPUB (finished, MasasilaM style): {epub} ({os.path.getsize(epub) // 1024} KB)")


def build_epub(bd, cfg):
    if cfg.get("finish"):
        return build_epub_finished(bd, cfg, cfg["finish"])
    epub = os.path.join(bd, cfg["epub_name"])
    hand = read_metadata(bd, cfg)      # pandoc: --metadata beats --metadata-file, so flags are skipped for keys set in metadata.yaml
    cmd = ["pandoc", "book.md", "-o", epub, "--from", "markdown+pipe_tables", "--to", "epub3",
           "--epub-cover-image=" + cover_path(cfg),
           *([] if "title" in hand else ["--metadata", f"title={cfg['title']}"]),
           *(["--metadata", f"author={cfg['author']}"] if cfg.get("author") and "creator" not in hand and "author" not in hand else []),
           *([] if "lang" in hand else ["--metadata", f"lang={cfg['lang']}"]),
           "--split-level=1", "--resource-path=.", "--toc", "--toc-depth=1"]
    for key in ("publisher", "date", "description", "subject"):   # optional, from book.json
        if cfg.get(key) and key not in hand:
            cmd += ["--metadata", f"{key}={cfg[key]}"]
    if cfg.get("source_url") and "source" not in hand:             # dc:source (pandoc 'source' metadata)
        cmd += ["--metadata", f"source={cfg['source_url']}"]
    cmd += metadata_file_args(bd, cfg)
    r = subprocess.run(cmd, cwd=bd, capture_output=True, text=True)
    if r.returncode != 0 or "PandocResourceNotFound" in r.stderr:
        print("pandoc problems:\n" + r.stderr[:3000])
        sys.exit("build failed")
    postprocess_epub(epub, cfg, bd=bd)
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
    refs = set(re.findall(r"epub_assets/([\w.-]+\.(?:jpg|webp))", md)) | {os.path.basename(cover_path(cfg))}
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
        media = {x.split("/")[-1] for x in names if x.lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".svg"))}
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
    if cfg.get("corrections", {}).get("enforce"):
        # every change from the OCR baseline to book.md must be an entry in corrections.json (see corrections.py)
        import corrections
        try:
            fails += ["corrections: " + x for x in corrections.check(bd, scan=True)]
        except SystemExit as e:
            fails.append(str(e))
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
    ap.add_argument("--export-metadata", action="store_true",
                    help="write metadata.yaml pre-filled from the current config (hand-editable EPUB metadata)")
    a = ap.parse_args()
    bd = os.path.abspath(a.book_dir)
    if a.init:
        init_book(bd)
        return
    cfg = load_cfg(bd)
    configure(cfg)
    if a.export_metadata:
        export_metadata(bd, cfg)
        return
    steps = [s.strip() for s in a.steps.split(",") if s.strip()]
    if cfg.get("source", "ocr") == "book.md":
        if "ocr" in steps:
            print("source=book.md: skipping ocr step (book.md is the source)")
        steps = [s for s in steps if s != "ocr"]
    ok = True
    if "ocr" in steps:
        ok = ocr(bd, cfg, a.pages, a.force_ocr) and ok
        if not ok:
            sys.exit(1)
    if "images" in steps or "build" in steps or "verify" in steps:
        # md-source books don't need the OCR page files; verify() ignores n
        n = page_count(bd) if ("images" in steps or cfg.get("source", "ocr") != "book.md") else 0
    if "images" in steps:
        extract_images(bd, cfg, n)
    if "build" in steps:
        build(bd, cfg, n)
    if "verify" in steps:
        ok = verify(bd, cfg, n) and ok
    if "se" in steps:
        import se_edition   # only needed for this step; keeps `se`/cairo optional for everyone else
        cfg["_metadata"] = read_metadata(bd, cfg)
        ok = se_edition.build_se(bd, cfg) and ok
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
