#!/usr/bin/env python3
"""Standard Ebooks (manual 1.9.1) edition builder for the scanned-PDF pipeline.

Reads book.md (the single source of truth) and book.json "se" and produces, inside the book folder:
    se-src/<slug>/       a Standard Ebooks source tree (generated; safe to delete)
    se-out/              output of `se build`
    <epub_stem> (SE).epub  the final, Indonesian-localised EPUB

Run through the pipeline:   python3 pipeline.py <book_dir> --steps se

Adaptations for this pipeline (the `se` tool itself is never modified):
  * White-label: no Standard Ebooks logo, imprint claim or certification. The stock "Uncopyright"
    page says the text is US public domain; it is replaced by a licence page for the editorial work.
  * Indonesian: `se` hard-codes English labels (Table of Contents, Landmarks, Titlepage, Frontmatter)
    and `se lint` insists on them in the source, so the source stays SE-compliant and the labels are
    localised in the finished EPUB (labels table below, overridable in book.json se.labels).
  * Nav `aria-labelledby` is dropped in the output: epubcheck 5.4 rejects it, 5.3 (bundled with se) accepts it.
  * Text comes only from book.md. Anything this builder does not understand (tables, lists, verse, raw HTML)
    stops the build with a message instead of being guessed.
  * Gates: `se lint` must print nothing, `se build --check` must pass, a word-parity check of book.md
    against the generated body must show zero differences, and epubcheck (bundled and system) must report
    0 errors.

Toolchain: `uv tool install standardebooks`, `brew install cairo`, epubcheck optional.
"""
import glob, html, io, json, os, re, shutil, subprocess, sys, zipfile
from datetime import datetime, timezone

MONTHS_ID = ["Januari", "Februari", "Maret", "April", "Mei", "Juni", "Juli", "Agustus",
             "September", "Oktober", "November", "Desember"]

LABELS = {
    "id": {
        "Table of Contents": "Daftar Isi",
        "Landmarks": "Penanda",
        "Titlepage": "Halaman Judul",
        "Frontmatter": "Bagian Awal",
        "imprint": "Keterangan Edisi",
        "colophon": "Kolofon",
        "license": "Lisensi",
        "masthead": "Halaman Sumber",
        "by": "Oleh",
    },
}

NS = ('<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" '
      'epub:prefix="z3998: http://www.daisy.org/z3998/2012/vocab/structure/, '
      'se: https://standardebooks.org/vocab/1.0" xml:lang="%s">')


class SeError(SystemExit):
    pass


def fail(msg):
    raise SeError("SE edition: " + msg)


# ------------------------------------------------------------------ small helpers
def esc(s):
    return html.escape(s, quote=False)


def write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "w", encoding="utf-8").write(data)


def ind(block, n):
    return "\n".join(("\t" * n + l) if l.strip() else l for l in block.split("\n"))


def slugify(s):
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return s or "section"


def se_env():
    env = dict(os.environ)
    lib = "/opt/homebrew/lib"
    if os.path.isdir(lib):
        env["DYLD_FALLBACK_LIBRARY_PATH"] = lib + (":" + env["DYLD_FALLBACK_LIBRARY_PATH"] if env.get("DYLD_FALLBACK_LIBRARY_PATH") else "")
    return env


def run_se(*args, cwd=None, check=True):
    p = subprocess.run(["se", *args], cwd=cwd, env=se_env(), capture_output=True, text=True)
    if check and p.returncode != 0:
        fail("`se %s` failed (exit %d):\n%s%s" % (" ".join(args), p.returncode, p.stdout, p.stderr))
    return p


def se_package_dir():
    exe = shutil.which("se")
    if not exe:
        fail("`se` not found. Install with: uv tool install standardebooks  (and: brew install cairo)")
    first = open(str(exe), encoding="utf-8", errors="replace").readline()
    py = first[2:].strip() if first.startswith("#!") else sys.executable
    out = subprocess.run([py, "-c", "import se,os;print(os.path.dirname(se.__file__))"],
                         capture_output=True, text=True)
    return out.stdout.strip() if out.returncode == 0 else ""


# ------------------------------------------------------------------ typography (SE manual ch. 8, Indonesian text)
def typo(s):
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r'"\s*([^"]*?)\s*"', lambda m: "\u201c" + m.group(1).strip() + "\u201d", s)
    # single-quoted words become double quotes: SE uses single quotes only for nested quotation
    s = re.sub(r"(?<![A-Za-z])'([^']+?)'(?![A-Za-z])", lambda m: "\u201c" + m.group(1) + "\u201d", s)
    s = re.sub(r"\u201d\(", "\u201d (", s)
    s = re.sub(r"\u201d([.,])", lambda m: m.group(1) + "\u201d", s)  # American style: . and , inside quotes
    s = re.sub(r"\s+([?!,.;:])", r"\1", s)
    return s


class Inline:
    """Escape + semantic markup driven by book.json se.{acronyms,name_titles,foreign,italic_names}."""

    def __init__(self, se):
        self.acronyms = se.get("acronyms", [])
        self.name_titles = se.get("name_titles", [])
        self.foreign = se.get("foreign", {})  # {"en": ["volunteer", ...]}
        self.italic_names = se.get("italic_names", [])  # [{"text": "...", "type": "se:name.publication.magazine"}]

    def __call__(self, s):
        s = esc(typo(s))
        for t in self.name_titles:
            s = re.sub(r"(?<=\w) %s\b" % re.escape(t), ' <abbr epub:type="z3998:name-title">%s</abbr>' % t, s)
        for ab in self.acronyms:
            s = re.sub(r"\b%s\b(?=[^<]|$)" % re.escape(ab), '<abbr epub:type="z3998:acronym">%s</abbr>' % ab, s)
        for lang, words in self.foreign.items():
            for w in words:
                s = re.sub(r"(?<![\u201c\u2018\w>])%s(?![\w\u201d\u2019<])" % re.escape(w),
                           '<i xml:lang="%s">%s</i>' % (lang, w), s)
        for n in self.italic_names:
            s = s.replace(n["text"], '<i epub:type="%s">%s</i>' % (n.get("type", "se:name.publication.book"), n["text"]))
        return s


