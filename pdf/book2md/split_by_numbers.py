import sys
from pathlib import Path
from pypdf import PdfReader, PdfWriter

if len(sys.argv) != 2:
    sys.exit("Usage: python split_by_numbers.py <input.pdf>")

PDF = Path(sys.argv[1])
OFFSET = 0  # PDF pages before printed page 1

chapters = {
    1: 1, 2: 13, 3: 25, 4: 41, 5: 57, 6: 71,
    7: 89, 8: 103, 9: 115, 10: 135, 11: 151,
    12: 167, 13: 181, 14: 195, 15: 207, 16: 231,
    17: 245, 18: 269, 19: 285, 20: 299, 21: 313,
    22: 329, 23: 341, 24: 361, 25: 373, 26: 383,
    27: 399, 28: 415,
}

reader = PdfReader(PDF)
output_dir = Path("chapters")
output_dir.mkdir(exist_ok=True)

starts = list(chapters.items())

for i, (chapter, start) in enumerate(starts):
    end = starts[i + 1][1] - 1 if i + 1 < len(starts) else 428

    first = start + OFFSET - 1
    last = end + OFFSET  # Exclusive

    if first < 0 or last > len(reader.pages):
        print(f"Skipping chapter {chapter}: page range exceeds PDF length.")
        continue

    writer = PdfWriter()
    for page in range(first, last):
        writer.add_page(reader.pages[page])

    output = output_dir / f"chapter_{chapter:02d}.pdf"
    with output.open("wb") as f:
        writer.write(f)

    print(f"Created {output} (printed pages {start}-{end})")

print("Done.")
