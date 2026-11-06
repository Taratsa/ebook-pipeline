# pdf-to-epub-turboocr

Scanned PDF -> TurboOCR (GPU, `/ocr/pdf`) -> cleaned Markdown -> EPUB3.
One script, one per-book config file. Book-independent code lives in `pipeline.py`.

## Requirements
- Python 3 with `pymupdf` and `Pillow` (`pip install pymupdf pillow`)
- `pandoc` 3.x (`brew install pandoc`)
- Network access to a TurboOCR server (`GET /health` must return `ok`)

## Run a new book
    BOOK="/path/to/book folder"          # folder containing exactly one .pdf
    P=/Users/sf/Documents/workspaces/pdf-to-epub-turboocr/pipeline.py

    python3 $P "$BOOK" --init            # writes book.json with TODOs
    # 1. edit book.json: ocr.server, title, author, epub_name
    python3 $P "$BOOK" --steps ocr       # resumable; re-run if it reports failed pages
    # 2. read the document's OWN table of contents (from the OCR output of the TOC page),
    #    fill "chapters" = [start_page_index, title, eyebrow_or_null]
    # 3. fill "skip_pages" (cover, garbled TOC page) and "front_matter"
    python3 $P "$BOOK" --steps build,verify
    # 4. review "$BOOK/review.txt" (see below), add keep_joins / footer_patterns, re-run build,verify

Page indexes are 0-based and match the PDF page order.

## What is book-specific (book.json only)
- source_pdf, ocr.server, title, author, lang, epub_name
- cover_page, skip_pages, front_matter
- chapters: [start_page, title, eyebrow] — take these from the document's TOC, never guess them
- footer_patterns: regexes for running footers (page header/footer lines)
- keep_joins: [a, b] pairs that are a single word split across a line/page break.
  Add only after checking the real word; these are the only paragraph-break joins made.
- typo_fixes: [wrong, right] exact string fixes for OCR errors you have verified
- verify: must_contain / must_not_contain probes for the final EPUB text

## Quality rules built in
- Inline hyphen wraps: "tiba- tiba" -> "tiba-tiba" (compound kept), "pemerko- saan" -> "pemerkosaan".
  Decided by reduplication/affix matching (`is_redup`), not a dictionary.
- Paragraph-break hyphens are NOT joined unless listed in keep_joins. Unlisted ones are
  reported in review.txt so a human decides. Blind joining corrupts text (e.g. "Kemer-" + "nyiksaan").
- OCR alt text on images is dropped (it duplicates body text or is footer noise).
- Edge bare page numbers and running footers are removed.

## Outputs (inside the book folder)
- ocr/md/pNN.md      raw OCR per page (resumable cache)
- ocr/meta.json      pdf sha256, OCR settings
- epub_assets/       images + cover.jpg
- book.md            cleaned Markdown (the thing you proofread)
- review.txt         A) unjoined breaks  B) stale keep_joins  C) join-decision counts
- verify.json        PASS/FAIL checks; process exits 1 on FAIL
- <epub_name>        the deliverable

## Reproducibility
Same book folder + same book.json + same ocr/md = byte-identical book.md and identical EPUB text.
To prove it on a new machine: run `--steps images,build,verify` on a folder that already has ocr/md
and compare book.md with the stored copy.

## Known limits
- Requires one page OCR request per PDF page (sent as single-page PDFs, avoids the 100 MB body limit).
- Chapter boundaries and keep_joins are manual decisions; the pipeline cannot infer them.
- Vision-based checks were deliberately not used; verification is text-based only.


## Source of truth: book.md

Set `"source": "book.md"` in book.json to hand-edit book.md. The pipeline then never
regenerates it: `--steps build,verify` only runs pandoc and verify (ocr/images are skipped).

## Custom cover

`"cover_image": "epub_assets/cover.webp"` in book.json uses a hand-made cover instead of the cover page
rendered from the PDF (the `images` step then skips rendering it). Convert a PNG losslessly with
`Image.open("cover.png").convert("RGB").save("cover.webp", "WEBP", lossless=True)`. Some stores and
older readers ignore WebP covers; use a `.jpg` cover if the book is going to one.

## Hand-editable EPUB metadata and content

Files in the book folder, all optional; each one overrides what the build would generate:

| File | Controls | Fallback if absent |
|---|---|---|
| `metadata.yaml` | OPF metadata: title, creator(s), contributor(s), publisher, subjects, description, rights, source, date, pinned `identifier`, `lang` | values derived from book.json |
| `titlepage.html` | body of the title page (below the title) | author line |
| `colophon.html` | the Kolofon page | generated from book.json `finish` |
| `license.html` | the license page text | generated from `finish.license` |
| `book.md` | all chapter text, images, poems | n/a (source) |