# ------------------------------------------------------------------ book.md parsing
class Block:
    def __init__(self, kind, text, level=0):
        self.kind, self.text, self.level = kind, text, level


def parse_chapters(md, cfg, se):
    """-> (front_blocks, [(title, eyebrow, [Block...])]). Front matter = everything before the first
    real chapter; real chapters are the level-1 headings other than book.json front_matter.heading."""
    front_heading = (cfg.get("front_matter") or {}).get("heading", "")
    parts = re.split(r"(?m)^# (.+)$", md)
    front, chapters = [], []
    for i in range(1, len(parts), 2):
        title, body = parts[i].strip(), parts[i + 1]
        blocks, eyebrow = [], None
        for raw in [b.strip() for b in body.split("\n\n") if b.strip()]:
            if re.fullmatch(r"-{3,}", raw):
                continue
            blocks.append(raw)
        if title == front_heading:
            front = blocks
        else:
            chapters.append((title, blocks))
    out = []
    qa = se.get("qa", False)
    for title, raws in chapters:
        eyebrow, blocks = None, []
        for raw in raws:
            if re.fullmatch(r"\*[^*\n]+\*", raw) and eyebrow is None and not blocks:
                eyebrow = raw.strip("*")
                continue
            m = re.match(r"(#{2,6}) +(.+)$", raw, re.S)
            if m:
                text = m.group(2).strip()
                if qa and text.endswith("?"):  # OCR promoted a question to a heading
                    blocks.append(Block("question", text))
                else:
                    blocks.append(Block("heading", text, len(m.group(1))))
                continue
            if re.fullmatch(r"!\[[^\]]*\]\(([^)]+)\)(\{[^}]*\})?", raw):
                mm = re.match(r"!\[[^\]]*\]\(([^)]+)\)", raw)
                blocks.append(Block("image", mm.group(1) if mm else raw))
                continue
            if raw.startswith(("<", "|", "```", ":::")) or re.match(r"(?m)^([-*+] |\d+\. )", raw) or "\\\n" in raw:
                fail("unsupported construct in chapter %r (table/list/verse/raw HTML): %r. "
                     "Extend se_edition.py or simplify book.md." % (title, raw[:70]))
            if re.fullmatch(r"\d+ dari \d+ halaman", raw):  # scanner page footer
                continue
            blocks.append(Block("question" if (qa and raw.endswith("?")) else "p", raw))
        out.append((title, eyebrow, blocks))
    return front, out


def join_split_paragraphs(blocks):
    """A paragraph that ends mid-sentence (no terminal punctuation) and is followed by a lowercase start
    is a page-break split from the scan: join them. Only for plain paragraphs."""
    res = []
    for b in blocks:
        if (res and b.kind == "p" and res[-1].kind == "p"
                and not re.search(r"[.?!:\"\u201d)\]]\s*$", res[-1].text) and re.match(r"[a-z]", b.text)):
            res[-1].text = res[-1].text.rstrip() + " " + b.text
        else:
            res.append(b)
    return res


# ------------------------------------------------------------------ page builders
def page(title, body_type, inner, css=("core.css", "local.css"), lang="id"):
    links = "".join('\n\t\t<link href="../css/%s" rel="stylesheet" type="text/css"/>' % c for c in css)
    return ('<?xml version="1.0" encoding="utf-8"?>\n%s\n\t<head>\n\t\t<title>%s</title>%s\n\t</head>\n'
            '\t<body epub:type="%s">\n%s\n\t</body>\n</html>\n') % (NS % lang, esc(title), links, body_type, inner)


def chapter_xhtml(cfg, se, inline, idx, n_chapters, title, blocks, images, front_epigraph):
    single = n_chapters == 1
    work = se["work"]
    cid = work if single else "chapter-%d" % (idx + 1)
    parts, stack = [], []

    def close_to(level):
        while stack and stack[-1] >= level:
            stack.pop()
            parts.append("</section>")

    for b in blocks:
        if b.kind == "heading":
            close_to(b.level)
            hid = slugify(b.text)
            parts.append('<section id="%s" epub:type="z3998:subchapter">' % hid)
            parts.append('\t<h%d epub:type="title">%s</h%d>' % (b.level + 1, esc(typo(b.text)), b.level + 1))
            stack.append(b.level)
        elif b.kind == "image":
            n = len(images) + 1
            src = b.text
            alt = (se.get("image_alt") or {}).get(os.path.basename(src))
            if not alt:
                fail("no alt text for image %s: add it to book.json se.image_alt (prose description, ends with a period)" % src)
            images.append((src, "illustration-%d.jpg" % n))
            parts.append('<figure id="illustration-%d">\n\t<img alt="%s" src="../images/illustration-%d.jpg" epub:type="z3998:illustration"/>\n</figure>'
                         % (n, esc(alt), n))
        elif b.kind == "question":
            parts.append('<p class="question">%s</p>' % inline(b.text))
        else:
            parts.append("<p>%s</p>" % inline(b.text))
    close_to(0)

    sub = se.get("subtitle") if single else None
    if single:
        title = se["title"]
    if sub:
        h = '<hgroup>\n\t<h2 epub:type="title">%s</h2>\n\t<p epub:type="subtitle">%s</p>\n</hgroup>' % (esc(title), esc(sub))
    else:
        h = '<h2 epub:type="title">%s</h2>' % esc(title)
    if front_epigraph and idx == 0:
        h = ('<header>\n%s\n\t<blockquote epub:type="epigraph">\n\t\t<p>%s</p>\n\t\t<cite>%s</cite>\n\t</blockquote>\n</header>'
             % (ind(h, 1), inline(front_epigraph["text"]), esc(front_epigraph["cite"])))
    sec = '<section id="%s" epub:type="chapter">\n%s\n%s\n</section>' % (cid, ind(h, 1), ind("\n".join(parts), 1))
    return cid, page(title, "bodymatter", ind(sec, 2)), sec


