import sys
import subprocess
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

words = sys.argv[1:]
if not words:
    print("Usage: python name_fix.py word1 word2 ...")
    sys.exit(1)

# Common professional / conference-style fonts
font_names = [
    "Liberation Sans",
    "Liberation Sans Narrow",
    "Liberation Serif",
    "Carlito",
    "Caladea",
    "Lato",
    "Lato Heavy",
    "Noto Sans",
    "Noto Sans Display",
    "Noto Serif",
    "DejaVu Sans",
    "DejaVu Sans Condensed",
    "DejaVu Serif",
    "Nimbus Sans",
    "Nimbus Roman",
    "URW Gothic",
    "URW Bookman",
    "URW Palladio",
]

fonts = []

for i, name in enumerate(font_names):
    try:
        path = subprocess.check_output(
            ["fc-match", "-f", "%{file}", name],
            text=True
        ).strip()

        font_id = f"font{i}"
        pdfmetrics.registerFont(TTFont(font_id, path))
        fonts.append(font_id)
    except Exception:
        pass

PAGE_W, PAGE_H = 595, 842
MARGIN = 20

# Dense grid
COLS = 4
CELL_W = (PAGE_W - 2 * MARGIN) / COLS
ROW_H = 42

c = canvas.Canvas("font_samples.pdf", pagesize=(PAGE_W, PAGE_H))

x = MARGIN
y = PAGE_H - MARGIN - ROW_H

for word in words:
    for font in fonts:

        # Different sizes, cycling through them
        for size in [12, 14, 16, 18, 20, 24, 28]:

            if x + CELL_W > PAGE_W - MARGIN:
                x = MARGIN
                y -= ROW_H

            if y < MARGIN:
                c.showPage()
                x = MARGIN
                y = PAGE_H - MARGIN - ROW_H

            c.setFont(font, size)

            # Vertically center approximately
            c.drawString(x, y, word)

            x += CELL_W

c.save()

print(f"Created font_samples.pdf using {len(fonts)} fonts.")
