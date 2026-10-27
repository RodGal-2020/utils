#!/usr/bin/env python3
"""bookpipe - split every book PDF in a folder into chapters and convert them to Markdown.

One self-contained script that replaces book2md.py, pdf2md.py and split_by_numbers.py.

Pipeline (each stage is resumable and writes its state to disk):

    discover -> inspect -> plan -> split -> convert -> postprocess

Chapter detection, in priority order (all detectors run; the others cross-check):
    1. sidecar    <book>.bookpipe.toml/.json next to the PDF (manual starts, like split_by_numbers)
    2. bookmarks  embedded PDF outline, picking the real chapter level
    3. toc        printed table of contents, with ONE fitted page offset (plus drift tracking)
    4. headings   font-size scan for chapter-start pages
    5. whole      the whole book as one unit (always the last resort)

Every page ends up in exactly one output: pages before the first chapter become
<book>_chapter00 ("Front matter"); short units are merged, never dropped.

Commands
    run      plan (reusing a valid plan) + split + convert      [default]
    plan     detect chapters only; writes plan.json and preview.md per book
    split    plan + split into chapter PDFs, no conversion
    status   table of every book: detector, agreement, conversion state, problems
    sidecar  write <book>.bookpipe.toml next to the PDF from its current plan

Examples
    python bookpipe.py run books/
    python bookpipe.py plan books/ --book "*bishop*"
    python bookpipe.py run books/ --methods pymupdf4llm,basic --no-images
    python bookpipe.py run books/ --retry-fallback          # upgrade chapters that fell back
    python bookpipe.py status books/

Editing a plan by hand: open out/<book>/plan.json, change "start" (1-based PDF page),
"title" or "kind" of any unit, add/remove units, and set "locked": true. Ends are
recomputed from the starts. Locked plans are never overwritten.

Configuration precedence: built-in defaults < bookpipe.toml/.json in the input folder
(or --config) < command-line flags. Config keys are the long option names with
underscores, e.g.  methods = "marker,pymupdf4llm"   min_chapter_pages = 3

Dependencies: pymupdf (required). Optional: marker-pdf (marker_single), pymupdf4llm.
"""
from __future__ import annotations

import argparse
import csv
import difflib
import fnmatch
import hashlib
import importlib.util
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

try:
    import tomllib  # Python 3.11+
except ImportError:  # pragma: no cover
    tomllib = None

try:
    import pymupdf
except ImportError:
    try:
        import fitz as pymupdf  # older PyMuPDF
    except ImportError:
        sys.exit("Missing dependency: pip install pymupdf")

DETECTOR_VERSION = 3
PLAN_VERSION = 1

# --------------------------------------------------------------------------------------
# Defaults (every key can be set in bookpipe.toml or on the command line)
# --------------------------------------------------------------------------------------
DEFAULTS = {
    "output": "book_chapters",
    "book": None,
    "recursive": False,
    "config": None,
    # planning
    "min_chapters": 2,
    "min_chapter_pages": 2,
    "toc_scan_pages": 40,
    "max_offset": 100,
    "threshold": 0.72,
    "split_front": False,
    "crosscheck": True,
    "preview": True,
    "preview_pages": 12,
    "ignore_lock": False,
    # splitting
    "overwrite": False,
    "prune": False,
    "keep_pdfs": True,
    # conversion
    "methods": "auto",
    "timeout": 600,
    "ocr": "auto",
    "marker_mode": "cli",
    "marker_args": "",
    "images": True,
    "frontmatter": True,
    "retry_fallback": False,
    "combine": False,
}

METHOD_RANK = {"marker": 0, "pymupdf4llm": 1, "basic": 2}
METHOD_ALIASES = {"marker-pdf": "marker", "pymupdf": "pymupdf4llm", "pymupdf-basic": "basic"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".svg"}

# --------------------------------------------------------------------------------------
# Patterns
# --------------------------------------------------------------------------------------
NUM_WORDS = ("one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|"
             "fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty")
CHAPTER_WORDS = (r"chapter|chap\.|chapitre|cap[ií]tulo|capitolo|kapitel|tema|lecci[oó]n|"
                 r"lecture|lesson|unit|unidad")
PART_WORDS = r"part|parte|partie|teil|book|libro|volume|volumen"
LABEL = rf"(?:\d{{1,3}}|[IVXLCivxlc]{{1,7}}|{NUM_WORDS})"

CHAPTER_RE = re.compile(rf"^\s*(?:{CHAPTER_WORDS})\s+{LABEL}\b", re.I)
PART_RE = re.compile(rf"^\s*(?:{PART_WORDS})\s+{LABEL}\b", re.I)
LABEL_ONLY_RE = re.compile(rf"^\s*(?:{CHAPTER_WORDS}|{PART_WORDS})\s+{LABEL}\s*[.:]?\s*$", re.I)
NUMBERED_RE = re.compile(r"^\s*\d{1,2}(?:\.|:)?\s+(?!\d)[^\W\d_]")       # "3 Linear models"
SUBNUMBERED_RE = re.compile(r"^\s*\d{1,2}\.\d")                          # "3.1 Basics"
STRIP_LABEL_RE = re.compile(
    rf"^\s*(?:(?:{CHAPTER_WORDS}|{PART_WORDS})\s+{LABEL}|\d{{1,2}}(?:\.\d+)*)\s*[.:\-–—]?\s*", re.I)
BACK_RE = re.compile(
    r"^\s*(references|bibliography|bibliograf[ií]a|referencias|appendix|appendices|ap[eé]ndice|"
    r"(subject |author )?index|glossary|glosario|notes|endnotes|further reading|solutions|answers|"
    r"epilogue|afterword)\b", re.I)
FRONT_RE = re.compile(
    r"^\s*(cover|front ?matter|half[- ]title|title page|title|copyright|dedication|epigraph|"
    r"(brief |detailed )?contents|table of contents|[ií]ndice|sumario|contenidos?|preface|prefacio|"
    r"pr[oó]logo|foreword|acknowledg\w*|agradecimientos|about (the )?authors?|"
    r"list of (figures|tables|abbreviations|symbols)|abbreviations|notation|nomenclature)\s*$", re.I)
SKIP_RE = re.compile(r"^\s*(cover|(brief |detailed )?contents|table of contents|[ií]ndice general)\s*$", re.I)
TOC_HEAD_RE = re.compile(
    r"^\s*((brief |detailed )?contents|table\s+of\s+contents|contenidos?|[ií]ndice( general)?|sumario|"
    r"table des mati[eè]res|inhalt(sverzeichnis)?)\s*$", re.I)
TOC_ROW_RE = re.compile(
    r"^(?P<title>.*?\S)(?:\s*(?:[.…·•_]\s*){2,}|\s+)(?P<page>\d{1,4}|[ivxlcdm]{1,7})$")
BARE_PAGE_RE = re.compile(r"^(\d{1,4}|[ivxlcdm]{1,7})$")


def clean(s: str) -> str:
    return re.sub(r"\s+", " ", str(s)).strip(" .\t·")


def norm(s: str) -> str:
    return re.sub(r"[^\w]+", " ", clean(s).casefold()).strip()


def strip_label(title: str) -> str:
    return STRIP_LABEL_RE.sub("", title, count=1).strip()


def classify(title: str) -> str:
    t = clean(title)
    if SUBNUMBERED_RE.match(t):
        return "section"
    if CHAPTER_RE.match(t) or NUMBERED_RE.match(t):
        return "chapter"
    if PART_RE.match(t):
        return "part"
    if BACK_RE.match(t):
        return "back"
    if FRONT_RE.match(t):
        return "front"
    return "other"