`first-publisher` and `first-published` in metadata.yaml become `<meta property="schema:firstPublisher">`
and `schema:firstPublished` in the OPF (pandoc has no field for them, so they are injected after the build,
as in the MasasilaM editions). `publisher` must be a single string: a YAML list makes pandoc write an empty
`<dc:publisher>`, so join several with ", ".

Start `metadata.yaml` from the current values with `pipeline.py <book> --export-metadata` (never
overwrites an existing file). Keys set there override book.json, and the title and author on the
title page follow them. Pin `identifier` so readers keep reading position across rebuilds; set `date`
to the edition date, otherwise the build timestamp is used. File names can be changed with
`metadata_file` / `titlepage_file` / `colophon_file` / `license_file`.

## Logos in the kolofon

Put logos in `colophon.html` as `epub_assets/<file>.svg`. For several logos side by side wrap them in
`<div class="logo-row">` (first image = first logo) and keep them as separate images. Readers differ in
CSS and font support, so make each SVG self-contained instead of compensating in CSS:

- `viewBox` cropped to the drawn content only (sources often carry ~20% empty padding);
- live `<text>` converted to outlines (fontTools) and `<style>` classes inlined as `fill`/`stroke` attributes;
- the same `width`/`height` on the `<svg>` root and on the `<img>` tag (the size is set there, not in CSS).

`.logo-row` in `templates/masasilam.css` is deliberately bare (centred row, inline-block images,
vertical-align middle): no offsets or negative margins.

## Navigation document and cover page

Every build removes `cover.xhtml` and `nav.xhtml` from the spine (reading order). `linear="no"` is not
enough: KOReader ignores it unless "Hide non-linear fragments" is switched on, so the cover page and the
multi-page TOC counted as pages 1-5 (a book opened at page 6) and you could page back onto the cover.
The cover image stays the `cover-image` in the manifest (shelves and libraries still show it) and
`nav.xhtml` stays in the package (readers build their TOC menu from it). The landmarks that pointed at the
cover page and the nav are dropped, because epubcheck rejects landmarks to non-spine documents (RSC-011).
Landmark labels are translated to Indonesian when `lang` is `id`.

Finished builds also: `finish.title_lines` (title page broken over lines), `finish.titlepage_toc` (list the
title page in the TOC under this label), `finish.toc_depth` (default 2), and an empty visible colophon
heading whose `title` attribute carries the TOC label (`finish.hide_colophon_heading`, default true).
In book.md, `{title="..."}` on a heading sets its TOC label, `![](x.webp){.image-medium}` sets a class on
an image, and `<div class="subtitle"><strong>...</strong></div>` under a chapter heading is a subtitle.

## Image format

`"image_format": "webp"` in book.json writes page images as `epub_assets/pNN_N.webp` (quality
`"webp_quality"`, default 75; default format is `jpg`). The cover stays `cover.jpg`. After switching
an existing md-source book, run `--steps images` and change the `.jpg` references in book.md to `.webp`
(the images step never edits book.md). epubcheck accepts WebP, but very old readers may not render it.

## Finished EPUB (MasasilaM style)

Add a `"finish": {...}` object to book.json and `build` produces a finished edition:
title page, colophon (credits, rights box, source link, attribution), nested TOC
(chapters + `##` sections), full OPF metadata, `div.poem` verse, `div.image-with-caption`
figures and `templates/masasilam.css`. book.md is not modified (a temporary input file
is generated and deleted).

    "finish": {
      "publisher": "...", "subjects": ["..."], "toc_title": "Daftar Isi",
      "source_label": "Pustaka Langka (...)",
      "rights_lines": ["..."],
      "credits": [["Koreksi manual", "Name"]],
      "contributors": [["edt", "Name"]],
      "contact": "optional mailto address",
      "attribution_html": "optional footer HTML"
    }

Hand-edited colophon: put a `colophon.html` (raw HTML) in the book folder and the build uses it
instead of the generated one; delete it to fall back to book.json `finish`. Use another file
name with `"colophon_file"` in `finish`.

License page: a back-matter page titled with the license name (default `CC BY-SA 4.0`, subtitle =
full name) is added, and `dc:rights` carries the same name. Modelled on the MasasilaM edition of
*Tahun yang Tak Pernah Berakhir* (in-copyright material: only the digital-edition work is licensed,
rights in the original are not claimed, rights holders may object via `finish.contact`).
`license.html` in the book folder overrides the text (the h1 title is still set by the license name);
`"license": {"name": "...", "full_name": "...", "url": "...", "title": "..."}` in `finish` changes the license
(`title` = page/TOC heading, default the license name; use e.g. "Hak Cipta dan Lisensi" when only part of the book is licensed);
`"license": false` omits the page. The licensing choice is the editor's decision: review it per book.

