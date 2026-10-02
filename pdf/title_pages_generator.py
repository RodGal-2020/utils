from pathlib import Path
import re
import subprocess

md_file = next(Path(".").glob("*.md"))
lines = [
    line.strip()
    for line in md_file.read_text(encoding="utf-8").splitlines()
    if line.strip()
]

for i, title in enumerate(lines, 1):
    title = re.sub(r"^#+\s*", "", title)

    # Filename
    filename = re.sub(r'[<>:"/\\|?*]', "", title)
    filename = re.sub(r"\s+", "_", filename).strip("_")
    filename = f"{i:02d}_{filename}_title.pdf"

    # Escape LaTeX special characters
    latex_title = re.sub(r"([&%$#{}_])", r"\\\1", title)

    tex = rf"""
\documentclass{{article}}
\usepackage{{fontspec}}
\usepackage{{geometry}}
\geometry{{margin=2cm}}

\begin{{document}}

\begin{{center}}
\Huge\bfseries {latex_title}
\end{{center}}

\end{{document}}
"""

    tex_file = Path(filename).with_suffix(".tex")
    tex_file.write_text(tex, encoding="utf-8")

    subprocess.run(
        [
            "xelatex",
            "-interaction=nonstopmode",
            "-halt-on-error",
            "-jobname",
            Path(filename).stem,
            str(tex_file),
        ],
        check=True,
    )

    # Remove auxiliary files
    for ext in [".tex", ".aux", ".log", ".out"]:
        Path(filename).with_suffix(ext).unlink(missing_ok=True)