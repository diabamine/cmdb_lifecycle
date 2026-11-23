"""Security feeds: CISA KEV, EPSS scores, optionally the full NVD base. FETCH ONLY, nothing is standardized here.

The exposure layer (cmdb/exposure.py) reads these raw files directly.
"""
from datetime import date
from pathlib import Path

from cmdb.ingest.common import get, save_download, write_atomic, SNAPSHOT_UA

FEEDS = {
    "cisa_kev.json": "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json",
    "epss_scores.csv.gz": "https://epss.empiricalsecurity.com/epss_scores-current.csv.gz",
}
NVD_MIRROR = "https://github.com/fkie-cad/nvd-json-data-feeds/releases/latest/download"


def fetch(raw_dir: Path) -> None:
    for name, url in FEEDS.items():
        print("fetching", name)
        save_download(raw_dir, name, url)


def fetch_nvd(raw_dir: Path) -> None:
    """Full NVD CVE base from the fkie-cad mirror (per-year .json.xz, ~120 MB, no API key).

    Afterwards run build_nvd_index.py snapshot/nvd snapshot/nvd.sqlite. Not part of fetch(): too big for a default run.
    """
    for year in range(1999, date.today().year + 1):
        content = get(f"{NVD_MIRROR}/CVE-{year}.json.xz", headers={"User-Agent": SNAPSHOT_UA}, tries=5, timeout=120)
        write_atomic(raw_dir / "nvd" / f"CVE-{year}.json.xz", content)
        print("  nvd", year, flush=True)
