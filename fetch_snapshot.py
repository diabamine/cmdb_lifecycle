#!/usr/bin/env python3
"""Run ONCE on a machine WITH internet. Produces a self-contained, dated snapshot directory to carry offline.

  python3 -I fetch_snapshot.py --out snapshot          # endoflife.date + Red Hat lifecycle + CISA KEV + EPSS
  python3 -I fetch_snapshot.py --out snapshot --nvd    # + full NVD CVE base (then: python3 -I build_nvd_index.py snapshot/nvd snapshot/nvd.sqlite)
"""
import argparse, gzip, hashlib, json, os, sys, time, urllib.request, urllib.error
from datetime import date, datetime, timezone
from pathlib import Path

UA = {"User-Agent": "cmdb-lifecycle-snapshot/0.2"}
SOURCES = {
    "endoflife_full.json": "https://endoflife.date/api/v1/products/full",
    "redhat_lifecycle.json": "https://access.redhat.com/product-life-cycles/api/v1/products",
    "cisa_kev.json": "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json",
    "epss_scores.csv.gz": "https://epss.empiricalsecurity.com/epss_scores-current.csv.gz",
}
NVD_MIRROR = "https://github.com/fkie-cad/nvd-json-data-feeds/releases/latest/download"

def get(url, headers=None, tries=5):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={**UA, **(headers or {})})
            with urllib.request.urlopen(req, timeout=120) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            wait = 10 * (i + 1) if e.code in (403, 429, 503) else 3
            print(f"  ! HTTP {e.code} {url[:80]} retry in {wait}s", file=sys.stderr); time.sleep(wait)
        except Exception as e:
            print(f"  ! {e} retry", file=sys.stderr); time.sleep(5)
    raise RuntimeError(f"giving up on {url}")

def fetch_nvd(out: Path):
    """Full NVD CVE base from the fkie-cad mirror (per-year .json.xz, ~120 MB total, no API key, ~1 min)."""
    out.mkdir(exist_ok=True)
    for y in range(1999, date.today().year + 1):
        (out / f"CVE-{y}.json.xz").write_bytes(get(f"{NVD_MIRROR}/CVE-{y}.json.xz")); print("  nvd", y, flush=True)

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--out", default="snapshot"); ap.add_argument("--nvd", action="store_true")
    a = ap.parse_args(); out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    manifest = {"created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"), "files": {}}
    for name, url in SOURCES.items():
        print("fetching", name); b = get(url); (out / name).write_bytes(b)
        manifest["files"][name] = {"url": url, "bytes": len(b), "sha256": hashlib.sha256(b).hexdigest()}
    if a.nvd:
        print("fetching full NVD CVE base"); fetch_nvd(out / "nvd"); manifest["files"]["nvd/"] = {"url": NVD_MIRROR}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1)); print("snapshot ->", out)
if __name__ == "__main__": main()
