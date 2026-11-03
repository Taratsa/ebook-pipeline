#!/usr/bin/env python3
"""Build a symspell-format frequency dict from Leipzig Indonesian corpora.

Inputs: the *-words.txt files (tab-separated: id, word, count) of
ind_wikipedia_2021_1M and ind_newscrawl_2016_1M.
Output: 'word count' per line, lowercased, keeps hyphenated compounds
(e.g. masing-masing). Drops anything that is not a-z (plus Latin-1 accents).
Usage: build_leipzig_dict.py <leipzig_dir> <out.txt>
"""
import collections, os, re, sys

LEIP, OUT = sys.argv[1], sys.argv[2]
PAT = re.compile(r"[a-zà-ÿ]+(?:-[a-zà-ÿ]+)*")
SOURCES = [
    "ind_wikipedia_2021_1M/ind_wikipedia_2021_1M-words.txt",
    "ind_newscrawl_2016_1M/ind_newscrawl_2016_1M-words.txt",
]

counts = collections.Counter()
for rel in SOURCES:
    with open(os.path.join(LEIP, rel), encoding="utf-8", errors="replace") as f:
        for line in f:
            parts = line.rstrip("\n").split("\t")
            if len(parts) >= 3 and parts[2].isdigit():
                counts[parts[1].lower()] += int(parts[2])

kept = [(w, c) for w, c in counts.items() if PAT.fullmatch(w)]
kept.sort(key=lambda x: -x[1])
with open(OUT, "w", encoding="utf-8") as f:
    for w, c in kept:
        f.write(f"{w} {c}\n")
print(f"types={len(kept)} tokens={sum(c for _, c in kept)} -> {OUT}")
