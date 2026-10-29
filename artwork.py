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


def fetch_cover_url(title: str, artist: str) -> str | None:
    """Return a high-res cover URL from iTunes (preferred) or Deezer."""
    if not title or not artist:
        return None
    import requests
    q = f"{artist} {title}"
    # iTunes
    try:
        r = requests.get(
            "https://itunes.apple.com/search",
            params={"term": q, "entity": "song", "limit": 1},
            timeout=10,
        )
        if r.ok:
            results = r.json().get("results") or []
            if results:
                url = results[0].get("artworkUrl100") or ""
                if url:
                    return url.replace("100x100", "600x600")
    except Exception:
        pass
    # Deezer fallback
    try:
        r = requests.get(
            "https://api.deezer.com/search",
            params={"q": q, "limit": 1},
            timeout=10,
        )
        if r.ok:
            data = r.json().get("data") or []
            if data:
                alb = data[0].get("album") or {}
                return alb.get("cover_xl") or alb.get("cover_big") or alb.get("cover_medium")
    except Exception:
        pass
    return None


def download_cover(url: str, dst: Path) -> bool:
    """Download cover image to dst."""
    if not url:
        return False
    import requests
    try:
        r = requests.get(url, timeout=15)
        if r.ok and r.content and len(r.content) > 1000:
            dst.write_bytes(r.content)
            return True
    except Exception:
        pass
    return False
