"""Step 1: raw files in snapshot/ -> data/standard/<source>.csv.

  python -m cmdb.ingest                      # offline: standardize the committed raw files
  python -m cmdb.ingest --fetch              # fetch every source first (network), then standardize
  python -m cmdb.ingest --fetch --only microsoft ibm
  python -m cmdb.ingest --fetch --only eolnetwork --eol-vendors cisco aruba   # new vendors use free-tier listing slots

A source that fails to fetch keeps its previous raw data; the run continues and exits 1 at the end.
"""
import argparse
import csv
import sys
from pathlib import Path

from cmdb.ingest import (apple, cisco, eolnetwork, endoflife, ibm, juniper, manual, microsoft, nvidia, redhat,
                         security, vmware)
from cmdb.schema import STANDARD_COLUMNS

SOURCES = {
    "endoflife": endoflife, "redhat": redhat, "microsoft": microsoft, "juniper": juniper, "vmware": vmware,
    "ibm": ibm, "apple": apple, "nvidia": nvidia, "cisco": cisco, "eolnetwork": eolnetwork, "manual": manual,
}
# Fetched with --fetch (when no --only is given) but never standardized.
FETCH_ONLY = {"security": security}
# cisco and eolnetwork skip themselves (print a note) when their credentials are missing.


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=STANDARD_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--fetch", action="store_true", help="fetch raw data (network) before standardizing")
    parser.add_argument("--only", nargs="+", choices=[*SOURCES, *FETCH_ONLY], metavar="NAME",
                        help="limit to these sources: " + ", ".join([*SOURCES, *FETCH_ONLY]))
    parser.add_argument("--raw", default="snapshot", type=Path, help="raw data directory (default: snapshot)")
    parser.add_argument("--out", default="data/standard", type=Path, help="output directory (default: data/standard)")
    parser.add_argument("--eol-vendors", nargs="+", metavar="SLUG", help="eol.network vendors to list with --fetch")
    parser.add_argument("--nvd", action="store_true", help="with --fetch: also download the full NVD CVE base (~120 MB)")
    args = parser.parse_args()

    names = args.only or [*SOURCES, *FETCH_ONLY]
    failed = []

    if args.fetch:
        args.raw.mkdir(parents=True, exist_ok=True)
        for name in names:
            module = SOURCES.get(name) or FETCH_ONLY[name]
            try:
                if name == "eolnetwork":
                    module.fetch(args.raw, args.eol_vendors)
                else:
                    module.fetch(args.raw)
                if name == "security" and args.nvd:
                    module.fetch_nvd(args.raw)
            except Exception as err:      # any failure (also a changed page layout): never lose the other sources
                print(f"{name}: FETCH FAILED, keeping existing raw data: {err}", file=sys.stderr)
                failed.append(name)

    for name in names:
        if name not in SOURCES:
            continue
        rows = SOURCES[name].standardize(args.raw)
        write_csv(args.out / f"{name}.csv", rows)
        print(f"{name}: {len(rows)} rows -> {args.out / (name + '.csv')}")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
