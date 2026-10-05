import sys
import subprocess
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

words = sys.argv[1:]
if not words:
    print("Usage: python name_fix.py word1 word2 ...")
    sys.exit(1)

fonts = ["DejaVu Sans", "DejaVu Sans Bold"]
sizes = [12, 18, 24, 32, 40, 48]

for i, font in enumerate(fonts):
    path = subprocess.check_output(
        ["fc-match", "-f", "%{file}", font],
        text=True
    ).strip()
    pdfmetrics.registerFont(TTFont(f"font{i}", path))

c = canvas.Canvas("font_samples.pdf", pagesize=(595, 842))
y = 800

for word in words:
    for i, font in enumerate(fonts):
        for size in sizes:
            if y < 60:
                c.showPage()
                y = 800

            c.setFont(f"font{i}", size)
            c.drawString(40, y, word)
            y -= size + 10

c.save()
print("Created font_samples.pdf")