def masthead_page(front, se, inline, images, labels):
    """The source's own front page (book.md front matter, e.g. a magazine masthead) shown as printed, in order.
    Headings listed in se.masthead.sections become sub-sections (h3); every other heading is a plain masthead line.
    -> (xhtml, expected_tokens)"""
    sections = set((se.get("masthead") or {}).get("sections", []))
    parts, exp, in_sec = [], [], False

    def close():
        nonlocal in_sec
        if in_sec:
            parts.append("</section>")
            in_sec = False

    for raw in front:
        m = re.match(r"(#{2,6}) +(.+)$", raw, re.S)
        img = re.fullmatch(r"!\[[^\]]*\]\(([^)]+)\)(\{[^}]*\})?", raw)
        if m:
            text = m.group(2).strip()
            exp += tokens(text)
            if text in sections:
                close()
                parts.append('<section id="%s" epub:type="z3998:subchapter">' % slugify(text))
                parts.append('\t<h3 epub:type="title">%s</h3>' % esc(typo(text)))
                in_sec = True
            else:
                close()
                parts.append('<p class="masthead">%s</p>' % inline(text))
        elif img:
            close()
            src = re.match(r"!\[[^\]]*\]\(([^)]+)\)", raw).group(1)
            alt = (se.get("image_alt") or {}).get(os.path.basename(src))
            if not alt:
                fail("no alt text for image %s: add it to book.json se.image_alt" % src)
            n = len(images) + 1
            images.append((src, "illustration-%d.jpg" % n))
            parts.append('<figure id="illustration-%d">\n\t<img alt="%s" src="../images/illustration-%d.jpg" epub:type="z3998:illustration"/>\n</figure>'
                         % (n, esc(alt), n))
        else:
            if raw.startswith(("<", "|", "```", ":::")) or re.match(r"(?m)^([-*+] |\d+\. )", raw):
                fail("unsupported construct in book.md front matter: %r" % raw[:70])
            exp += tokens(raw)
            parts.append("<p>%s</p>" % inline(raw))
    close()
    title = labels["masthead"]
    body = '<section id="masthead" epub:type="chapter">\n\t<h2 epub:type="title">%s</h2>\n%s\n</section>' % (
        esc(title), ind("\n".join(parts), 1))
    return page(title, "bodymatter", ind(body, 2)), exp


SEED_HEADER = """<!-- {what}
Edit freely: the text below is the text in the book (no hidden values). Rebuild: python3 pipeline.py <book_dir> --steps se
Delete this file to regenerate it from the template. This comment is not shown in the book.
Format: one paragraph per blank line. *italic*  **bold**  [text](https://url)  "quotes" become curly quotes.
A line ending in two spaces or a backslash is a line break. Raw inline HTML is passed through (the colophon uses it
for Standard Ebooks semantics). Optional placeholders, filled from book.json at build time if you type them:
{{title}} {{author}} {{producer}} {{source_label}} {{date}} {{iso}} {{license}} -->

"""

SEEDS = {
    "imprint": ("Keterangan Edisi (halaman imprint, awal buku)",
                "Edisi digital ini disusun oleh {producer} dari {source_label}. Teks dibaca dengan OCR, lalu diperiksa dengan "
                "pemeriksa ejaan dan dikoreksi pada kesalahan OCR yang jelas.\n\n"
                "Hak cipta atas teks berada pada penulis dan/atau pemegang hak masing-masing; edisi ini tidak mengklaim hak atas "
                "isi tersebut. Kontribusi penyuntingan edisi digital ini dilepaskan ke domain publik di bawah "
                "[CC0 1.0](https://creativecommons.org/publicdomain/zero/1.0/deed.id). "
                "Keterangan lengkap ada di halaman [{license}](uncopyright.xhtml) pada akhir buku.\n"),
    "colophon": ("Kolofon (akhir buku)",
                 '<i epub:type="se:name.publication.book" xml:lang="id">{title}</i>  \noleh  \n'
                 '<b epub:type="z3998:personal-name">{author}</b>.\n\n'
                 "Edisi digital ini disusun oleh  \n**{producer}**  \ndari {source_label}.\n\n"
                 'Sampul menggunakan huruf  \n<b epub:type="se:name.visual-art.typeface">League Spartan</b>,  \ndirancang oleh  \n'
                 "[The League of Moveable Type](https://www.theleagueofmoveabletype.com/).\n\n"
                 'Edisi digital ini pertama dirilis pada  \n<time datetime="{iso}">{date}</time>.\n'),
    "license": ("Lisensi (akhir buku)",
                "Kontribusi penyuntingan edisi digital ini, termasuk hasil OCR, pemeriksaan dan koreksi naskah, penataan huruf, "
                "struktur EPUB, dan navigasi, dilepaskan ke domain publik di bawah "
                "[CC0 1.0](https://creativecommons.org/publicdomain/zero/1.0/deed.id).\n\n"
                "Pelepasan ini tidak mencakup teks, foto, maupun isi lain yang menjadi dasar edisi ini, yang tetap menjadi milik "
                "para pemegang haknya masing-masing. Edisi ini tidak mengklaim hak atas isi tersebut.\n"),
}

_TAG = re.compile(r"(<[^>]+>)")