Poems: consecutive italic paragraphs whose lines end with a backslash in book.md become one
`div.poem`. Validate with `epubcheck` (`brew install epubcheck`).

### Attribution

The structure, class names and stylesheet follow the MasasilaM edition of
*Menyambut Kongres Kebudayaan* (https://masasilam.com; contributors release their work under
CC0 1.0). `templates/masasilam.css` is adapted from that edition's stylesheet and credits
MasasilaM in its header. The colophon footer credits MasasilaM by default.

## Standard Ebooks edition (`--steps se`)

A second, independent artifact: `<epub_name> (SE).epub`, built from `book.md` to the Standard Ebooks
Manual of Style 1.9.1 (https://standardebooks.org/manual/1.9.1) and gated by the real SE toolchain.
The normal EPUB and `book.json` flow are unchanged; the SE step is opt-in.

    python3 pipeline.py <book_dir> --steps se

Toolchain (once): `uv tool install standardebooks`, `brew install cairo` (the step sets
`DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib` for `se`). epubcheck is optional but used when present.

What `se_edition.py` does, and where it departs from stock SE (the `se` tool itself is never modified):

- Text comes only from `book.md`. Chapters = `#` headings other than `front_matter.heading`; `##` and deeper
  become nested `z3998:subchapter` sections. Tables, lists, verse and raw HTML are not supported and stop the
  build with a message instead of being guessed. Scanner footers ("N dari M halaman") are dropped and
  paragraphs split by a page break (no end punctuation + lowercase start) are rejoined.
- Typography per the manual: curly double quotes (single quotes only for nesting), `.`/`,` inside quotes,
  no space before closing punctuation. Acronyms get `z3998:acronym` (Indonesian writes them without periods),
  foreign words get `<i xml:lang>`, titles of publications get `se:name.*` italics: all listed in `book.json`.
- White-label: no SE logo, no SE imprint claim. The stock Uncopyright (says "US public domain") is replaced by a
  licence page (CC0 for the editorial work only, text stays with its rights holders).
- Indonesian: `se` hard-codes English navigation labels and `se lint` demands them in the source, so the source
  stays SE-compliant and the finished EPUB is localised afterwards (Daftar Isi, Penanda, Halaman Judul, Bagian Awal;
  override with `se.labels`). The nav `aria-labelledby` is dropped because epubcheck 5.4 rejects it (5.3, bundled
  with `se`, accepts it).
- Cover: plain typographic 1400x2100 (League Spartan, shipped with `se`), or `se.cover_image`.
- `se-lint-ignore.xml` is generated; each rule carries a reason, and rules that match nothing are removed
  (`se lint` fails on unused ones).

Gates (the step exits 1 unless all pass): `se lint` prints nothing; word parity of `book.md` against the generated
body is exactly equal (tokens, ignoring quote marks); `se build --check` succeeds; epubcheck (bundled and system)
reports 0 fatals/errors/warnings.

`book.json` `"se"` block (required keys marked *):

    "se": {
      "author"*, "publisher"*, "producer"*: {"name"}, "rights"*, "abstract"*, "description_html"*,
      "subjects"*: [["Human rights--Indonesia", "sh2008105905"]],   # LCSH label + id; look them up, do not guess
      "title": "main title (no subtitle)", "subtitle", "work": "file-name slug", "author_full_name", "author_file_as",
      "author_wikipedia", "author_loc": "https://id.loc.gov/authorities/names/<id>.html",
      "contributors": [{"id": "interviewer", "name", "file_as", "role": "ivr", "wikipedia"}],
      "sources": ["https://..."],          # dc:source; leave out when the origin is unverified
      "source_label": "pindaian PDF ...", "genre": "Nonfiction",
      "qa": true,                            # interviews: paragraphs ending in "?" get class="question"
      "epigraph": {"heading": "Highlight", "cite": "Munir"},   # front-matter heading whose next paragraph is the epigraph
      "acronyms": [], "name_titles": ["SH"], "foreign": {"en": ["..."]}, "italic_names": [{"text", "type"}],
      "image_alt": {"p03_0.webp": "Prose description ending in a period."},   # required for every image
      "cover_image": "epub_assets/x.jpg", "cover_title", "labels": {}, "lint_ignore": [{"file","code","reason"}],
      "production_notes": ["..."], "released": "set automatically on first build"
    }

Hand-editable pages: `se-imprint.html`, `se-colophon.html`, `se-license.html` in the book folder hold one
paragraph per blank-line block (raw inline HTML allowed; `{date}`, `{iso}`, `{producer}` are substituted).
Delete a file to fall back to the generated text. `se-src/` and `se-out/` are regenerated on every run.

Never invent `dc:source`, contacts, publisher names or licence choices: take them from the source or ask.

