#!/usr/bin/env python3
"""Split book PDFs into chapter PDFs and optionally convert each chapter to Markdown.

Detection priority:
1. Embedded PDF outline/bookmarks (authoritative when usable).
2. Printed table of contents parsed from a fast text preview of the first/last pages.
3. Heading scan across the book to resolve printed page-number offsets.

Outputs are deterministic: bookname_chapterXX.pdf / .md, plus a manifest and
an optional edge-preview Markdown file for diagnosis.
"""
from __future__ import annotations

import argparse
import difflib
import json
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Optional

try:
    import pymupdf
except ImportError:
    try:
        import fitz as pymupdf
    except ImportError as exc:
        raise SystemExit("Missing dependency: pip install pymupdf") from exc

CHAPTER_RE = re.compile(r"^(chapter|chapitre|cap[ií]tulo|capitol|kapitel|part|book)\s+([\divxlcdm]+)\b", re.I)
TOC_HEAD_RE = re.compile(r"^\s*(table\s+of\s+contents|contents|contenido|contenidos|[ií]ndice|sumario)\s*$", re.I)
TOC_LINE_RE = re.compile(r"^\s*(?P<title>.+?)\s*(?:\.{2,}|\s{2,})\s*(?P<page>[ivxlcdm]+|\d+)\s*$", re.I)
BACK_RE = re.compile(r"^(references|bibliography|appendix|appendices|index|glossary|notes)\b", re.I)

@dataclass
class Chapter:
    number: int
    title: str
    start_page: int
    end_page: int = -1
    source: str = ""
    confidence: float = 0.0
    printed_page: Optional[str] = None


