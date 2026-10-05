from pathlib import Path
from PIL import Image

SIZE = (1080, 1350)
EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}

folder = Path(__file__).parent

for path in folder.iterdir():
    if path.suffix.lower() not in EXTENSIONS:
        continue

    with Image.open(path) as img:
        img = img.convert("RGB")

        # Scale while preserving aspect ratio
        img.thumbnail(SIZE, Image.Resampling.LANCZOS)

        # White 4:5 canvas
        canvas = Image.new("RGB", SIZE, "white")

        # Center image
        x = (SIZE[0] - img.width) // 2
        y = (SIZE[1] - img.height) // 2

        canvas.paste(img, (x, y))

        output = path.with_name(f"{path.stem}_4x5.png")
        canvas.save(output)

        print(f"{path.name} -> {output.name}")
