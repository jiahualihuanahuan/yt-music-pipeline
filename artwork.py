"""Process cover art: crop to 1:1, add subtle border, save as JPG."""
from pathlib import Path
from PIL import Image, ImageOps

BORDER = 8
BG = (20, 20, 24)


def process_cover(src: Path, dst: Path):
    if not src.exists():
        return False
    try:
        im = Image.open(src).convert("RGB")
        im = ImageOps.fit(im, (600, 600), Image.Resampling.LANCZOS)
        canvas = Image.new("RGB", (600 + 2 * BORDER, 600 + 2 * BORDER), BG)
        canvas.paste(im, (BORDER, BORDER))
        canvas.save(dst, "JPEG", quality=92)
        return True
    except Exception as e:
        print(f"artwork error {src}: {e}")
        return False