def fmt_dur(sec: float) -> str:
    if sec < 60:
        return f"{sec:.1f}s"
    if sec < 3600:
        return f"{int(sec // 60)}m {sec % 60:.0f}s"
    return f"{int(sec // 3600)}h {int(sec % 3600 // 60)}m"


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(str(tmp), "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(str(tmp), str(path))


def read_json(path: Path) -> Optional[dict]:
    try:
        with open(str(path), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def load_mapping(path: Path) -> dict:
    """Load a .toml or .json file into a dict."""
    if path.suffix.lower() == ".toml":
        if tomllib is None:
            raise RuntimeError(f"{path.name}: TOML needs Python 3.11+; use a .json file instead")
        with open(str(path), "rb") as f:
            return tomllib.load(f)
    with open(str(path), encoding="utf-8") as f:
        return json.load(f)


def log(msg: str = "", err: bool = False) -> None:
    print(msg, file=sys.stderr if err else sys.stdout, flush=True)


# --------------------------------------------------------------------------------------
# Book: cached access to one PDF
# --------------------------------------------------------------------------------------
TEXT_FLAGS = getattr(pymupdf, "TEXTFLAGS_TEXT", 0)


class Book:
    def __init__(self, path: Path):
        self.path = path
        self.doc = pymupdf.open(str(path))
        self.n = self.doc.page_count
        self._text: dict[int, str] = {}
        self._lines: dict[int, list] = {}
        self._body: Optional[float] = None
        self._labels: Optional[list[str]] = None
        self._density: Optional[float] = None

    def close(self) -> None:
        try:
            self.doc.close()
        except Exception:
            pass

    def text(self, p: int) -> str:
        if p not in self._text:
            try:
                self._text[p] = self.doc[p].get_text("text", sort=True) or ""
            except Exception:
                self._text[p] = ""
        return self._text[p]

    def lines(self, p: int) -> list[tuple[float, float, str]]:
        """(relative y, font size, text) per visual line, top to bottom."""
        if p in self._lines:
            return self._lines[p]
        out = []
        try:
            page = self.doc[p]
            height = float(page.rect.height) or 1.0
            data = page.get_text("dict", flags=TEXT_FLAGS)
            for block in data.get("blocks", []):
                for line in block.get("lines", []):
                    spans = [s for s in line.get("spans", []) if str(s.get("text", "")).strip()]
                    if not spans:
                        continue
                    text = clean("".join(s["text"] for s in spans))
                    if text:
                        out.append((line["bbox"][1] / height, max(float(s["size"]) for s in spans), text))
        except Exception:
            out = []
        out.sort(key=lambda x: x[0])
        self._lines[p] = out
        return out

    def sample_pages(self, k: int = 150) -> list[int]:
        step = max(1, self.n // k)
        return list(range(0, self.n, step))

    def body_size(self) -> float:
        if self._body is None:
            sizes: Counter = Counter()
            for p in self.sample_pages():
                for _, size, text in self.lines(p):
                    sizes[round(size * 2) / 2] += len(text)
            self._body = sizes.most_common(1)[0][0] if sizes else 0.0
        return self._body

    def headings(self, p: int, top: float = 0.65) -> list[tuple[float, str]]:
        """Lines set noticeably larger than body text in the upper part of the page."""
        body = self.body_size()
        if not body:
            return []
        out = []
        for y, size, text in self.lines(p):
            if y > top or size < body * 1.15 or len(text) > 200:
                continue
            if BARE_PAGE_RE.match(text) and size < body * 1.8:
                continue
            out.append((size, text))
        return out

    def labels(self) -> list[str]:
        if self._labels is None:
            try:
                labels = [self.doc[p].get_label() or "" for p in range(self.n)]
            except Exception:
                labels = [""] * self.n
            self._labels = labels if any(labels) else []
        return self._labels

    def chars_per_page(self) -> float:
        if self._density is None:
            counts = sorted(len(self.text(p).strip()) for p in self.sample_pages(40))
            self._density = float(counts[len(counts) // 2]) if counts else 0.0
        return self._density

    def fingerprint(self) -> dict:
        st = self.path.stat()
        return {"size": st.st_size, "mtime_ns": st.st_mtime_ns, "pages": self.n}


def title_score(book: Book, title: str, p: int) -> float:
    """How well a page looks like the start of the unit called `title` (0..1)."""
    full, core = norm(title), norm(strip_label(title))
    if not full or not (0 <= p < book.n):
        return 0.0
    heads = [norm(t) for _, t in book.headings(p)]
    penalty = 1.0
    if not heads and not book.lines(p):
        # no font information at all: fall back to the first lines of text, discounted.
        # (A page WITH font info but no headings is a body page; matching its plain text
        # would hit running headers that repeat the chapter title.)
        heads = [norm(x) for x in book.text(p).splitlines() if norm(x)][:6]
        penalty = 0.85
    if not heads:
        return 0.0
    blob = " ".join(heads)
    if full in blob or (len(core) >= 4 and core in blob):
        return penalty
    cands = heads + [a + " " + b for a, b in zip(heads, heads[1:])]
    target = core if len(core) >= 4 else full
    return penalty * max(difflib.SequenceMatcher(None, target, c).ratio() for c in cands)


# --------------------------------------------------------------------------------------
# Units and plans
# --------------------------------------------------------------------------------------
@dataclass
class Unit:
    start: int                      # 0-based PDF page
    title: str
    kind: str = "chapter"           # front | chapter | part | back
    source: str = ""
    confidence: float = 1.0
    printed: Optional[str] = None
    end: int = -1                   # 0-based inclusive, computed by finalize()
    index: int = 0
    includes: list = field(default_factory=list)

    def to_json(self) -> dict:
        d = asdict(self)
        d["start"], d["end"] = self.start + 1, self.end + 1
        d["pages"] = self.end - self.start + 1
        d["confidence"] = round(self.confidence, 3)
        if not d["includes"]:
            d.pop("includes")
        return d

    @staticmethod
    def from_json(d: dict) -> "Unit":
        return Unit(start=int(d["start"]) - 1, title=str(d.get("title", "")),
                    kind=str(d.get("kind", "chapter")), source=str(d.get("source", "manual")),
                    confidence=float(d.get("confidence", 1.0)), printed=d.get("printed"))


def unit_kind(title: str) -> str:
    k = classify(title)
    return {"section": "chapter", "other": "chapter"}.get(k, k)


def finalize(units: list[Unit], n: int, min_pages: int, split_front: bool) -> list[Unit]:
    """Sort, dedupe, merge short units, add front matter, number. Guarantees full coverage."""
    by_start: dict[int, Unit] = {}
    for u in sorted(units, key=lambda u: (u.start, u.kind == "front")):
        if 0 <= u.start < n and u.start not in by_start:
            by_start[u.start] = u
    units = [by_start[s] for s in sorted(by_start)]
    if not split_front:  # leading preface/foreword/... are absorbed into front matter
        while units and units[0].kind == "front":
            units.pop(0)
    if not units:
        return [Unit(0, "Whole book", "chapter", "whole", 0.0, end=n - 1, index=1)]

    def ends(us):
        for i, u in enumerate(us):
            u.end = us[i + 1].start - 1 if i + 1 < len(us) else n - 1

    ends(units)
    changed = True
    while changed and len(units) > 1:
        changed = False
        for i, u in enumerate(units):
            if u.end - u.start + 1 >= min_pages:
                continue
            if i + 1 < len(units):          # merge into the following unit (part page -> chapter)
                nxt = units[i + 1]
                nxt.includes = [u.title] + u.includes + nxt.includes
                nxt.start = u.start
            else:                           # short last unit -> previous unit
                prv = units[i - 1]
                prv.includes = prv.includes + [u.title] + u.includes
            units.pop(i)
            ends(units)
            changed = True
            break

    out = []
    if units[0].start > 0:
        out.append(Unit(0, "Front matter", "front", "coverage", 1.0, end=units[0].start - 1))
    out += units
    first = 0 if out[0].kind == "front" else 1   # front matter is always chapter00
    for i, u in enumerate(out):
        u.index = i + first
    return out


def sanity(units: list[Unit], n: int, min_chapters: int) -> list[str]:
    main = [u for u in units if u.kind != "front"]
    problems = []
    if len(main) < min_chapters:
        problems.append(f"only {len(main)} unit(s)")
        return problems
    starts = [u.start for u in main]
    if starts != sorted(set(starts)):
        problems.append("starts not strictly increasing")
    lengths = [b - a for a, b in zip(starts, starts[1:] + [n])]
    if len(main) >= 3 and max(lengths) > 0.8 * n:
        problems.append("one unit covers >80% of the book")
    if sorted(lengths)[len(lengths) // 2] < 2:
        problems.append("too fragmented (median unit < 2 pages)")
    return problems


def agreement(a: list[Unit], b: list[Unit], tol: int = 1) -> Optional[float]:
    sa = [u.start for u in a if u.kind in ("chapter", "back")]
    sb = [u.start for u in b if u.kind != "front"]
    if len(sa) < 2 or len(sb) < 2:
        return None
    return sum(any(abs(x - y) <= tol for y in sb) for x in sa) / len(sa)


# --------------------------------------------------------------------------------------
# Detector 1: sidecar (manual override)
# --------------------------------------------------------------------------------------
def find_sidecar(pdf: Path) -> Optional[Path]:
    for ext in (".toml", ".json"):
        p = pdf.with_name(pdf.stem + ".bookpipe" + ext)
        if p.exists():
            return p
    return None


def sidecar_entries(cfg: dict) -> list[tuple[int, str]]:
    raw = cfg.get("chapters", [])
    out = []
    if isinstance(raw, dict):  # {"1": 1, "2": 13} like split_by_numbers.py
        for k, v in sorted(raw.items(), key=lambda kv: int(kv[1]) if not isinstance(kv[1], dict) else int(kv[1]["start"])):
            if isinstance(v, dict):
                out.append((int(v["start"]), str(v.get("title", f"Chapter {k}"))))
            else:
                out.append((int(v), f"Chapter {k}"))
    else:
        for i, item in enumerate(raw, 1):
            if isinstance(item, dict):
                out.append((int(item["start"]), str(item.get("title", f"Chapter {i}"))))
            elif isinstance(item, (list, tuple)):
                out.append((int(item[0]), str(item[1]) if len(item) > 1 else f"Chapter {i}"))
            else:
                out.append((int(item), f"Chapter {i}"))
    return out


def start_score(book: Book, p: int) -> float:
    """Title-agnostic evidence that page p starts a chapter (used when no titles are known)."""
    body = book.body_size()
    hs = book.headings(p, top=0.5)
    if not body or not hs:
        return 0.0
    if any(CHAPTER_RE.match(t) or PART_RE.match(t) for _, t in hs[:3]):
        return 1.0
    if any(re.fullmatch(r"\d{1,2}", t) and s >= 1.8 * body for s, t in hs[:3]):
        return 1.0
    return 0.7 if max(s for s, _ in hs) >= 1.8 * body else 0.0


def anchor(book: Book, entries: list[tuple[str, int]], score: Callable[[str, int], float],
           thr: float, after: int, max_offset: int) -> tuple[Optional[list[tuple[int, float]]], str]:
    """Map printed page numbers to PDF pages.

    Fits ONE global offset (page labels first, otherwise a vote over offsets), then walks the
    entries in order and allows small local drift (unnumbered plates, blank pages), so the
    offset can change part-way through the book. Returns [(pdf_page, confidence)] per entry
    (None when the offset could not be anchored) and a note.
    """
    def hits(off):
        h, tot = 0, 0.0
        for title, num in entries:
            p = num - 1 + off
            if after < p < book.n:
                s = score(title, p)
                if s >= thr:
                    h, tot = h + 1, tot + s
        return h, tot

    best_off, best, how = None, (0, 0.0), ""
    labels = book.labels()
    if labels:
        idx: dict[str, int] = {}
        for i, lab in enumerate(labels):
            if i > after:
                idx.setdefault(lab, i)
        offs = Counter(idx[str(num)] - (num - 1) for _, num in entries if str(num) in idx)
        if offs:
            off = offs.most_common(1)[0][0]
            h = hits(off)
            if h[0] >= max(2, 0.3 * len(entries)):
                best_off, best, how = off, h, "page labels"
    if best_off is None:
        for off in range(-10, min(max_offset, book.n) + 1):
            h = hits(off)
            if h > best:
                best_off, best = off, h
        how = "voted offset"
    need = max(2, int(0.3 * len(entries) + 0.999))
    if best_off is None or best[0] < need:
        return None, "could not anchor printed page numbers to PDF pages"

    out, off, last, drift, verified = [], best_off, after, 0, 0
    for title, num in entries:
        pred = num - 1 + off
        found = None
        for d in (0, 1, -1, 2, -2, 3, 4, 5, 6):
            p = pred + d
            if last < p < book.n:
                s = score(title, p)
                if s >= thr:
                    found = (p, s)
                    break
        if found:
            p, s = found
            if p - (num - 1) != off:
                drift += 1
                off = p - (num - 1)
            verified += 1
            out.append((p, s))
            last = p
        elif last < pred < book.n:
            out.append((pred, 0.4))
            last = pred
        else:
            out.append((-1, 0.0))
    note = f"{how} {best_off:+d}, {verified}/{len(entries)} starts verified"
    if drift:
        note += f", offset drift corrected {drift}x"
    return out, note


def detect_sidecar(book: Book, cfg: dict) -> tuple[list[Unit], str]:
    entries = sidecar_entries(cfg)
    if not entries:
        return [], "sidecar has no chapters"
    mode = str(cfg.get("pages", "printed")).lower()
    offset = cfg.get("offset", "auto")
    if mode == "pdf":
        pages = [(s - 1, 1.0) for s, _ in entries]
        note = "PDF page numbers"
    elif isinstance(offset, int):
        pages = [(s - 1 + offset, 1.0) for s, _ in entries]
        note = f"printed pages, fixed offset {offset:+d}"
    else:
        named = any(not re.fullmatch(r"Chapter \d+", t) for _, t in entries)
        scorer = (lambda t, p: title_score(book, t, p)) if named else (lambda t, p: start_score(book, p))
        res, note = anchor(book, [(t, s) for s, t in entries], scorer, 0.6, -1, int(cfg.get("max_offset", 100)))
        if res is None:
            res = [(s - 1, 0.3) for s, _ in entries]
            note += "; assumed offset 0"
        pages = res
        note = "printed pages, " + note
    units = [Unit(p, t, unit_kind(t), "sidecar", c, printed=str(s))
             for (s, t), (p, c) in zip(entries, pages) if 0 <= p < book.n]
    end = cfg.get("end")  # last page of the main text (like the 428 in split_by_numbers.py)
    if end is not None and units:
        if mode == "pdf":
            p = int(end) - 1
        else:  # use the offset in force at the last chapter (after any drift)
            last_num = int(units[-1].printed)
            p = int(end) - 1 + (units[-1].start - (last_num - 1))
        if 0 <= p < book.n - 1:
            units.append(Unit(p + 1, "Back matter", "back", "sidecar", 1.0))
    return units, note


# --------------------------------------------------------------------------------------
# Detector 2: bookmarks
# --------------------------------------------------------------------------------------
def detect_bookmarks(book: Book) -> tuple[list[Unit], str]:
    try:
        toc = book.doc.get_toc(simple=True) or []
    except Exception:
        toc = []
    rows = [(int(l), clean(t), int(p) - 1) for l, t, p in toc
            if isinstance(p, (int, float)) and 1 <= int(p) <= book.n and clean(t)]
    if len(rows) < 2:
        return [], "no usable bookmarks"
    levels = sorted({r[0] for r in rows})
    selected, parents, chosen = [], [], None
    for lvl in levels:
        cur = [r for r in rows if r[0] == lvl]
        # a chapter level needs >= 2 distinct pages of real content, not parts/front/back matter
        content = {r[2] for r in cur if classify(r[1]) in ("chapter", "other", "section")}
        if len(content) >= 2:
            selected, chosen = cur, lvl
            break
        parents += cur
    if not selected:  # tiny books: accept the first level with two distinct pages
        for lvl in levels:
            cur = [r for r in rows if r[0] == lvl]
            if len({r[2] for r in cur}) >= 2:
                selected, chosen, parents = cur, lvl, []
                break
    if not selected:
        return [], "bookmarks point to a single page"
    keep_parents = [r for r in parents if PART_RE.match(r[1]) or BACK_RE.match(r[1])]
    units = []
    for _, title, p in sorted(selected + keep_parents, key=lambda r: r[2]):
        if SKIP_RE.match(title):
            continue
        # outlines are sometimes off by one page: nudge forward if the next page clearly matches
        if title_score(book, title, p) < 0.5 and title_score(book, title, p + 1) >= 0.85:
            p += 1
        units.append(Unit(p, title, unit_kind(title), "bookmarks", 1.0))
    return units, f"outline level {chosen}" + (" (+ parts)" if keep_parents else "")


# --------------------------------------------------------------------------------------
# Detector 3: printed table of contents
# --------------------------------------------------------------------------------------
def find_toc_pages(book: Book, scan: int) -> list[int]:
    limit = min(scan, book.n)

    def rows_on(p):
        lines = [clean(x) for x in book.text(p).splitlines() if clean(x)]
        return lines, sum(bool(TOC_ROW_RE.match(l)) for l in lines)

    start = None
    for p in range(limit):
        lines, numbered = rows_on(p)
        if numbered >= 3 and any(TOC_HEAD_RE.match(l) for l in lines[:10]):
            start = p
            break
    if start is None:
        return []
    pages = [start]
    for p in range(start + 1, min(start + 15, book.n)):
        lines, numbered = rows_on(p)
        if numbered >= 4 and numbered >= 0.25 * len(lines):
            pages.append(p)
        else:
            break
    return pages


def parse_toc_rows(lines: list[str]) -> list[tuple[str, str]]:
    rows, pending = [], []
    for line in lines:
        if not line or TOC_HEAD_RE.match(line):
            pending = []
            continue
        if BARE_PAGE_RE.match(line):
            if pending:
                rows.append((clean(" ".join(pending)), line))
                pending = []
            continue
        if LABEL_ONLY_RE.match(line):
            pending = [line]
            continue
        m = TOC_ROW_RE.match(line)
        if m and re.search(r"[^\W\d_]", m.group("title")):
            rows.append((clean(" ".join(pending + [m.group("title")])), m.group("page")))
            pending = []
        else:
            pending = (pending + [line])[-3:]
    return rows


def longest_nondecreasing(rows: list[tuple[str, int, str]]) -> list[tuple[str, int, str]]:
    if not rows:
        return []
    best = [1] * len(rows)
    prev = [-1] * len(rows)
    for i in range(len(rows)):
        for j in range(i):
            if rows[j][1] <= rows[i][1] and best[j] + 1 > best[i]:
                best[i], prev[i] = best[j] + 1, j
    i = max(range(len(rows)), key=lambda k: best[k])
    out = []
    while i >= 0:
        out.append(rows[i])
        i = prev[i]
    return out[::-1]


def major_rows(rows: list[tuple[str, int, str]]) -> list[tuple[str, int, str]]:
    chapters = [i for i, r in enumerate(rows) if r[2] == "chapter"]
    if len(chapters) < 2:
        return [r for r in rows if r[2] != "section"]
    first, last = chapters[0], chapters[-1]
    out = []
    for i, r in enumerate(rows):
        k = r[2]
        if k in ("chapter", "part", "back"):
            out.append(r)
        elif k in ("front", "other") and i < first:
            out.append(r)
        elif k == "front" and i > last:
            out.append(r)
    return out


def detect_toc(book: Book, args) -> tuple[list[Unit], str, dict]:
    meta: dict = {}
    pages = find_toc_pages(book, args.toc_scan_pages)
    if not pages:
        return [], "no printed contents found", meta
    lines = [clean(x) for p in pages for x in book.text(p).splitlines() if clean(x)]
    raw = parse_toc_rows(lines)
    arabic, seen = [], set()
    for title, pg in raw:
        if not pg.isdigit():
            continue  # roman-numbered rows are front matter
        key = (norm(title), int(pg))
        if key in seen:
            continue
        seen.add(key)
        arabic.append((title, int(pg), classify(title)))
    arabic = [r for r in arabic if r[1] <= book.n + 50]
    arabic = longest_nondecreasing(arabic)
    rows = major_rows(arabic)
    meta = {"toc_pages": [p + 1 for p in pages], "rows": [[t, pg] for t, pg, _ in rows]}
    if len(rows) < 2:
        return [], f"contents on p.{pages[0] + 1} but fewer than 2 usable rows", meta

    where = f"contents p.{pages[0] + 1}-{pages[-1] + 1}"
    res, how = anchor(book, [(t, num) for t, num, _ in rows], lambda t, p: title_score(book, t, p),
                      args.threshold, pages[-1], args.max_offset)
    if res is None:
        return [], f"{where} ({len(rows)} rows) but {how}", meta
    units = [Unit(p, t, unit_kind(t), "toc", c, printed=str(num))
             for (t, num, _), (p, c) in zip(rows, res) if p >= 0]
    return units, f"{where}, {how}", meta


# --------------------------------------------------------------------------------------
# Detector 4: font-based heading scan
# --------------------------------------------------------------------------------------
def heading_title(book: Book, p: int) -> str:
    hs = book.headings(p)
    if not hs:
        return f"Page {p + 1}"
    big = max(s for s, _ in hs)
    parts = [t for s, t in hs if s >= max(0.5 * big, 1.3 * book.body_size())][:3]
    return clean(" ".join(parts))[:150]


def detect_headings(book: Book, skip: set[int]) -> tuple[list[Unit], str]:
    body = book.body_size()
    if not body:
        return [], "no text layer"
    info = []
    for p in range(book.n):
        if p in skip:
            continue
        hs = book.headings(p)
        if not hs:
            continue
        top = hs[:3]
        labelled = any((CHAPTER_RE.match(t) or PART_RE.match(t)) for s, t in top) or \
            any(re.fullmatch(r"\d{1,2}", t) and s >= 1.8 * body for s, t in top)
        info.append((p, max(s for s, _ in hs), labelled))
    chosen = [x for x in info if x[2]]
    how = "chapter-labelled headings"
    if len(chosen) >= 2:
        # back matter (Index, References...) set at least as large as the chapter headings
        floor = max(1.5 * body, 0.5 * min(big for _, big, _ in chosen))
        picked = {x[0] for x in chosen}
        for p, big, _ in info:
            if p not in picked and p > chosen[0][0] and big >= floor and \
                    any(BACK_RE.match(t) for _, t in book.headings(p)[:2]):
                chosen.append((p, big, True))
        chosen.sort()
    else:
        tiers = Counter(round(big * 2) / 2 for _, big, _ in info if big >= 1.5 * body)
        chosen = []
        for size in sorted(tiers, reverse=True):
            if 2 <= tiers[size] <= max(2, book.n // 3):
                cand = [x for x in info if round(x[1] * 2) / 2 >= size]
                if len(cand) >= 2 and book.n / len(cand) >= 3:
                    chosen, how = cand, f"largest heading tier ({size:g}pt, body {body:g}pt)"
                    break
    units, last_title = [], None
    for p, _, _ in chosen:
        title = heading_title(book, p)
        if units and units[-1].start == p - 1 and norm(title) == last_title:
            continue  # same heading repeated on the next page
        units.append(Unit(p, title, unit_kind(title), "headings", 0.7 if "labelled" in how else 0.5))
        last_title = norm(title)
    return units, how if units else "no chapter-like headings"


# --------------------------------------------------------------------------------------
# Planning
# --------------------------------------------------------------------------------------
PLAN_KEYS = ("min_chapters", "min_chapter_pages", "toc_scan_pages", "max_offset",
             "threshold", "split_front", "crosscheck")


def settings_hash(args, sidecar_cfg: Optional[dict]) -> str:
    blob = json.dumps({"v": [DETECTOR_VERSION, PLAN_VERSION],
                       "args": {k: getattr(args, k) for k in PLAN_KEYS},
                       "sidecar": sidecar_cfg}, sort_keys=True, default=str)
    return hashlib.sha1(blob.encode()).hexdigest()[:12]


def build_plan(book: Book, args, sidecar_path: Optional[Path]) -> dict:
    sidecar_cfg = load_mapping(sidecar_path) if sidecar_path else None
    candidates: dict[str, list[Unit]] = {}
    notes: dict[str, str] = {}
    toc_meta: dict = {}

    if sidecar_cfg is not None:
        candidates["sidecar"], notes["sidecar"] = detect_sidecar(book, sidecar_cfg)
    candidates["bookmarks"], notes["bookmarks"] = detect_bookmarks(book)
    candidates["toc"], notes["toc"], toc_meta = detect_toc(book, args)

    order = ["sidecar", "bookmarks", "toc", "headings"]
    chosen, problems_by = None, {}
    for name in order:
        if name == "headings" and "headings" not in candidates:
            if chosen is not None and not args.crosscheck:
                break
            skip = {p - 1 for p in toc_meta.get("toc_pages", [])}
            candidates["headings"], notes["headings"] = detect_headings(book, skip)
        if chosen is not None or name not in candidates:
            continue
        units = candidates[name]
        if not units:
            continue
        trial = finalize([Unit(**{**asdict(u), "includes": []}) for u in units], book.n,
                         args.min_chapter_pages, args.split_front)
        problems = sanity(trial, book.n, args.min_chapters)
        if problems:
            problems_by[name] = problems
            notes[name] += " — rejected: " + "; ".join(problems)
        else:
            chosen = name

    if chosen:
        final = finalize(candidates[chosen], book.n, args.min_chapter_pages, args.split_front)
    else:
        chosen = "whole"
        final = [Unit(0, book.path.stem, "chapter", "whole", 1.0, end=book.n - 1, index=1)]

    agree = {}
    for name, units in candidates.items():
        if name != chosen and units:
            a = agreement(final, units)
            if a is not None:
                agree[name] = round(a, 2)

    density = book.chars_per_page()
    return {
        "plan_version": PLAN_VERSION,
        "source_pdf": str(book.path),
        "fingerprint": book.fingerprint(),
        "settings": settings_hash(args, sidecar_cfg),
        "created": datetime.now().isoformat(timespec="seconds"),
        "locked": False,
        "page_count": book.n,
        "detector": chosen,
        "detector_notes": notes,
        "agreement": agree,
        "text_layer": {"median_chars_per_page": int(density), "scanned": density < 30},
        "printed_toc": toc_meta,
        "units": [u.to_json() for u in final],
        "candidates": {k: [{"start": u.start + 1, "title": u.title, "kind": u.kind,
                            "confidence": round(u.confidence, 2)} for u in v]
                       for k, v in candidates.items() if v},
    }


def units_from_plan(plan: dict, n: int, args) -> list[Unit]:
    """Rebuild units from a (possibly hand-edited) plan; ends always recomputed."""
    raw = sorted((Unit.from_json(d) for d in plan.get("units", [])), key=lambda u: u.start)
    if plan.get("locked"):
        # respect the user's units exactly: only recompute ends and fill a leading gap
        units = []
        for u in raw:
            if 0 <= u.start < n and (not units or u.start > units[-1].start):
                units.append(u)
        if not units:
            units = [Unit(0, "Whole book", "chapter", "manual")]
        for i, u in enumerate(units):
            u.end = units[i + 1].start - 1 if i + 1 < len(units) else n - 1
        if units[0].start > 0:
            units.insert(0, Unit(0, "Front matter", "front", "coverage", 1.0, end=units[0].start - 1))
        first = 0 if units[0].kind == "front" else 1
        for i, u in enumerate(units):
            u.index = i + first
        return units
    out = []
    for d, u in zip(sorted(plan["units"], key=lambda d: d["start"]), raw):
        u.end, u.index = int(d["end"]) - 1, int(d["index"])
        out.append(u)
    return out


def write_preview(book: Book, plan: dict, path: Path, k: int) -> None:
    pages = sorted(set(range(min(k, book.n))) | set(range(max(0, book.n - k), book.n)))
    body = book.body_size()
    out = [f"# Preview: {book.path.name}", "",
           f"- PDF pages: {book.n}", f"- Body font size: {body:g}pt",
           f"- Median characters per page: {plan['text_layer']['median_chars_per_page']}",
           f"- Chosen detector: **{plan['detector']}**", ""]
    for name, note in plan["detector_notes"].items():
        out.append(f"- {name}: {note}")
    out += ["", "## Chosen units", "", "| # | kind | PDF pages | title |", "|---|---|---|---|"]
    for u in plan["units"]:
        out.append(f"| {u['index']:02d} | {u['kind']} | {u['start']}-{u['end']} | {u['title']} |")
    out += ["", "## Large headings across the book", ""]
    for p in range(book.n):
        hs = [(s, t) for s, t in book.headings(p) if s >= body * 1.4]
        if hs:
            out.append(f"- p.{p + 1}: " + " / ".join(f"{t} ({s:g}pt)" for s, t in hs[:3]))
    out += ["", "## Edge pages", ""]
    for p in pages:
        label = book.labels()[p] if book.labels() else ""
        out += [f"### PDF page {p + 1}" + (f" (label {label})" if label else ""), "", "```",
                "\n".join(book.text(p).splitlines()[:40]), "```", ""]
    atomic_write(path, "\n".join(out))


# --------------------------------------------------------------------------------------
# Conversion
# --------------------------------------------------------------------------------------
def find_marker() -> Optional[str]:
    exe = shutil.which("marker_single")
    if exe:
        return exe
    bindir = Path(sys.executable).parent
    for c in (bindir / "marker_single", bindir / "marker_single.exe", bindir / "Scripts" / "marker_single.exe"):
        if c.exists():
            return str(c)
    return None


def available_methods(marker_mode: str) -> list[str]:
    out = []
    if find_marker() or (marker_mode == "api" and importlib.util.find_spec("marker")):
        out.append("marker")
    if importlib.util.find_spec("pymupdf4llm"):
        out.append("pymupdf4llm")
    out.append("basic")
    return out


def resolve_methods(spec: str, marker_mode: str) -> list[str]:
    avail = available_methods(marker_mode)
    if spec in ("auto", "", None):
        return avail
    if spec == "none":
        return []
    wanted = [METHOD_ALIASES.get(m.strip(), m.strip()) for m in spec.split(",") if m.strip()]
    bad = [m for m in wanted if m not in METHOD_RANK]
    if bad:
        raise SystemExit(f"Unknown method(s): {', '.join(bad)}. Choose from marker, pymupdf4llm, basic, auto, none")
    missing = [m for m in wanted if m not in avail]
    for m in missing:
        log(f"WARNING: method '{m}' is not installed; skipping it", err=True)
    return [m for m in wanted if m in avail]


class EmptyOutput(RuntimeError):
    pass


@dataclass
class ConvResult:
    text: str
    images: dict  # name -> Path
    tmp: Optional[Path] = None


def conv_marker_cli(pdf: Path, ctx: dict) -> ConvResult:
    exe = find_marker()
    if not exe:
        raise RuntimeError("marker_single not found")
    tmp = Path(tempfile.mkdtemp(prefix="marker_", dir=str(ctx["work"])))
    cmd = [exe, str(pdf), "--output_dir", str(tmp)]
    if not ctx["ocr"]:
        cmd.append("--disable_ocr")
    cmd += shlex.split(ctx["marker_args"]) if ctx["marker_args"] else []
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=ctx["timeout"])
    except subprocess.TimeoutExpired:
        shutil.rmtree(tmp, ignore_errors=True)
        raise RuntimeError(f"timed out after {fmt_dur(ctx['timeout'])}")
    if r.returncode != 0:
        shutil.rmtree(tmp, ignore_errors=True)
        msg = (r.stderr or r.stdout or "unknown error").strip().splitlines()
        raise RuntimeError("marker failed: " + " | ".join(msg[-6:])[:1500])
    mds = [tmp / pdf.stem / f"{pdf.stem}.md", tmp / f"{pdf.stem}.md"]
    md = next((m for m in mds if m.exists()), None)
    if md is None:
        found = list(tmp.rglob("*.md"))
        md = found[0] if len(found) == 1 else None
    if md is None:
        shutil.rmtree(tmp, ignore_errors=True)
        raise RuntimeError("marker finished but no .md output was found")
    images = {f.name: f for f in md.parent.rglob("*") if f.suffix.lower() in IMAGE_EXT}
    return ConvResult(md.read_text(encoding="utf-8", errors="replace"), images, tmp)


_MARKER_API = None


def conv_marker_api(pdf: Path, ctx: dict) -> ConvResult:
    """Experimental: load Marker's models once per run (marker-pdf 1.x Python API)."""
    global _MARKER_API
    if _MARKER_API is None:
        from marker.converters.pdf import PdfConverter  # type: ignore
        from marker.models import create_model_dict  # type: ignore
        from marker.output import text_from_rendered  # type: ignore
        log("  loading Marker models (once per run)...")
        _MARKER_API = (PdfConverter, create_model_dict(), text_from_rendered)
    PdfConverter, models, text_from_rendered = _MARKER_API
    config = {"output_format": "markdown"}
    if not ctx["ocr"]:
        config["disable_ocr"] = True
    rendered = PdfConverter(artifact_dict=models, config=config)(str(pdf))
    text, _, images = text_from_rendered(rendered)
    tmp = Path(tempfile.mkdtemp(prefix="marker_", dir=str(ctx["work"])))
    paths = {}
    for name, img in (images or {}).items():
        fp = tmp / Path(name).name
        img.save(str(fp))
        paths[fp.name] = fp
    return ConvResult(text, paths, tmp)


def conv_marker(pdf: Path, ctx: dict) -> ConvResult:
    if ctx["marker_mode"] == "api" and not ctx.get("marker_api_failed"):
        try:
            return conv_marker_api(pdf, ctx)
        except ImportError as exc:
            ctx["marker_api_failed"] = True
            log(f"  Marker API unavailable ({exc}); using marker_single", err=True)
    return conv_marker_cli(pdf, ctx)


def conv_pymupdf4llm(pdf: Path, ctx: dict) -> ConvResult:
    import pymupdf4llm  # type: ignore
    if not ctx["images"]:
        return ConvResult(pymupdf4llm.to_markdown(str(pdf)), {})
    tmp = Path(tempfile.mkdtemp(prefix="p4l_", dir=str(ctx["work"])))
    try:
        text = pymupdf4llm.to_markdown(str(pdf), write_images=True, image_path=str(tmp), image_format="png")
    except TypeError:
        text = pymupdf4llm.to_markdown(str(pdf), write_images=True, image_path=str(tmp))
    images = {f.name: f for f in tmp.iterdir() if f.suffix.lower() in IMAGE_EXT}
    return ConvResult(text, images, tmp)


def conv_basic(pdf: Path, ctx: dict) -> ConvResult:
    parts = []
    with pymupdf.open(str(pdf)) as doc:
        for i in range(doc.page_count):
            parts.append(f"<!-- page {ctx['first_page'] + i} -->\n\n{doc[i].get_text('text', sort=True)}")
    return ConvResult("\n\n".join(parts), {})


CONVERTERS: dict[str, Callable[[Path, dict], ConvResult]] = {
    "marker": conv_marker, "pymupdf4llm": conv_pymupdf4llm, "basic": conv_basic}

IMG_LINK_RE = re.compile(r"(!\[[^\]]*\]\()([^)\s]+)((?:\s+\"[^\"]*\")?\))")


def finish_markdown(res: ConvResult, unit: Unit, stem: str, md_dir: Path, meta: dict, ctx: dict) -> int:
    img_dir = md_dir / "images" / stem
    if img_dir.exists():
        shutil.rmtree(img_dir, ignore_errors=True)
    text = res.text
    if res.images and ctx["images"]:
        img_dir.mkdir(parents=True, exist_ok=True)
        for name, src in res.images.items():
            shutil.copy2(str(src), str(img_dir / name))

        def fix(m):
            name = Path(m.group(2).replace("\\", "/")).name
            if name in res.images:
                return f"{m.group(1)}images/{stem}/{name}{m.group(3)}"
            return m.group(0)
        text = IMG_LINK_RE.sub(fix, text)
    if ctx["frontmatter"]:
        fm = ["---"] + [f"{k}: {json.dumps(v, ensure_ascii=False)}" for k, v in meta.items()] + ["---", ""]
        text = "\n".join(fm) + "\n" + text.lstrip()
    atomic_write(md_dir / f"{stem}.md", text)
    return len(res.text)


# --------------------------------------------------------------------------------------
# Logging (CSV, compatible in spirit with pdf2md.py's conversion_log.csv)
# --------------------------------------------------------------------------------------
LOG_FIELDS = ["timestamp", "book", "unit", "pages", "method", "duration_sec", "success", "output_chars", "error"]


def log_row(path: Path, **row) -> None:
    new = not path.exists()
    try:
        with open(str(path), "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=LOG_FIELDS)
            if new:
                w.writeheader()
            row["timestamp"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            w.writerow({k: row.get(k, "") for k in LOG_FIELDS})
    except OSError as exc:
        log(f"WARNING: could not write log {path}: {exc}", err=True)


def sec_per_page(path: Path) -> dict[str, float]:
    tot: dict[str, list[float]] = {}
    if not path.exists():
        return {}
    try:
        with open(str(path), encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if r.get("success") == "True" and float(r.get("duration_sec") or 0) > 0:
                    t = tot.setdefault(r["method"], [0.0, 0.0])
                    t[0] += float(r["duration_sec"])
                    t[1] += float(r.get("pages") or 1)
    except (OSError, ValueError, KeyError):
        return {}
    return {m: s / p for m, (s, p) in tot.items() if p}


# --------------------------------------------------------------------------------------
# Per-book processing
# --------------------------------------------------------------------------------------
def book_dirname(pdf: Path, all_pdfs: list[Path]) -> str:
    same = [p for p in all_pdfs if p.stem == pdf.stem]
    if len(same) <= 1:
        return pdf.stem
    return f"{pdf.stem}__{hashlib.sha1(str(pdf.parent).encode()).hexdigest()[:6]}"


def get_plan(book: Book, root: Path, args, force_rebuild: bool) -> tuple[dict, bool]:
    """Return (plan, rebuilt). Locked plans are never replaced."""
    plan_path = root / "plan.json"
    sidecar = find_sidecar(book.path)
    old = read_json(plan_path)
    if old and old.get("locked") and not args.ignore_lock:
        if old.get("fingerprint", {}).get("size") != book.fingerprint()["size"]:
            log(f"  WARNING: PDF changed since the locked plan was written; keeping the locked plan", err=True)
        return old, False
    sidecar_cfg = load_mapping(sidecar) if sidecar else None
    if (old and not force_rebuild and old.get("plan_version") == PLAN_VERSION
            and old.get("fingerprint") == book.fingerprint()
            and old.get("settings") == settings_hash(args, sidecar_cfg)):
        return old, False
    plan = build_plan(book, args, sidecar)
    atomic_write(plan_path, json.dumps(plan, indent=2, ensure_ascii=False))
    if args.preview:
        write_preview(book, plan, root / "preview.md", args.preview_pages)
    return plan, True


def describe_plan(plan: dict) -> str:
    units = plan["units"]
    main = [u for u in units if u["kind"] != "front"]
    s = f"{len(main)} unit(s) via {plan['detector']}"
    if plan["detector"] == "whole":
        s = "NO CHAPTERS DETECTED - whole book as one unit (see preview.md, or write a sidecar)"
    if plan.get("locked"):
        s += " [locked]"
    if plan.get("agreement"):
        s += " | agreement " + ", ".join(f"{k} {v:.0%}" for k, v in plan["agreement"].items())
    low = [u for u in units if u.get("confidence", 1) < 0.5 and u["kind"] != "front"]
    if low:
        s += f" | {len(low)} unverified start(s)"
    if plan["text_layer"]["scanned"]:
        s += " | NO TEXT LAYER (scanned?)"
    return s


def unit_stem(book_stem: str, u: Unit, width: int) -> str:
    return f"{book_stem}_chapter{u.index:0{width}d}"


def split_units(book: Book, units: list[Unit], root: Path, state: dict, args) -> dict[str, Path]:
    pdf_dir = root / "pdf"
    pdf_dir.mkdir(parents=True, exist_ok=True)
    width = max(2, len(str(max(u.index for u in units))))
    out = {}
    for u in units:
        stem = unit_stem(book.path.stem, u, width)
        dst = pdf_dir / f"{stem}.pdf"
        rec = state["units"].get(stem, {})
        same_range = rec.get("start") == u.start + 1 and rec.get("end") == u.end + 1
        if not same_range:  # range changed -> previous Markdown is stale
            rec.pop("md", None)
        if args.overwrite or not dst.exists() or not same_range:
            new = pymupdf.open()
            new.insert_pdf(book.doc, from_page=u.start, to_page=u.end)
            meta = {k: v for k, v in (book.doc.metadata or {}).items() if isinstance(v, str)}
            meta.update(title=u.title, subject=f"PDF pages {u.start + 1}-{u.end + 1} of {book.path.name}")
            try:
                new.set_metadata(meta)
            except Exception:
                pass
            new.save(str(dst), garbage=3, deflate=True)
            new.close()
        rec.update(start=u.start + 1, end=u.end + 1, title=u.title, kind=u.kind)
        state["units"][stem] = rec
        out[stem] = dst
    # outputs from an older plan that no longer exist in this one
    for folder, pattern in ((pdf_dir, "*.pdf"), (root / "md", "*.md")):
        if not folder.exists():
            continue
        for f in folder.glob(f"{book.path.stem}_chapter{pattern}"):
            if f.stem not in out:
                if args.prune:
                    f.unlink()
                    log(f"  pruned stale {f.name}")
                else:
                    state.setdefault("orphans", [])
                    if f.name not in state["orphans"]:
                        state["orphans"].append(f.name)
    if args.prune:
        state["orphans"] = []
    for stem in [s for s in state["units"] if s not in out]:
        state["units"].pop(stem)  # records of units that are no longer in the plan
    return out


def convert_units(book: Book, units: list[Unit], pdfs: dict[str, Path], root: Path, state: dict,
                  methods: list[str], args, log_path: Path, save: Callable[[], None]) -> tuple[int, int]:
    if not methods:
        return 0, 0
    md_dir = root / "md"
    work = root / ".work"
    md_dir.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)
    scanned = book.chars_per_page() < 30
    ocr = args.ocr == "on" or (args.ocr == "auto" and scanned)
    if scanned:
        log("  note: little or no text layer; " + ("Marker will run with OCR" if ocr else "OCR is off, output may be empty"))
    best = min(METHOD_RANK[m] for m in methods)
    stems = list(pdfs)
    todo = []
    for stem, u in zip(stems, units):
        rec = state["units"][stem].get("md")
        done = rec and rec.get("ok") and (md_dir / f"{stem}.md").exists()
        if done and not args.overwrite and not (args.retry_fallback and METHOD_RANK[rec["method"]] > best):
            continue
        todo.append((stem, u))
    if not todo:
        log("  all units already converted")
        return 0, 0
    rates = sec_per_page(log_path)
    pages_left = sum(u.end - u.start + 1 for _, u in todo)
    if methods[0] in rates:
        log(f"  {len(todo)} unit(s) to convert, ~{fmt_dur(rates[methods[0]] * pages_left)} with {methods[0]}")

    ok = failed = 0
    for i, (stem, u) in enumerate(todo, 1):
        rec = state["units"][stem]
        prev = rec.get("md")
        chain = methods
        if prev and prev.get("ok") and args.retry_fallback and not args.overwrite:
            chain = [m for m in methods if METHOD_RANK[m] < METHOD_RANK[prev["method"]]]
        npages = u.end - u.start + 1
        ctx = {"work": work, "ocr": ocr, "timeout": args.timeout, "marker_mode": args.marker_mode,
               "marker_args": args.marker_args, "images": args.images, "frontmatter": args.frontmatter,
               "first_page": u.start + 1}
        label = f"  [{i}/{len(todo)}] {stem} ({npages} pp)"
        success = False
        errors = []
        for m in chain:
            t0 = time.time()
            res = None
            try:
                if not pdfs[stem].exists():
                    raise RuntimeError("chapter PDF missing")
                res = CONVERTERS[m](pdfs[stem], ctx)
                if len(res.text.strip()) < 20:
                    raise EmptyOutput("empty output" + (" (no text layer)" if scanned else ""))
                meta = {"book": book.path.stem, "source_pdf": book.path.name, "unit": u.index,
                        "kind": u.kind, "title": u.title, "pdf_pages": [u.start + 1, u.end + 1],
                        "converter": m, "converted": datetime.now().isoformat(timespec="seconds")}
                if u.printed:
                    meta["printed_start"] = u.printed
                chars = finish_markdown(res, u, stem, md_dir, meta, ctx)
                dt = time.time() - t0
                rec["md"] = {"ok": True, "method": m, "seconds": round(dt, 2), "chars": chars,
                             "when": datetime.now().isoformat(timespec="seconds")}
                log_row(log_path, book=book.path.name, unit=stem, pages=npages, method=m,
                        duration_sec=f"{dt:.2f}", success=True, output_chars=chars)
                fell = f" (fallback after: {'; '.join(errors)})" if errors else ""
                log(f"{label} ok via {m} in {fmt_dur(dt)}{fell}")
                success = True
                break
            except Exception as exc:  # noqa: BLE001 - every failure moves on to the next method
                dt = time.time() - t0
                err = f"{m}: {str(exc).strip()[:300]}"
                errors.append(err)
                log_row(log_path, book=book.path.name, unit=stem, pages=npages, method=m,
                        duration_sec=f"{dt:.2f}", success=False, output_chars=0, error=str(exc)[:500])
            finally:
                if res is not None and res.tmp:
                    shutil.rmtree(res.tmp, ignore_errors=True)
        if success:
            ok += 1
            if not args.keep_pdfs:
                pdfs[stem].unlink(missing_ok=True)
        else:
            failed += 1
            if prev and prev.get("ok"):
                log(f"{label} upgrade failed, kept {prev['method']} output: {'; '.join(errors)}")
            else:
                rec["md"] = {"ok": False, "errors": errors, "when": datetime.now().isoformat(timespec="seconds")}
                log(f"{label} FAILED: {'; '.join(errors)}", err=True)
        save()
    shutil.rmtree(work, ignore_errors=True)
    return ok, failed


def combine_book(book_stem: str, stems: list[str], root: Path) -> None:
    parts = []
    for stem in stems:
        md = root / "md" / f"{stem}.md"
        if not md.exists():
            continue
        text = md.read_text(encoding="utf-8")
        if text.startswith("---\n"):
            end = text.find("\n---\n", 4)
            if end != -1:
                text = text[end + 5:]
        parts.append(f"<!-- {stem} -->\n\n{text.strip()}\n")
    if parts:
        atomic_write(root / "md" / f"{book_stem}_full.md", "\n\n".join(parts))


def process_book(pdf: Path, root: Path, args, command: str, methods: list[str], log_path: Path) -> dict:
    root.mkdir(parents=True, exist_ok=True)
    state_path = root / "state.json"
    state = read_json(state_path) or {"units": {}}
    state.setdefault("units", {})

    def save():
        atomic_write(state_path, json.dumps(state, indent=2, ensure_ascii=False))

    book = Book(pdf)
    summary = {"book": pdf.name, "ok": 0, "failed": 0}
    try:
        plan, rebuilt = get_plan(book, root, args, force_rebuild=(command == "plan"))
        log(f"  plan: {describe_plan(plan)}" + ("" if rebuilt else " (existing)"))
        summary["detector"] = plan["detector"]
        if command == "plan":
            return summary
        units = units_from_plan(plan, book.n, args)
        pdfs = split_units(book, units, root, state, args)
        save()
        if command == "split":
            log(f"  split into {len(pdfs)} PDF(s)")
            return summary
        sidecar = find_sidecar(pdf)
        book_methods = methods
        if sidecar:
            cfg = load_mapping(sidecar)
            if cfg.get("skip"):
                log("  skipped (sidecar: skip = true)")
                return summary
            if cfg.get("methods"):
                book_methods = resolve_methods(str(cfg["methods"]), args.marker_mode)
        ok, failed = convert_units(book, units, pdfs, root, state, book_methods, args, log_path, save)
        summary.update(ok=ok, failed=failed)
        if args.combine:
            combine_book(pdf.stem, list(pdfs), root)
        if not args.keep_pdfs and not failed:
            shutil.rmtree(root / "pdf", ignore_errors=True)
    finally:
        book.close()
        save()
    return summary


# --------------------------------------------------------------------------------------
# status and sidecar commands
# --------------------------------------------------------------------------------------
def status(pdfs: list[Path], out: Path) -> None:
    rows = [("book", "pages", "detector", "units", "agreement", "markdown", "methods", "flags")]
    for pdf in pdfs:
        root = out / book_dirname(pdf, pdfs)
        plan = read_json(root / "plan.json")
        state = read_json(root / "state.json") or {"units": {}}
        if not plan:
            rows.append((pdf.name, "", "-", "", "", "", "", "not planned"))
            continue
        units = plan["units"]
        recs = [state["units"].get(k, {}).get("md") or {} for k in state["units"]]
        okc = sum(1 for r in recs if r.get("ok"))
        fail = sum(1 for r in recs if r and not r.get("ok"))
        methods = Counter(r["method"] for r in recs if r.get("ok"))
        flags = []
        if plan.get("locked"):
            flags.append("locked")
        if plan["text_layer"]["scanned"]:
            flags.append("scanned")
        if any(v < 0.6 for v in plan.get("agreement", {}).values()):
            flags.append("LOW-AGREEMENT")
        if any(u.get("confidence", 1) < 0.5 and u["kind"] != "front" for u in units):
            flags.append("unverified-starts")
        if fail:
            flags.append(f"FAILED:{fail}")
        if methods and len(methods) > 1:
            flags.append("mixed-methods")
        try:
            if plan.get("fingerprint", {}).get("size") != pdf.stat().st_size:
                flags.append("PDF-CHANGED")
        except OSError:
            pass
        if state.get("orphans"):
            flags.append(f"orphans:{len(state['orphans'])}")
        agree = ", ".join(f"{k} {v:.0%}" for k, v in plan.get("agreement", {}).items()) or "-"
        rows.append((pdf.name, str(plan["page_count"]), plan["detector"], str(len(units)), agree,
                     f"{okc}/{len(units)}", ", ".join(f"{m}:{c}" for m, c in methods.items()) or "-",
                     " ".join(flags) or "ok"))
    widths = [min(42, max(len(r[i]) for r in rows)) for i in range(len(rows[0]))]
    for j, r in enumerate(rows):
        log("  ".join(c[:widths[i]].ljust(widths[i]) for i, c in enumerate(r)).rstrip())
        if j == 0:
            log("  ".join("-" * w for w in widths))


def write_sidecar(pdf: Path, root: Path) -> None:
    plan = read_json(root / "plan.json")
    if not plan:
        raise RuntimeError("no plan yet; run 'plan' first")
    use_toml = tomllib is not None
    dst = pdf.with_name(pdf.stem + ".bookpipe" + (".toml" if use_toml else ".json"))
    if dst.exists():
        raise RuntimeError(f"{dst.name} already exists; edit it or delete it first")
    units = [u for u in plan["units"] if not (u["kind"] == "front" and u.get("source") == "coverage")]
    if use_toml:
        lines = [f"# bookpipe sidecar for {pdf.name}", "# Starts are PDF page numbers (1-based).",
                 "# To use printed page numbers instead: pages = \"printed\" and offset = <int> or \"auto\".",
                 'pages = "pdf"', "# methods = \"marker,pymupdf4llm\"", "# skip = false", ""]
        for u in units:
            lines += ["[[chapters]]", f"start = {u['start']}", f"title = {json.dumps(u['title'], ensure_ascii=False)}", ""]
        atomic_write(dst, "\n".join(lines))
    else:
        atomic_write(dst, json.dumps({"pages": "pdf", "chapters": [{"start": u["start"], "title": u["title"]} for u in units]},
                                     indent=2, ensure_ascii=False))
    log(f"  wrote {dst}")


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    d = DEFAULTS
    g = common.add_argument_group("input/output")
    g.add_argument("input", nargs="?", type=Path, default=Path("."), help="PDF file or folder of PDFs (default: .)")
    g.add_argument("-o", "--output", type=Path, help=f"output folder (default: {d['output']})")
    g.add_argument("--book", help="only books whose file name matches this glob or substring")
    g.add_argument("--recursive", action=argparse.BooleanOptionalAction, help="search subfolders")
    g.add_argument("--config", type=Path, help="config file (default: bookpipe.toml/.json in the input folder)")

    g = common.add_argument_group("chapter detection")
    g.add_argument("--min-chapters", type=int, help=f"fewest units for a plan to be accepted (default {d['min_chapters']})")
    g.add_argument("--min-chapter-pages", type=int, help=f"shorter units are merged into a neighbour (default {d['min_chapter_pages']})")
    g.add_argument("--toc-scan-pages", type=int, help=f"pages searched for a printed contents (default {d['toc_scan_pages']})")
    g.add_argument("--max-offset", type=int, help=f"largest printed->PDF page offset tried (default {d['max_offset']})")
    g.add_argument("--threshold", type=float, help=f"title match score to verify a page (default {d['threshold']})")
    g.add_argument("--split-front", action=argparse.BooleanOptionalAction, help="keep preface/foreword as own units instead of front matter")
    g.add_argument("--crosscheck", action=argparse.BooleanOptionalAction, help="also run the heading scan to cross-check (default on)")
    g.add_argument("--preview", action=argparse.BooleanOptionalAction, help="write preview.md for diagnosis (default on)")
    g.add_argument("--preview-pages", type=int, help=f"first/last pages shown in preview.md (default {d['preview_pages']})")
    g.add_argument("--ignore-lock", action=argparse.BooleanOptionalAction, help="re-plan even books whose plan.json is locked")

    g = common.add_argument_group("splitting")
    g.add_argument("--overwrite", action=argparse.BooleanOptionalAction, help="redo chapter PDFs and Markdown that already exist")
    g.add_argument("--prune", action=argparse.BooleanOptionalAction, help="delete outputs left over from an older plan")
    g.add_argument("--keep-pdfs", action=argparse.BooleanOptionalAction, help="keep chapter PDFs after conversion (default on)")

    g = common.add_argument_group("conversion")
    g.add_argument("--methods", help="comma list tried in order: marker, pymupdf4llm, basic; or auto / none (default auto)")
    g.add_argument("--timeout", type=int, help=f"per-chapter Marker timeout in seconds (default {d['timeout']})")
    g.add_argument("--ocr", choices=["auto", "on", "off"], help="auto = OCR only books without a text layer (default)")
    g.add_argument("--marker-mode", choices=["cli", "api"], help="cli = marker_single per chapter (default); api = load models once (experimental)")
    g.add_argument("--marker-args", help="extra arguments passed to marker_single, e.g. \"--use_llm\"")
    g.add_argument("--images", action=argparse.BooleanOptionalAction, help="extract images (default on)")
    g.add_argument("--frontmatter", action=argparse.BooleanOptionalAction, help="YAML front matter in each .md (default on)")
    g.add_argument("--retry-fallback", action=argparse.BooleanOptionalAction, help="re-convert units done with a lower-ranked method")
    g.add_argument("--combine", action=argparse.BooleanOptionalAction, help="also write <book>_full.md")

    ap = argparse.ArgumentParser(prog="bookpipe", description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog="Run 'bookpipe.py <command> -h' for options. Full guide at the top of the script.")
    sub = ap.add_subparsers(dest="command")
    for name, helptext in (("run", "plan + split + convert (default)"), ("plan", "detect chapters only"),
                           ("split", "plan + split, no conversion"), ("status", "show progress and problems"),
                           ("sidecar", "write a sidecar file next to each PDF from its plan")):
        sub.add_parser(name, parents=[common], help=helptext)
    return ap


def resolve_args(argv: list[str]) -> argparse.Namespace:
    ap = build_parser()
    if not argv or argv[0] not in ("run", "plan", "split", "status", "sidecar", "-h", "--help"):
        argv = ["run"] + argv
    args = ap.parse_args(argv)
    cfg_path = args.config
    if cfg_path is None:
        base = args.input if args.input.is_dir() else args.input.parent
        for name in ("bookpipe.toml", "bookpipe.json"):
            if (base / name).exists():
                cfg_path = base / name
                break
    cfg = {}
    if cfg_path:
        cfg = {k.replace("-", "_"): v for k, v in load_mapping(cfg_path).items()}
        unknown = set(cfg) - set(DEFAULTS)
        if unknown:
            log(f"WARNING: unknown keys in {cfg_path.name}: {', '.join(sorted(unknown))}", err=True)
    for key, default in DEFAULTS.items():
        if getattr(args, key, None) is None:
            setattr(args, key, cfg.get(key, default))
    args.output = Path(args.output)
    args.methods = str(args.methods)
    return args


def discover(args) -> list[Path]:
    inp: Path = args.input
    if inp.is_file():
        return [inp]
    if not inp.is_dir():
        raise SystemExit(f"Not found: {inp}")
    out = args.output.resolve()
    files = inp.rglob("*") if args.recursive else inp.iterdir()
    pdfs = []
    for f in files:
        if not f.is_file() or f.suffix.lower() != ".pdf":
            continue
        try:
            f.resolve().relative_to(out)
            continue  # never pick up our own chapter PDFs
        except ValueError:
            pass
        if args.book:
            pat = args.book.casefold()
            name = f.name.casefold()
            if not (fnmatch.fnmatch(name, pat) or fnmatch.fnmatch(f.stem.casefold(), pat) or pat in name):
                continue
        pdfs.append(f)
    return sorted(pdfs, key=lambda p: str(p).casefold())


def main(argv: Optional[list[str]] = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    args = resolve_args(sys.argv[1:] if argv is None else argv)
    command = args.command or "run"
    pdfs = discover(args)
    if not pdfs:
        log("No PDF files found.", err=True)
        return 1
    args.output.mkdir(parents=True, exist_ok=True)
    if command == "status":
        status(pdfs, args.output)
        return 0
    if command == "sidecar":
        for pdf in pdfs:
            log(pdf.name)
            try:
                write_sidecar(pdf, args.output / book_dirname(pdf, pdfs))
            except Exception as exc:
                log(f"  {exc}", err=True)
        return 0

    methods = resolve_methods(args.methods, args.marker_mode) if command == "run" else []
    if command == "run":
        log(f"Conversion chain: {' -> '.join(methods) if methods else 'none (split only)'}")
        if methods and methods[0] != "marker" and args.methods == "auto":
            log("  tip: pip install marker-pdf for the best results on maths-heavy books")
    log_path = args.output / "bookpipe_log.csv"
    t0 = time.time()
    summaries = []
    for i, pdf in enumerate(pdfs, 1):
        log(f"\n[{i}/{len(pdfs)}] {pdf.name}")
        try:
            summaries.append(process_book(pdf, args.output / book_dirname(pdf, pdfs), args, command, methods, log_path))
        except KeyboardInterrupt:
            log("\nInterrupted; progress so far is saved. Rerun the same command to resume.", err=True)
            return 130
        except Exception as exc:  # noqa: BLE001
            log(f"  ERROR: {type(exc).__name__}: {exc}", err=True)
            summaries.append({"book": pdf.name, "error": str(exc)})
    log(f"\nDone in {fmt_dur(time.time() - t0)}: {len(pdfs)} book(s)")
    if command == "run":
        ok = sum(s.get("ok", 0) for s in summaries)
        failed = sum(s.get("failed", 0) for s in summaries)
        log(f"  converted {ok} unit(s), {failed} failed; see '{Path(sys.argv[0]).name} status' and {log_path}")
    errors = [s for s in summaries if "error" in s]
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