def curly(s):
    """Quote typography only (SE manual ch. 8); leaves spacing alone so inline markup keeps its spaces."""
    s = re.sub(r'"([^"\n]*?)"', lambda m: "\u201c" + m.group(1) + "\u201d", s)
    s = re.sub(r"(?<![A-Za-z])'([^'\n]+?)'(?![A-Za-z])", lambda m: "\u201c" + m.group(1) + "\u201d", s)
    s = re.sub(r"\u201d([.,])", lambda m: m.group(1) + "\u201d", s)
    return s


def md_para(text):
    """One markdown paragraph -> inline XHTML. Subset: *i* **b** [t](u), line breaks, curly quotes, raw inline HTML."""
    lines = text.strip("\n").split("\n")
    out = []
    for i, ln in enumerate(lines):
        hard = ln.endswith("  ") or ln.endswith("\\")
        ln = ln.rstrip().rstrip("\\").rstrip()
        out.append(ln + ("\x01" if hard and i < len(lines) - 1 else (" " if i < len(lines) - 1 else "")))
    t = "".join(out)
    parts = _TAG.split(t)
    for i in range(0, len(parts), 2):                     # text segments only; tags (odd indexes) pass through
        seg = re.sub(r"&(?!#?\w+;)", "&amp;", parts[i])
        seg = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", lambda m: "\x02%s\x03%s\x04" % (m.group(2), m.group(1)), seg)
        seg = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", seg)
        seg = re.sub(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])", r"<i>\1</i>", seg)
        seg = curly(seg)
        parts[i] = seg
    t = "".join(parts)
    t = re.sub(r"\x02([^\x03]+)\x03", r'<a href="\1">', t).replace("\x04", "</a>").replace("\x01", "<br/>\n")
    return t.strip()


def page_paras(bd, kind, cfg, se, labels, subs):
    """Body paragraphs of a SE front/back page from <book_dir>/se-<kind>.md. The file is created from SEEDS on first
    run so it can be edited by hand; legacy se-<kind>.html (raw <p> text) is still read if present."""
    md_p, html_p = (os.path.join(bd, "se-%s.%s" % (kind, ext)) for ext in ("md", "html"))
    if not os.path.exists(md_p) and not os.path.exists(html_p):
        what, body = SEEDS[kind]
        for k in sorted(subs, key=len, reverse=True):      # write the real text into the file: what you see is what you edit
            body = body.replace("{" + k + "}", subs[k])
        open(md_p, "w", encoding="utf-8").write(SEED_HEADER.format(what=what) + body)
        print("se: created %s (edit it by hand; delete it to regenerate)" % os.path.basename(md_p))
    path = md_p if os.path.exists(md_p) else html_p
    txt = re.sub(r"<!--.*?-->", "", open(path, encoding="utf-8").read(), flags=re.S)
    for k in sorted(subs, key=len, reverse=True):
        txt = txt.replace("{" + k + "}", subs[k])
    unknown = sorted(set(re.findall(r"\{[a-z_]+\}", txt)))
    if unknown:
        fail("%s uses unknown placeholder(s) %s; known: %s" % (os.path.basename(path), ", ".join(unknown), ", ".join("{%s}" % k for k in sorted(subs))))
    blocks = [b.strip("\n") for b in re.split(r"\n\s*\n", txt) if b.strip()]
    print("se: %s from hand-editable %s" % (kind, os.path.basename(path)))
    return [b.strip() if path == html_p else md_para(b) for b in blocks]


def paras_html(paras):
    return "\n".join("\t<p>%s</p>" % p for p in paras)


