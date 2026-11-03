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
`"license": {"name": "...", "full_name": "...", "url": "..."}` in `finish` changes the license;
`"license": false` omits the page. The licensing choice is the editor's decision: review it per book.

Poems: consecutive italic paragraphs whose lines end with a backslash in book.md become one
`div.poem`. Validate with `epubcheck` (`brew install epubcheck`).

### Attribution

The structure, class names and stylesheet follow the MasasilaM edition of
*Menyambut Kongres Kebudayaan* (https://masasilam.com; contributors release their work under
CC0 1.0). `templates/masasilam.css` is adapted from that edition's stylesheet and credits
MasasilaM in its header. The colophon footer credits MasasilaM by default.
