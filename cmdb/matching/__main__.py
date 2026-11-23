"""Step 3 from the command line: match a CSV (vendor, product, version, ...) against the master catalog.

  python -m cmdb.matching --cmdb inventory.csv --out matched.csv [--catalog data/master_catalog.csv] [--exposure snapshot]
"""
import argparse
import csv
from datetime import date

from cmdb.catalog import load_catalog
from cmdb.matching.matcher import load_aliases, match_rows


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cmdb", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--catalog", default="data/master_catalog.csv")
    parser.add_argument("--aliases", default="aliases.csv")
    parser.add_argument("--exposure", metavar="SNAPSHOT_DIR", help="also add KEV / EPSS / NVD columns from this snapshot dir")
    args = parser.parse_args()

    with open(args.cmdb, newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    catalog = load_catalog(args.catalog)
    results = match_rows(rows, catalog, aliases=load_aliases(args.aliases), today=date.today())

    if args.exposure:
        # imported here so the plain matching path never needs the security snapshot
        from pathlib import Path
        from cmdb.exposure import add_exposure
        results = add_exposure(results, catalog, Path(args.exposure))

    # exposure adds columns, so take the union in first-seen order
    columns = list(dict.fromkeys(key for row in results for key in row))
    with open(args.out, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(results)
    print(f"{len(results)} rows -> {args.out}")


if __name__ == "__main__":
    main()
