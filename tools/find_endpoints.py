import re
import requests
from pathlib import Path

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
ASSET_URL = "https://www.blablalink.com/assets/nikke/version/default/assets/index-VEMOLsjB.js"
CACHE_DIR = Path(__file__).parent.parent / "cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

def fetch_text(url, timeout=20):
    r = requests.get(url, headers={"User-Agent": UA}, timeout=timeout)
    r.raise_for_status()
    r.encoding = r.apparent_encoding or "utf-8"
    return r.text

def save(path: Path, text: str):
    path.write_text(text, encoding="utf-8")

def find_urls(text: str):
    pattern = re.compile(r"https?://[^\s\'\"(){}<>]+")
    return sorted(set(pattern.findall(text)))

def find_patterns(text: str):
    keys = ["openid", "nikke", "nikke-list", "type=", "combat", "union", "alliance", "guild"]
    hits = {}
    for k in keys:
        locs = [m.start() for m in re.finditer(re.escape(k), text)]
        hits[k] = locs[:20]
    return hits

def main():
    print("Fetching asset:", ASSET_URL)
    try:
        js = fetch_text(ASSET_URL)
    except Exception as e:
        print("Fetch failed:", e)
        return

    js_path = CACHE_DIR / "index-VEMOLsjB.js"
    save(js_path, js)
    print("Saved JS to:", js_path.as_posix())

    urls = find_urls(js)
    interesting = [u for u in urls if any(d in u for d in [
        "blablalink.com", "playerinfinite.com", "api.blablalink.com", "sg-", "cdn", "tools"
    ])]

    patt = find_patterns(js)

    out_lines = []
    out_lines.append(f"Total URLs: {len(urls)}")
    out_lines.append(f"Interesting URLs: {len(interesting)}")
    out_lines.append("")
    out_lines.append("Top 60 interesting URLs:")
    for u in interesting[:60]:
        out_lines.append(u)
    out_lines.append("")
    out_lines.append("Pattern locations:")
    for k, v in patt.items():
        out_lines.append(f"{k}: {len(v)} hits; first positions: {v[:5]}")

    out_text = "\n".join(out_lines)
    out_file = CACHE_DIR / "endpoint_candidates.txt"
    save(out_file, out_text)
    print("Written endpoint summary:", out_file.as_posix())
    print()
    print(out_text)

if __name__ == "__main__":
    main()