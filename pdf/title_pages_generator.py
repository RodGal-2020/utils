from pathlib import Path
import re
import subprocess

md_file = next(Path(".").glob("*.md"))
lines = [line.strip() for line in md_file.read_text().splitlines() if line.strip()]

for i, title in enumerate(lines, 1):
    # Remove Markdown heading markers
    title = re.sub(r"^#+\s*", "", title)

    # Filename: number + title, with unsafe characters removed
    filename = re.sub(r'[<>:"/\\|?*]', "", title)
    filename = re.sub(r"\s+", "_", filename).strip("_")
    filename = f"{i:02d}_{filename}.pdf"

    # Escape LaTeX-sensitive characters
    latex_title = title.replace("\\", r"\textbackslash{}")
    latex_title = re.sub(r"([&%$#{}_])", r"\\\1", latex_title)

    tex = rf"""
\documentclass{{article}}
\usepackage{{geometry}}
\geometry{{margin=2cm}}
\begin{{document}}
\begin{{center}}
\Huge\bfseries {latex_title}
\end{{center}}
\end{{document}}
"""

    tex_file = Path(filename).with_suffix(".tex")
    tex_file.write_text(tex)

    subprocess.run(
        ["pdflatex", "-interaction=nonstopmode", "-halt-on-error",
         "-jobname", Path(filename).stem, str(tex_file)],
        stdout=subprocess.DEVNULL,
        check=True
    )

    tex_file.unlink()
    for ext in [".aux", ".log"]:
        Path(filename).with_suffix(ext).unlink(missing_ok=True)