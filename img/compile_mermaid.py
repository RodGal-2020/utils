from pathlib import Path
import subprocess

folder = Path(__file__).parent
mmd_files = list(folder.glob("*.mmd"))

if len(mmd_files) != 1:
    raise RuntimeError(f"Expected exactly one .mmd file, found {len(mmd_files)}")

mmd_file = mmd_files[0]
output_file = mmd_file.with_suffix(".png")

subprocess.run([
    "mmdc",
    "-i", str(mmd_file),
    "-o", str(output_file)
], check=True)

print(f"Created: {output_file}")
