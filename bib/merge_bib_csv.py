from pathlib import Path
import pandas as pd

folder = Path(__file__).parent

# -----------------
# Merge BibTeX files
# -----------------

bib_files = sorted(folder.glob("*.bib"))

if bib_files:
    bib_output = folder / "merged.bib"

    with bib_output.open("w", encoding="utf-8") as out:
        for path in bib_files:
            out.write(f"% ===== {path.name} =====\n\n")
            out.write(path.read_text(encoding="utf-8"))
            out.write("\n\n")

    print(f"Created: {bib_output}")


# -----------------
# Merge CSV files
# -----------------

csv_files = sorted(folder.glob("*.csv"))

if csv_files:
    dataframes = []

    for path in csv_files:
        df = pd.read_csv(path)
        dataframes.append(df)

    merged = pd.concat(dataframes, ignore_index=True)

    csv_output = folder / "merged.csv"
    merged.to_csv(csv_output, index=False)

    print(f"Created: {csv_output}")
