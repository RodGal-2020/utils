#!/usr/bin/env python3

from pathlib import Path
import argparse
import csv
import json
import mimetypes


# Common source/document files: include their full contents.
TEXT_EXTENSIONS = {
    ".md", ".markdown", ".txt",
    ".py", ".pyw", ".r", ".rmd", ".qmd",
    ".jl", ".rs", ".go", ".java",
    ".c", ".h", ".cc", ".cpp", ".cxx", ".hpp",
    ".js", ".jsx", ".ts", ".tsx",
    ".sh", ".bash", ".zsh", ".fish",
    ".sql",
    ".html", ".htm", ".css", ".scss", ".less",
    ".tex", ".sty", ".cls", ".bib",
    ".xml", ".svg",
    ".yml", ".yaml", ".toml", ".ini", ".cfg", ".conf",
    ".dockerfile",
}

# Structured data: show only a glimpse.
DATA_EXTENSIONS = {
    ".csv", ".tsv",
    ".json", ".jsonl", ".ndjson",
    ".parquet", ".feather",
    ".rds", ".rdata",
    ".xls", ".xlsx",
}

# Usually not useful to dump recursively.
SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    "node_modules",
    ".cache",
}


def human_size(size):
    for unit in ["B", "KB", "MB", "GB"]:
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def is_binary(path):
    try:
        with path.open("rb") as f:
            chunk = f.read(4096)
        return b"\x00" in chunk
    except OSError:
        return True


def read_text(path):
    for encoding in ("utf-8", "utf-8-sig", "latin-1"):
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            pass
    return None


def describe_binary(path):
    mime, _ = mimetypes.guess_type(path.name)
    size = human_size(path.stat().st_size)

    description = f"- Type: `{mime or 'unknown/binary'}`\n- Size: `{size}`"

    if mime and mime.startswith("image/"):
        try:
            from PIL import Image
            with Image.open(path) as img:
                description += f"\n- Image dimensions: `{img.width} × {img.height}`"
        except Exception:
            pass

    elif mime == "application/pdf":
        try:
            import fitz  # PyMuPDF
            with fitz.open(path) as pdf:
                description += f"\n- PDF pages: `{len(pdf)}`"
        except Exception:
            pass

    return description


def data_preview(path, max_rows=8, max_chars=2500):
    ext = path.suffix.lower()

    if ext in {".csv", ".tsv"}:
        delimiter = "\t" if ext == ".tsv" else ","

        text = read_text(path)
        if text is None:
            return "_Could not decode file._"

        rows = list(csv.reader(text.splitlines(), delimiter=delimiter))[:max_rows]

        if not rows:
            return "_Empty file._"

        # Markdown table
        header = rows[0]
        output = [
            "| " + " | ".join(header) + " |",
            "| " + " | ".join("---" for _ in header) + " |",
        ]

        for row in rows[1:]:
            row = row + [""] * (len(header) - len(row))
            output.append("| " + " | ".join(row[:len(header)]) + " |")

        return "\n".join(output)

    if ext == ".json":
        try:
            data = json.loads(path.read_text(encoding="utf-8"))

            if isinstance(data, dict):
                preview = dict(list(data.items())[:10])
            elif isinstance(data, list):
                preview = data[:5]
            else:
                preview = data

            result = json.dumps(preview, indent=2, ensure_ascii=False)
            if len(result) > max_chars:
                result = result[:max_chars] + "\n..."

            return f"```json\n{result}\n```"

        except Exception:
            text = read_text(path)
            return f"```text\n{text[:max_chars]}\n```" if text else "_Could not read._"

    if ext in {".jsonl", ".ndjson"}:
        text = read_text(path)
        if not text:
            return "_Empty or unreadable file._"

        lines = [line for line in text.splitlines() if line.strip()][:5]
        return "```json\n" + "\n".join(lines) + "\n```"

    # Binary/structured data where no parser is assumed.
    return (
        f"- Size: `{human_size(path.stat().st_size)}`\n"
        f"- Structured data file: `{ext}`\n"
        "- Preview: parser not enabled; metadata shown only."
    )


def build_tree(root, skip_dirs):
    lines = [f"{root.name}/"]

    def recurse(directory, prefix=""):
        entries = sorted(
            [p for p in directory.iterdir()
             if not (p.is_dir() and p.name in skip_dirs)],
            key=lambda p: (not p.is_dir(), p.name.lower())
        )

        for i, path in enumerate(entries):
            last = i == len(entries) - 1
            branch = "└── " if last else "├── "
            lines.append(prefix + branch + path.name)

            if path.is_dir():
                recurse(
                    path,
                    prefix + ("    " if last else "│   ")
                )

    recurse(root)
    return "\n".join(lines)


def process_file(path, root):
    rel = path.relative_to(root)
    ext = path.suffix.lower()

    output = [f"\n## `{rel}`\n"]

    try:
        size = path.stat().st_size
    except OSError:
        return output + ["_Unable to access file._\n"]

    if ext in DATA_EXTENSIONS:
        output.append("**Data file — preview only.**\n")
        output.append(data_preview(path))
        return output

    if ext in TEXT_EXTENSIONS or not is_binary(path):
        text = read_text(path)

        if text is None:
            output.append("**Binary/unreadable file**\n")
            output.append(describe_binary(path))
            return output

        output.append("**Full contents:**\n")
        output.append(f"```text\n{text}\n```")
        return output

    output.append("**Binary/non-text file — description only.**\n")
    output.append(describe_binary(path))
    return output


def main():
    parser = argparse.ArgumentParser(
        description="Mix the contents of a folder into one Markdown file."
    )
    parser.add_argument("folder", type=Path)
    parser.add_argument(
        "-o", "--output",
        type=Path,
        default=Path("folder_dump.md"),
        help="Output Markdown file (default: folder_dump.md)"
    )
    args = parser.parse_args()

    root = args.folder.resolve()
    output_path = args.output.resolve()

    if not root.is_dir():
        raise SystemExit(f"Not a directory: {root}")

    files = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.relative_to(root).parts):
            continue
        if path.resolve() == output_path:
            continue
        files.append(path)

    files.sort(key=lambda p: str(p.relative_to(root)).lower())

    result = [
        f"# Folder dump: `{root.name}`\n",
        "## Folder tree\n",
        "```text",
        build_tree(root, SKIP_DIRS),
        "```\n",
        "## File contents\n",
    ]

    for path in files:
        result.extend(process_file(path, root))

    output_path.write_text("\n".join(result), encoding="utf-8")

    print(f"Created: {output_path}")
    print(f"Files processed: {len(files)}")


if __name__ == "__main__":
    main()