# ------------------------------------------------------------------ content.opf
def opf(cfg, se, words, now):
    def meta(prop, val, refines=None, scheme=None):
        r = ' refines="#%s"' % refines if refines else ""
        s = ' scheme="%s"' % scheme if scheme else ""
        return '\t\t<meta property="%s"%s%s>%s</meta>' % (prop, r, s, esc(val))

    L = []
    pub = se["publisher"]
    title, sub = se["title"], se.get("subtitle")
    L += ['\t\t<dc:identifier id="uid">%s/%s</dc:identifier>' % (slugify(se["author"]), se["work"]),
          "\t\t<dc:date>%s</dc:date>" % now,
          meta("dcterms:modified", now),
          "\t\t<dc:rights>%s</dc:rights>" % esc(se["rights"]),
          meta("rdf:type", "http://schema.org/Book"),
          '\t\t<dc:publisher id="publisher">%s</dc:publisher>' % esc(pub),
          meta("file-as", pub, "publisher"), meta("role", "pbl", "publisher", "marc:relators")]
    L += [meta("schema:accessMode", "textual"), meta("schema:accessModeSufficient", "textual")]
    for f in ("displayTransformability", "readingOrder", "structuralNavigation", "tableOfContents"):
        L.append(meta("schema:accessibilityFeature", f))
    if se.get("has_images"):
        L.append(meta("schema:accessibilityFeature", "alternativeText"))
    L.append(meta("schema:accessibilityHazard", "none"))
    L += ['\t\t<dc:title id="title">%s</dc:title>' % esc(title), meta("file-as", title, "title")]
    if sub:
        L += [meta("title-type", "main", "title"),
              '\t\t<dc:title id="subtitle">%s</dc:title>' % esc(sub), meta("file-as", sub, "subtitle"),
              meta("title-type", "subtitle", "subtitle"),
              '\t\t<dc:title id="fulltitle">%s: %s</dc:title>' % (esc(title), esc(sub)),
              meta("file-as", "%s: %s" % (title, sub), "fulltitle"), meta("title-type", "expanded", "fulltitle")]
    for i, (label, term) in enumerate(se["subjects"], 1):
        L.append('\t\t<dc:subject id="subject-%d">%s</dc:subject>' % (i, esc(label)))
    for i, (label, term) in enumerate(se["subjects"], 1):
        L += [meta("authority", "LCSH", "subject-%d" % i), meta("term", term, "subject-%d" % i)]
    L += [meta("schema:genre", se.get("genre", "Nonfiction")), meta("schema:abstract", se["abstract"]),
          "\t\t<dc:description>\n\t\t\t%s\n\t\t</dc:description>" % se["description_html"],
          "\t\t<dc:language>%s</dc:language>" % cfg.get("lang", "id")]
    for s in se.get("sources", []):
        L.append("\t\t<dc:source>%s</dc:source>" % esc(s))
    L.append(meta("schema:wordCount", str(words)))
    a = se["author"]
    L += ['\t\t<dc:creator id="author">%s</dc:creator>' % esc(a), meta("file-as", se.get("author_file_as", a), "author")]
    if se.get("author_full_name") and se["author_full_name"] != a:
        L.append(meta("schema:alternateName", se["author_full_name"], "author"))
    for u in (se.get("author_wikipedia"), se.get("author_loc")):
        if u:
            L.append('\t\t<link href="%s" refines="#author" rel="schema:sameAs"/>' % esc(u))
    L.append(meta("role", "aut", "author", "marc:relators"))
    for c in se.get("contributors", []):
        L += ['\t\t<dc:contributor id="%s">%s</dc:contributor>' % (c["id"], esc(c["name"])),
              meta("file-as", c.get("file_as", c["name"]), c["id"])]
        if c.get("wikipedia"):
            L.append('\t\t<link href="%s" refines="#%s" rel="schema:sameAs"/>' % (esc(c["wikipedia"]), c["id"]))
        L.append(meta("role", c["role"], c["id"], "marc:relators"))
    pr = se["producer"]
    L += ['\t\t<dc:contributor id="producer-1">%s</dc:contributor>' % esc(pr["name"]),
          meta("file-as", pr.get("file_as", pr["name"]), "producer-1")]
    if pr.get("url"):
        L.append('\t\t<link href="%s" refines="#producer-1" rel="schema:url"/>' % esc(pr["url"]))
    L.append(meta("role", "bkp", "producer-1", "marc:relators"))
    if se.get("has_images"):
        L.append(meta("role", "wat", "producer-1", "marc:relators"))  # SE m-040: writer of the alt text
    return ('<?xml version="1.0" encoding="utf-8"?>\n'
            '<package xmlns="http://www.idpf.org/2007/opf" dir="ltr" prefix="rdf: http://www.w3.org/1999/02/22-rdf-syntax-ns#" '
            'unique-identifier="uid" version="3.0" xml:lang="%s">\n\t<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n%s\n\t</metadata>\n'
            '\t<manifest>\n\t\t<item href="toc.xhtml" id="toc.xhtml" media-type="application/xhtml+xml" properties="nav"/>\n\t</manifest>\n'
            '\t<spine>\n\t\t<itemref idref="titlepage.xhtml"/>\n\t</spine>\n</package>\n') % (cfg.get("lang", "id"), "\n".join(L))


# ------------------------------------------------------------------ cover
def make_cover(bd, se, dest):
    from PIL import Image, ImageDraw, ImageFont, ImageOps
    if se.get("cover_image"):
        im = Image.open(os.path.join(bd, se["cover_image"])).convert("RGB")
        ImageOps.fit(im, (1400, 2100), getattr(Image, "Resampling", Image).LANCZOS).save(dest, quality=90)
        return
    pkg = se_package_dir()
    fonts = glob.glob(os.path.join(pkg, "data", "fonts", "league_spartan", "league-spartan-bold.otf"))
    if not fonts:
        fail("League Spartan font not found in the se package")
    im = Image.new("RGB", (1400, 2100), tuple(se.get("cover_color", (38, 50, 62))))
    d = ImageDraw.Draw(im)
    f1, f2 = ImageFont.truetype(fonts[0], 96), ImageFont.truetype(fonts[0], 64)
    lines, cur = [], ""
    for w in se.get("cover_title", se.get("work_title", "")).upper().split():
        t = (cur + " " + w).strip()
        if d.textlength(t, font=f1) > 1200 and cur:
            lines.append(cur)
            cur = w
        else:
            cur = t
    lines.append(cur)
    y = 760 - 65 * len(lines)
    for line in lines:
        d.text(((1400 - d.textlength(line, font=f1)) / 2, y), line, font=f1, fill=(255, 255, 255))
        y += 130
    au = se["author"].upper()
    d.text(((1400 - d.textlength(au, font=f2)) / 2, y + 120), au, font=f2, fill=(255, 255, 255))
    im.save(dest, quality=90)


MASTHEAD_CSS = '''
p.masthead{
\tfont-weight: bold;
\tmargin: 1em auto;
\ttext-align: center;
\ttext-indent: 0;
}
'''

ACR_CSS = '''
abbr[epub|type~="z3998:acronym"]{
\tfont-variant: all-small-caps;
}
'''

LOCAL_CSS = '''@charset "utf-8";
@namespace epub "http://www.idpf.org/2007/ops";

section[epub|type~="titlepage"] p{
\ttext-align: center;
\ttext-indent: 0;
}

section[epub|type~="imprint"] p,
section[epub|type~="colophon"] p,
section[epub|type~="copyright-page"] p{
\tmargin: 1em auto;
\ttext-indent: 0;
}

section[epub|type~="colophon"],
section[epub|type~="imprint"]{
\ttext-align: center;
}
''' + ACR_CSS

QA_CSS = '''
p.question{
\tfont-weight: bold;
\tmargin-top: 1em;
\ttext-indent: 0;
}
'''

EPIGRAPH_CSS = '''
/* Epigraphs */
[epub|type~="epigraph"]{
\tfont-style: italic;
}

[epub|type~="epigraph"] cite{
\tfont-style: normal;
\tfont-variant: small-caps;
\tmargin-top: 1em;
}

/* Epigraphs in section headers */
section > header [epub|type~="epigraph"]{
\tdisplay: inline-block;
\tmargin: auto;
\tmargin-bottom: 3em;
\tmax-width: 80%;
\ttext-align: initial;
}

@supports(display: table){
\tsection > header [epub|type~="epigraph"]{
\t\tdisplay: table;
\t}
}
/* End epigraphs in section headers */
'''

