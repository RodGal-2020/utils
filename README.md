# utils

Small personal utilities for **PDFs, images, Markdown, and bibliographic/CSV files**.

## Utilities

### `pdf/title_pages_generator.py`
Generates title pages for PDF documents.

```bash
python title_pages_generator.py
```

### `congress/name_fix.py`
Utility for processing/fixing names in congress-related files.

```bash
python name_fix.py
```

### `img/compile_mermaid.py`
Compiles a Mermaid `.mmd` diagram into an image.

```bash
python compile_mermaid.py
```

Example:

```text
diagram.mmd → diagram.png
```

### `img/instagramify.py`
Converts images to an Instagram-friendly **1080×1350 (4:5)** format while preserving their aspect ratio.

```bash
python instagramify.py
```

### `md/md_combiner.py`
Recursively combines supported files into a single Markdown file.

```bash
python md_combiner.py
```

Example:

```text
project/
├── data.csv
├── notes.md
└── scripts/
    └── analysis.R

        ↓

combined.md
```

### `bib/merge_bib_csv.py`
Merges all BibTeX files and all CSV files in a directory into separate output files.

```bash
python merge_bib_csv.py
```

Example:

```text
paper1.bib + paper2.bib → merged.bib
data1.csv  + data2.csv  → merged.csv
```