def clean(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip(" .\t")


def norm(s: str) -> str:
    s = clean(s).casefold()
    return re.sub(r"[^\w]+", " ", s).strip()


def roman_to_int(s: str) -> Optional[int]:
    if not re.fullmatch(r"[ivxlcdm]+", s, re.I): return None
    vals = {'i':1,'v':5,'x':10,'l':50,'c':100,'d':500,'m':1000}
    total, prev = 0, 0
    for ch in reversed(s.lower()):
        v = vals[ch]; total += -v if v < prev else v; prev = max(prev, v)
    return total


def edge_pages(n: int, k: int) -> list[int]:
    return sorted(set(range(min(k, n))) | set(range(max(0, n-k), n)))


def page_text(doc, pno: int) -> str:
    try: return doc[pno].get_text("text", sort=True) or ""
    except Exception: return ""


def write_edge_preview(doc, source: Path, out_dir: Path, count: int) -> Path:
    path = out_dir / f"{source.stem}_edge_preview.md"
    parts = [f"# Edge preview: {source.name}\n"]
    for p in edge_pages(doc.page_count, count):
        parts += [f"\n## PDF page {p+1}\n", page_text(doc, p)]
    path.write_text("\n".join(parts), encoding="utf-8")
    return path


def bookmark_candidates(doc, include_back: bool) -> list[Chapter]:
    toc = doc.get_toc(simple=True) or []
    valid = [(int(l), clean(str(t)), int(p)-1) for l,t,p in toc if isinstance(p,(int,float)) and 1 <= int(p) <= doc.page_count]
    if len(valid) < 2: return []
    levels = sorted({x[0] for x in valid})
    ranked = []
    for level in levels:
        rows = [x for x in valid if x[0] == level]
        strong = sum(bool(CHAPTER_RE.search(t)) for _,t,_ in rows)
        unique = len({p for _,_,p in rows})
        if unique >= 2:
            ranked.append((strong / len(rows), strong, -level, rows))
    if not ranked: return []
    _, strong, _, rows = max(ranked, key=lambda x: (x[0], x[1], len(x[3]), x[2]))
    # If explicit chapter-like entries exist, prefer them; otherwise the chosen major level.
    explicit = [r for r in valid if CHAPTER_RE.search(r[1])]
    if len({r[2] for r in explicit}) >= 2: rows = explicit
    seen, out = set(), []
    for _, title, p in sorted(rows, key=lambda x: x[2]):
        if p in seen or (BACK_RE.search(title) and not include_back): continue
        if title.casefold() in {"contents", "table of contents", "index"}: continue
        seen.add(p); out.append(Chapter(len(out)+1, title, p, source="bookmarks", confidence=1.0))
    return out if len(out) >= 2 else []


def parse_printed_toc(doc, scan_pages: int) -> list[tuple[str,str]]:
    texts = [page_text(doc, p) for p in range(min(scan_pages, doc.page_count))]
    starts = [i for i,t in enumerate(texts) if any(TOC_HEAD_RE.match(clean(line)) for line in t.splitlines())]
    if not starts: return []
    rows = []
    for p in range(starts[0], min(starts[0]+8, len(texts))):
        lines = [clean(x) for x in texts[p].splitlines() if clean(x)]
        i = 0
        while i < len(lines):
            line = lines[i]
            m = TOC_LINE_RE.match(line)
            if not m and i+1 < len(lines):
                m = TOC_LINE_RE.match(line + "  " + lines[i+1])
                if m: i += 1
            if m:
                title, pg = clean(m.group("title")), m.group("page")
                if len(title) >= 3 and not TOC_HEAD_RE.match(title): rows.append((title, pg))
            i += 1
    # Major entries are authoritative. If there are enough explicit chapters, discard sections.
    major = [(t,p) for t,p in rows if CHAPTER_RE.search(t) or BACK_RE.search(t)]
    return major if len(major) >= 2 else rows


def score_title(title: str, text: str) -> float:
    target = norm(title)
    if not target: return 0.0
    lines = [norm(x) for x in text.splitlines()[:35] if norm(x)]
    if target in " ".join(lines[:12]): return 1.0
    return max((difflib.SequenceMatcher(None, target, x).ratio() for x in lines), default=0.0)


def resolve_printed(doc, rows: list[tuple[str,str]], radius: int, threshold: float, include_back: bool) -> list[Chapter]:
    # Build candidate offsets using Arabic TOC numbers and matching page labels where available.
    labels = []
    try: labels = [doc[p].get_label() for p in range(doc.page_count)]
    except Exception: labels = [""] * doc.page_count
    resolved, last = [], -1
    for title, printed in rows:
        if BACK_RE.search(title) and not include_back: continue
        num = int(printed) if printed.isdigit() else roman_to_int(printed)
        candidates = {i for i,l in enumerate(labels) if l and l.casefold() == printed.casefold()}
        if num:
            # Common front-matter offsets, constrained after previous chapter.
            for offset in range(0, min(80, doc.page_count)):
                p = num - 1 + offset
                if 0 <= p < doc.page_count: candidates.add(p)
        # Prefer plausible monotonic pages and search around each estimate.
        expanded = set()
        for p in candidates:
            expanded.update(range(max(last+1, p-radius), min(doc.page_count, p+radius+1)))
        if not expanded:
            expanded = set(range(last+1, doc.page_count))
        best_p, best_s = -1, 0.0
        for p in sorted(expanded):
            s = score_title(title, page_text(doc, p))
            if s > best_s: best_p, best_s = p, s
        if best_p >= 0 and best_s >= threshold and best_p > last:
            resolved.append(Chapter(len(resolved)+1, title, best_p, source="printed_toc", confidence=round(best_s,3), printed_page=printed))
            last = best_p
    return resolved if len(resolved) >= 2 else []


def finalize(chapters: list[Chapter], page_count: int, min_pages: int) -> list[Chapter]:
    chapters = sorted({c.start_page: c for c in chapters}.values(), key=lambda c: c.start_page)
    out = []
    for i,c in enumerate(chapters):
        c.end_page = (chapters[i+1].start_page - 1) if i+1 < len(chapters) else page_count-1
        if c.end_page - c.start_page + 1 >= min_pages: out.append(c)
    for i,c in enumerate(out,1): c.number = i
    return out


def split_pdf(doc, source: Path, chapters: list[Chapter], pdf_dir: Path, overwrite: bool) -> list[Path]:
    pdf_dir.mkdir(parents=True, exist_ok=True); width = max(2, len(str(len(chapters))))
    outputs = []
    for c in chapters:
        out = pdf_dir / f"{source.stem}_chapter{c.number:0{width}d}.pdf"
        outputs.append(out)
        if out.exists() and not overwrite: continue
        dst = pymupdf.open(); dst.insert_pdf(doc, from_page=c.start_page, to_page=c.end_page)
        meta = dict(doc.metadata or {}); meta["title"] = c.title; meta["subject"] = f"Chapter extracted from {source.name}"
        dst.set_metadata({k:v for k,v in meta.items() if isinstance(v,str)})
        dst.save(out, garbage=3, deflate=True); dst.close()
    return outputs


def find_marker_command() -> Optional[str]:
    """Find marker_single in PATH or beside the active venv interpreter."""
    marker = shutil.which("marker_single")
    if marker:
        return marker
    bindir = Path(sys.executable).parent
    for candidate in (
        bindir / "marker_single",
        bindir / "marker_single.exe",
        bindir / "Scripts" / "marker_single.exe",
    ):
        if candidate.exists():
            return str(candidate)
    return None


def available_conversion_methods():
    """Return installed methods in preferred order."""
    methods = []
    if find_marker_command():
        methods.append(("marker", "Marker (BEST for mathematical equations)", "marker-pdf"))
    try:
        import pymupdf4llm  # noqa: F401
        methods.append(("pymupdf", "PyMuPDF4LLM (GOOD / faster)", "pymupdf4llm"))
    except Exception:
        pass
    methods.append(("basic", "PyMuPDF basic (FALLBACK)", "pymupdf-basic"))
    return methods


def choose_conversion_method(requested: Optional[str]) -> str:
    methods = available_conversion_methods()
    keys = {key for key, _, _ in methods}
    if requested != "ask":
        if requested == "none":
            return requested
        if requested not in keys:
            raise RuntimeError(
                f"Method '{requested}' is unavailable. Available: {', '.join(sorted(keys))}"
            )
        return requested

    print("\nAvailable conversion methods:")
    for index, (_, label, _) in enumerate(methods, 1):
        print(f"  {index}. {label}" + (" [default]" if index == 1 else ""))
    print(f"  {len(methods) + 1}. Split only; do not create Markdown")

    if not sys.stdin.isatty():
        print(f"Non-interactive input: using {methods[0][1]}")
        return methods[0][0]

    answer = input(f"Choose method [1-{len(methods) + 1}, default 1]: ").strip()
    if not answer:
        return methods[0][0]
    try:
        choice = int(answer)
    except ValueError as exc:
        raise RuntimeError("Enter a method number") from exc
    if choice == len(methods) + 1:
        return "none"
    if not 1 <= choice <= len(methods):
        raise RuntimeError("Invalid method number")
    return methods[choice - 1][0]


def method_directory(method: str) -> str:
    return next(
        (directory for key, _, directory in available_conversion_methods() if key == method),
        method,
    )


def convert(pdf: Path, output_root: Path, method: str, overwrite: bool, timeout: int) -> None:
    """Convert one chapter using the selected method."""
    if method == "none":
        return

    output_dir = output_root / method_directory(method)
    output_dir.mkdir(parents=True, exist_ok=True)

    if method == "marker":
        marker = find_marker_command()
        if not marker:
            raise RuntimeError("marker_single not found; install marker-pdf in the active venv")
        chapter_dir = output_dir / pdf.stem
        expected_md = chapter_dir / f"{pdf.stem}.md"
        if expected_md.exists() and not overwrite:
            return
        if overwrite and chapter_dir.exists():
            shutil.rmtree(chapter_dir)
        result = subprocess.run(
            [marker, str(pdf), "--output_dir", str(output_dir), "--disable_ocr"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            error = (result.stderr or result.stdout or "unknown Marker error").strip()
            raise RuntimeError(error[:4000])
        flat_md = output_dir / f"{pdf.stem}.md"
        candidates = list(chapter_dir.glob("*.md")) if chapter_dir.exists() else []
        if not expected_md.exists() and not flat_md.exists() and len(candidates) != 1:
            raise RuntimeError(f"Marker completed but output was not found under {output_dir}")
        return

    md = output_dir / f"{pdf.stem}.md"
    if md.exists() and not overwrite:
        return
    if method == "pymupdf":
        import pymupdf4llm
        md.write_text(pymupdf4llm.to_markdown(str(pdf)), encoding="utf-8")
    elif method == "basic":
        with pymupdf.open(pdf) as doc:
            text = "\n\n".join(
                f"# Page {i + 1}\n\n{page_text(doc, i)}" for i in range(doc.page_count)
            )
        md.write_text(text, encoding="utf-8")


def process(source: Path, args) -> None:
    root = args.output / source.stem; root.mkdir(parents=True, exist_ok=True)
    with pymupdf.open(source) as doc:
        preview = write_edge_preview(doc, source, root, args.edge_pages) if args.edge_preview else None
        chapters = bookmark_candidates(doc, args.include_back_matter)
        method = "bookmarks" if chapters else ""
        printed = []
        if not chapters:
            printed = parse_printed_toc(doc, args.toc_scan_pages)
            chapters = resolve_printed(doc, printed, args.search_radius, args.threshold, args.include_back_matter)
            method = "printed_toc" if chapters else "none"
        chapters = finalize(chapters, doc.page_count, args.min_chapter_pages)
        manifest = {"source_pdf": str(source), "page_count": doc.page_count, "detection_method": method,
                    "edge_preview": str(preview) if preview else None, "printed_toc_candidates": printed,
                    "chapters": [asdict(c) for c in chapters]}
        (root / f"{source.stem}_chapters.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
        if len(chapters) < args.min_chapters:
            print(f"WARN {source.name}: no reliable chapter plan; manifest written", file=sys.stderr); return
        print(f"{source.name}: {len(chapters)} chapters via {method}")
        if args.dry_run: return
        pdfs = split_pdf(doc, source, chapters, root / "pdf", args.overwrite)
    for pdf in pdfs:
        try:
            convert(pdf, root / "md", args.md_method, args.overwrite, args.timeout)
        except subprocess.TimeoutExpired:
            print(f"ERROR converting {pdf.name}: timed out after {args.timeout} seconds", file=sys.stderr)
        except Exception as exc:
            print(f"ERROR converting {pdf.name}: {exc}", file=sys.stderr)
    if not args.keep_pdfs and args.md_method != "none":
        shutil.rmtree(root / "pdf", ignore_errors=True)

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    ap.add_argument("input", type=Path, help="A PDF or directory containing PDFs")
    ap.add_argument("-o", "--output", type=Path, default=Path("book_chapters"))
    ap.add_argument(
        "--md-method",
        choices=["ask", "marker", "pymupdf", "basic", "none"],
        default="ask",
        help="Ask interactively by default; Enter selects the best available method",
    )
    ap.add_argument("--timeout", type=int, default=300, help="Per-chapter Marker timeout in seconds")
    ap.add_argument("--edge-pages", type=int, default=15)
    ap.add_argument("--edge-preview", action=argparse.BooleanOptionalAction, default=True)
    ap.add_argument("--toc-scan-pages", type=int, default=35)
    ap.add_argument("--search-radius", type=int, default=10)
    ap.add_argument("--threshold", type=float, default=0.62)
    ap.add_argument("--min-chapters", type=int, default=2)
    ap.add_argument("--min-chapter-pages", type=int, default=2)
    ap.add_argument("--include-back-matter", action=argparse.BooleanOptionalAction, default=True)
    ap.add_argument("--keep-pdfs", action=argparse.BooleanOptionalAction, default=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--overwrite", action="store_true")
    args = ap.parse_args()
    try:
        args.md_method = choose_conversion_method(args.md_method)
    except RuntimeError as exc:
        ap.error(str(exc))
    print(f"Selected conversion method: {args.md_method}")
    args.output.mkdir(parents=True, exist_ok=True)
    sources = [args.input] if args.input.is_file() else sorted(args.input.glob("*.pdf"))
    if not sources: ap.error("No PDF files found")
    for source in sources:
        try: process(source, args)
        except Exception as e: print(f"ERROR {source}: {e}", file=sys.stderr)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