FIGURE_CSS = '''
figure{
\tbreak-inside: avoid;
\tmargin: 1em 2.5em;
}

figure img{
\tdisplay: block;
\tmargin: auto;
\tmax-height: 100vh;
\tmax-width: 100%;
}
'''


def write_ignores(root, ignores):
    x = ['<?xml version="1.0" encoding="utf-8"?>', "<se-lint-ignore>"]
    for fpath in dict.fromkeys(i[0] for i in ignores):
        x.append('\t<file path="%s">' % fpath)
        for f2, code, reason in ignores:
            if f2 == fpath:
                x += ['\t\t<ignore code="%s">' % code, "\t\t\t<reason>%s</reason>" % esc(reason), "\t\t</ignore>"]
        x.append("\t</file>")
    x.append("</se-lint-ignore>")
    write(os.path.join(root, "se-lint-ignore.xml"), "\n".join(x) + "\n")


def prune_ignores(root, ignores):
    """`se lint` reports unused ignore rules as m-048. Lint once with every rule, drop the unused ones."""
    write_ignores(root, ignores)
    p = run_se("--plain", "lint", root, check=False)
    unused = set(re.findall(r"^\s*Line \d+:\s+(\S+): ([a-z]-\d+)\s*$", p.stdout + p.stderr, re.M))
    kept = [i for i in ignores if (i[0], i[1]) not in unused]
    if len(kept) != len(ignores):
        write_ignores(root, kept)
    return kept


# ------------------------------------------------------------------ word parity
def tokens(s):
    s = html.unescape(re.sub(r"<[^>]+>", " ", s))
    s = re.sub("[\u201c\u201d\u2018\u2019\"']", "", s)
    return re.findall(r"\w+", s.lower())


def parity(chapters, front_epigraph, body_files, se, extra_expected=()):
    """book.md chapter text vs generated XHTML: normalised word lists must be identical."""
    exp = list(extra_expected)
    for title, eyebrow, blocks in chapters:
        for b in blocks:
            if b.kind != "image":
                exp += tokens(b.text)
    got = []
    for f in body_files:
        x = open(f, encoding="utf-8").read()
        x = re.sub(r"<title>.*?</title>", "", x, flags=re.S)
        x = re.sub(r"<hgroup>.*?</hgroup>", "", x, flags=re.S)
        x = re.sub(r'<blockquote epub:type="epigraph">.*?</blockquote>', "", x, flags=re.S)
        x = re.sub(r"<h2 epub:type=\"title\">.*?</h2>", "", x, flags=re.S)
        got += tokens(x)
    import difflib
    diffs = [(op, exp[i1:i2], got[j1:j2]) for op, i1, i2, j1, j2 in
             difflib.SequenceMatcher(None, exp, got, autojunk=False).get_opcodes() if op != "equal"]
    return len(exp), len(got), diffs


# ------------------------------------------------------------------ localisation of the finished EPUB
def localise_epub(src, dst, cfg, se, labels):
    zin = zipfile.ZipFile(src)
    names = zin.namelist()
    with zipfile.ZipFile(dst, "w") as zout:
        for n in ["mimetype"] + [x for x in names if x != "mimetype"]:
            data = zin.read(n)
            if n.endswith(("toc.xhtml", "toc.ncx", "titlepage.xhtml")):
                t = data.decode("utf-8")
                if n.endswith("toc.xhtml"):
                    t = re.sub(r'(<nav )aria-labelledby="[^"]*" ', r"\1", t)  # epubcheck 5.4 rejects it
                for en in ("Table of Contents", "Landmarks", "Titlepage", "Frontmatter"):
                    t = re.sub(r">%s<" % en, ">%s<" % labels[en], t)
                    t = t.replace("<title>%s</title>" % en, "<title>%s</title>" % labels[en])
                data = t.encode("utf-8")
            zout.writestr(n, data, zipfile.ZIP_STORED if n == "mimetype" else zipfile.ZIP_DEFLATED)


def epubcheck_all(epub):
    ok = True
    cmds = []
    pkg = se_package_dir()
    jar = os.path.join(pkg, "data", "epubcheck", "epubcheck.jar") if pkg else ""
    if jar and os.path.exists(jar):
        cmds.append(("epubcheck (bundled with se)", ["java", "-jar", jar, epub]))
    if shutil.which("epubcheck"):
        cmds.append(("epubcheck (system)", ["epubcheck", epub]))
    for name, cmd in cmds:
        p = subprocess.run(cmd, capture_output=True, text=True)
        m = re.search(r"Messages: (\d+) fatals / (\d+) errors / (\d+) warnings", p.stdout + p.stderr)
        res = "?" if not m else "%s fatals / %s errors / %s warnings" % m.groups()
        print("  %s: %s" % (name, res))
        if not m or int(m.group(1)) or int(m.group(2)) or int(m.group(3)):
            ok = False
            print((p.stdout + p.stderr)[-1500:])
    if not cmds:
        print("  no epubcheck available; skipped")
    return ok


