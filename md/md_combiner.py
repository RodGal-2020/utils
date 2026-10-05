from pathlib import Path

folder = Path(__file__).parent
output = folder / "combined.md"

extensions = {
    ".rmd",
    ".csv",
    ".txt",
    ".kml",
    ".md",
    ".rds",
    ".rdata",
}

with output.open("w", encoding="utf-8") as out:

    for path in sorted(folder.rglob("*")):
        if not path.is_file():
            continue

        if path == output:
            continue

        if path.suffix.lower() not in extensions:
            continue

        out.write(f"\n\n{'=' * 80}\n")
        out.write(f"FILE: {path.relative_to(folder)}\n")
        out.write(f"{'=' * 80}\n\n")

        # Binary R files are detected but not read
        if path.suffix.lower() in {".rds", ".rdata"}:
            out.write("[Binary R file — contents not displayed]\n")
            continue

        try:
            out.write(path.read_text(encoding="utf-8"))
        except UnicodeDecodeError:
            out.write("[Could not decode file as UTF-8]\n")

print(f"Created: {output}")
