# Book PDF to Markdown

This utility splits book PDFs into chapter-level PDFs and Markdown files. It uses embedded PDF bookmarks first, then falls back to detecting a printed table of contents and matching chapter headings.

## Files

Keep these files in the same directory:

```text
book_pdf2md.py
requirements-book-pdf2md.txt
run_books.sh
```

## Quick start on Omarchy

Make the launcher executable:

```bash
chmod +x run_books.sh
```

Run it with one or more PDFs:

```bash
./run_books.sh ~/Documents/book1.pdf ~/Documents/book2.pdf
```

The script automatically:

1. Creates a `.venv` virtual environment if needed.
2. Activates the environment.
3. Installs or updates the required Python packages.
4. Copies the supplied PDFs into `books/` without overwriting existing copies.
5. Processes all PDFs in `books/`.
6. Writes the results to `results/`.

## Output

For each book, the resulting structure is similar to:

```text
results/
└── book1/
    ├── book1_edge_preview.md
    ├── book1_chapters.json
    ├── pdf/
    │   ├── book1_chapter01.pdf
    │   └── book1_chapter02.pdf
    └── md/
        ├── book1_chapter01.md
        └── book1_chapter02.md
```

The edge preview contains text from the first and last 15 pages. The JSON manifest records the detected chapter titles, page boundaries, detection method, and confidence values.

## Run the Python utility directly

Activate the environment:

```bash
source .venv/bin/activate
```

Inspect chapter detection without splitting:

```bash
python book_pdf2md.py ~/Documents/book1.pdf --dry-run
```

Process a PDF normally:

```bash
python book_pdf2md.py ~/Documents/book1.pdf -o results/
```

Process every PDF in a directory:

```bash
python book_pdf2md.py books/ -o results/
```

Deactivate the environment when finished:

```bash
deactivate
```

## Useful options

Create chapter PDFs without Markdown conversion:

```bash
python book_pdf2md.py books/ -o results/ --md-method none
```

Use simple text extraction instead of `pymupdf4llm`:

```bash
python book_pdf2md.py books/ -o results/ --md-method basic
```

Recreate existing output files:

```bash
python book_pdf2md.py books/ -o results/ --overwrite
```

Exclude back matter such as appendices, bibliography, and index entries:

```bash
python book_pdf2md.py books/ -o results/ --no-include-back-matter
```

## Notes

- Existing files copied into `books/` are not overwritten.
- Embedded bookmarks are treated as the preferred chapter boundaries.
- If reliable bookmarks are unavailable, the utility searches for a printed contents page and tries to resolve its page numbering.
- If no reliable chapter plan is found, the utility writes diagnostic files but does not make uncertain cuts.