# ------------------------------------------------------------------ main entry
def build_se(bd, cfg):
    se = cfg.get("se")
    if not se:
        fail('book.json has no "se" block (see README: Standard Ebooks edition)')
    lang = cfg.get("lang", "id")
    if lang not in LABELS:
        fail("only lang=id labels are defined; add LABELS[%r]" % lang)
    labels = {**LABELS[lang], **se.get("labels", {})}
    for k in ("author", "publisher", "producer", "subjects", "abstract", "description_html", "rights"):
        if not se.get(k):
            fail("book.json se.%s is required" % k)
    if not se.get("title"):
        main, _, rest = cfg["title"].partition(": ")
        se["title"] = main
        if rest and not se.get("subtitle"):
            se["subtitle"] = rest
    se.setdefault("work", slugify(se["title"]))
    slug = "%s_%s" % (slugify(se["author"]), se["work"])
    root = os.path.join(bd, "se-src", slug)
    epub_dir = os.path.join(root, "src", "epub")
    text_dir = os.path.join(epub_dir, "text")

    md = open(os.path.join(bd, "book.md"), encoding="utf-8").read()
    front, chapters = parse_chapters(md, cfg, se)
    if not chapters:
        fail("no chapters found in book.md")
    chapters = [(t, e, join_split_paragraphs(bl)) for t, e, bl in chapters]

    epigraph = None
    if se.get("epigraph"):
        h = se["epigraph"]["heading"]
        txt = None
        for i, raw in enumerate(front):
            if raw.lstrip("#").strip() == h and i + 1 < len(front):
                txt = front[i + 1]
        if not txt:
            fail("epigraph heading %r not found in book.md front matter" % h)
        epigraph = {"text": txt, "cite": se["epigraph"]["cite"]}

    # release timestamp: recorded once so rebuilds do not change the colophon
    path_cfg = os.path.join(bd, "book.json")
    if not se.get("released"):
        se["released"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        raw = json.load(open(path_cfg, encoding="utf-8"))
        raw.setdefault("se", {})["released"] = se["released"]
        json.dump(raw, open(path_cfg, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        print("se: recorded first release time in book.json se.released")
    rel = datetime.strptime(se["released"], "%Y-%m-%dT%H:%M:%SZ")
    when = {"iso": se["released"], "id": "%d %s %d" % (rel.day, MONTHS_ID[rel.month - 1], rel.year)}

    if os.path.isdir(os.path.join(bd, "se-src")):
        shutil.rmtree(os.path.join(bd, "se-src"))
    os.makedirs(os.path.join(bd, "se-src"))
    run_se("create-draft", "--offline", "-a", se["author"], "-t", se["title"], cwd=os.path.join(bd, "se-src"))
    for f in ("src/epub/images/logo.svg", "src/epub/images/cover.svg", "src/epub/images/titlepage.svg"):
        if os.path.exists(os.path.join(root, f)):
            os.remove(os.path.join(root, f))
    shutil.rmtree(os.path.join(root, "images"), ignore_errors=True)  # SE branding sources, not used here
    for f in os.listdir(text_dir):
        os.remove(os.path.join(text_dir, f))

    inline = Inline(se)
    images, body_files, words = [], [], 0
    masthead_exp, masthead_file = [], None
    if se.get("masthead", {}).get("enabled", True) and front:
        mx, masthead_exp = masthead_page(front, se, inline, images, labels)
        masthead_file = os.path.join(text_dir, "masthead.xhtml")
        write(masthead_file, mx)
    for i, (title, eyebrow, blocks) in enumerate(chapters):
        cid, xhtml, sec = chapter_xhtml(cfg, se, inline, i, len(chapters), title, blocks, images, epigraph)
        write(os.path.join(text_dir, cid + ".xhtml"), xhtml)
        body_files.append(os.path.join(text_dir, cid + ".xhtml"))
        words += len(re.sub(r"<[^>]+>", " ", sec).split())
    se["has_images"] = bool(images)
    if images:
        from PIL import Image
        for src, name in images:
            p = os.path.join(bd, src)
            if not os.path.exists(p):
                fail("image not found: %s" % p)
            Image.open(p).convert("RGB").save(os.path.join(epub_dir, "images", name), quality=88)

    write(os.path.join(text_dir, "titlepage.xhtml"), page("Titlepage", "frontmatter", ind(
        '<section id="titlepage" epub:type="titlepage">\n\t<h1 epub:type="title">%s</h1>\n'
        '\t<p>%s <b epub:type="z3998:author z3998:personal-name">%s</b>.</p>\n</section>'
        % (esc(se["title"]), labels["by"], esc(se["author"])), 2)))
    subs = {"date": when["id"], "iso": when["iso"], "producer": esc(se["producer"]["name"]), "title": esc(se["title"]),
            "author": esc(se["author"]), "source_label": esc(se.get("source_label") or "pindaian PDF"), "license": labels["license"]}
    imprint = page_paras(bd, "imprint", cfg, se, labels, subs)
    write(os.path.join(text_dir, "imprint.xhtml"), page(labels["imprint"], "frontmatter", ind(
        '<section id="imprint" epub:type="imprint">\n\t<h2 epub:type="title">%s</h2>\n%s\n</section>'
        % (labels["imprint"], paras_html(imprint)), 2)))
    colophon = page_paras(bd, "colophon", cfg, se, labels, subs)
    write(os.path.join(text_dir, "colophon.xhtml"), page(labels["colophon"], "backmatter", ind(
        '<section id="colophon" epub:type="colophon">\n\t<h2 epub:type="title">%s</h2>\n%s\n</section>'
        % (labels["colophon"], paras_html(colophon)), 2)))
    lic = page_paras(bd, "license", cfg, se, labels, subs)
    write(os.path.join(text_dir, "uncopyright.xhtml"), page(labels["license"], "backmatter", ind(
        '<section id="uncopyright" epub:type="copyright-page">\n\t<h2 epub:type="title">%s</h2>\n%s\n</section>'
        % (labels["license"], paras_html(lic)), 2)))

    used_acr = any("z3998:acronym" in open(f, encoding="utf-8").read() for f in body_files)
    css = (LOCAL_CSS if used_acr else LOCAL_CSS.replace(ACR_CSS, "")) + (QA_CSS if se.get("qa") else "") + (EPIGRAPH_CSS if epigraph else "") + (MASTHEAD_CSS if masthead_file else "") + (FIGURE_CSS if images else "")
    write(os.path.join(epub_dir, "css", "local.css"), css)
    se.setdefault("cover_title", se["title"])
    write(os.path.join(epub_dir, "content.opf"), opf(cfg, se, words, se["released"]))
    make_cover(bd, se, os.path.join(epub_dir, "images", "cover.jpg"))

    notes = ["# Catatan produksi", "",
             "- Sumber dan keterangan edisi: lihat `se-imprint.md` dan `se-colophon.md` (dapat disunting langsung). Teks berasal dari OCR (TurboOCR), bukan dari ketik ulang.",
             "- Naskah dibaca langsung dari `book.md`; edisi ini dibangun ulang dengan `pipeline.py --steps se`.",
             "- Koreksi OCR hanya pada kesalahan yang jelas (pemeriksa ejaan SymSpell, ditinjau manual). Ejaan lama dan tidak baku dari narasumber dipertahankan.",
             "- Kutip lurus dan kutip tunggal diubah menjadi kutip lengkung ganda sesuai manual SE; titik dan koma ditaruh di dalam tanda kutip.",
             "- Singkatan ditulis tanpa titik sesuai kaidah Indonesia dan ditandai z3998:acronym; aturan lint s-023 (kapitalisasi judul gaya Inggris) tidak berlaku untuk bahasa Indonesia.",
             "- Edisi white-label, bukan rilis Standard Ebooks: tanpa logo atau klaim SE. Halaman lisensi menggantikan Uncopyright SE karena teks sumber tidak berada di domain publik.",
             "- Label navigasi (Daftar Isi, Penanda, Halaman Judul) diterjemahkan setelah `se build`; sumber tetap sesuai SE agar `se lint` lolos."]
    notes += ["- " + n for n in se.get("production_notes", [])]
    write(os.path.join(root, "production-notes.md"), "\n".join(notes) + "\n")

    # Ignore rules that do not match make `se lint` fail (m-048), so probe: write all, run lint, drop the unused ones.
    ignores = [("*.xhtml", "s-023", "Judul berbahasa Indonesia; kapitalisasi judul gaya Inggris dari `se titlecase` tidak berlaku."),
               ("content.opf", "m-056", "Edisi white-label, bukan rilis Standard Ebooks; tidak ada halaman penulis di standardebooks.org untuk ditautkan.")]
    if masthead_file:
        ignores += [("masthead.xhtml", "t-048", "Kalimat pembuka kotak keterangan dicetak dengan nama acara dalam huruf kapital di sumber; dipertahankan apa adanya."),
                    ("masthead.xhtml", "y-003", "Baris alamat/kontak dicetak tanpa tanda baca akhir di sumber; dipertahankan apa adanya."),
                    ("masthead.xhtml", "y-034", "Titik pada alamat web dan surel (perspektif.net) bukan singkatan.")]
    ignores += [(i["file"], i["code"], i["reason"]) for i in se.get("lint_ignore", [])]
    write_ignores(root, ignores)

    for cmd in ("build-manifest", "build-spine"):
        run_se(cmd, root)
    if masthead_file:
        # `se build-spine` sorts body files alphabetically; the source page comes before the text, so put it right after the imprint
        opf_p = os.path.join(epub_dir, "content.opf")
        x = open(opf_p, encoding="utf-8").read()
        ref = re.search(r"[ \t]*<itemref idref=\"masthead\.xhtml\"/>\n", x)
        anchor = re.search(r"<itemref idref=\"imprint\.xhtml\"/>\n", x)
        if ref and anchor and ref.start() > anchor.end():
            x = x.replace(ref.group(0), "")
            at = x.index(anchor.group(0)) + len(anchor.group(0))
            x = x[:at] + ref.group(0) + x[at:]
            open(opf_p, "w", encoding="utf-8").write(x)
    for f in glob.glob(os.path.join(text_dir, "*.xhtml")):
        run_se("build-title", f, check=False)
    run_se("build-toc", root)
    run_se("clean", root)
    prune_ignores(root, ignores)

    ok = True
    print("SE EDITION", slug)
    p = run_se("--plain", "lint", root, check=False)
    lint_out = (p.stdout + p.stderr).strip()
    print("  se lint:", "clean" if not lint_out and p.returncode == 0 else "FAIL")
    if lint_out or p.returncode:
        print(lint_out[:3000])
        ok = False

    n_exp, n_got, diffs = parity(chapters, epigraph, ([masthead_file] if masthead_file else []) + body_files, se, masthead_exp)
    print("  word parity book.md vs body: %d vs %d tokens, %d differences" % (n_exp, n_got, len(diffs)))
    for d in diffs[:10]:
        print("    ", d)
    ok = ok and not diffs
    if epigraph:
        ep = re.search(r'<blockquote epub:type="epigraph">\s*<p>(.*?)</p>', open(body_files[0], encoding="utf-8").read(), re.S)
        same = ep and tokens(ep.group(1)) == tokens(epigraph["text"])
        print("  epigraph matches book.md front matter:", bool(same))
        ok = ok and bool(same)

    out_dir = os.path.join(bd, "se-out")
    shutil.rmtree(out_dir, ignore_errors=True)
    p = run_se("build", "--check", "-o", out_dir, root, check=False)
    print("  se build --check:", "ok" if p.returncode == 0 else "FAIL")
    if p.returncode:
        print((p.stdout + p.stderr)[-2000:])
        return False
    built = glob.glob(os.path.join(out_dir, "*.epub"))
    built = [b for b in built if not b.endswith("_advanced.epub")]
    if not built:
        fail("se build produced no epub")
    stem = os.path.splitext(cfg["epub_name"])[0]
    final = os.path.join(bd, "%s (SE).epub" % stem)
    localise_epub(built[0], final, cfg, se, labels)
    print("  EPUB:", final, "(%d KB)" % (os.path.getsize(final) // 1024))
    ok = epubcheck_all(final) and ok
    print("SE:", "PASS" if ok else "FAIL")
    return ok
