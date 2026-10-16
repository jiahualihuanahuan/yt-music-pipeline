"""Artist seed lists per region. Import ARTIST_QUERIES from config."""
from config import ARTIST_QUERIES

REGIONS = {
    "cn": "Chinese (mainland + HK/TW)", "kr": "Korean", "jp": "Japanese",
    "eu": "European", "us": "American", "ca": "Canadian",
    "au": "Australian", "la": "Latin",
}

def artists_by_region():
    out = {k: [] for k in REGIONS}
    for name, region, weight in ARTIST_QUERIES:
        out.setdefault(region, []).append((name, weight))
    return out

def top_artists(region, n=20):
    items = artists_by_region().get(region, [])
    return [a for a, _ in sorted(items, key=lambda x: -x[1])[:n]]

if __name__ == "__main__":
    for r, label in REGIONS.items():
        print(f"{label}: {', '.join(top_artists(r, 10))}")
