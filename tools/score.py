#!/usr/bin/env python3
"""Score the matcher against the synthetic CMDB's ground truth.

  python tools/score.py --catalog data/master_catalog.csv --cmdb data/cmdb_synthetic.csv \
      --truth data/cmdb_truth.csv --today 2026-11-23
"""
import argparse
import csv
import sys
from collections import Counter
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cmdb.matching import match_rows
from cmdb.matching.normalize import norm


def read_csv(path):
    with open(path, encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def is_right_product(result, truth_product):
    """Truth names are endoflife-style slugs ('windows-server'); accept the product name or the id suffix."""
    if norm(result["matched_product"]) == norm(truth_product.replace("-", " ")):
        return True
    return result["product_id"].split(":", 1)[-1] == truth_product


def score(results, truth_rows):
    truth_by_asset = {row["asset_id"]: row for row in truth_rows}
    counts = Counter()
    for result in results:
        truth = truth_by_asset[result["asset_id"]]
        matched = result["matched_product"] != ""
        counts["rows"] += 1
        if not truth["truth_product"]:
            counts["unknown"] += 1
            counts["unknown_false_match"] += matched
            continue
        correct = matched and is_right_product(result, truth["truth_product"])
        auto = matched and result["review"] == ""
        counts["known"] += 1
        counts["known_matched"] += matched
        counts["known_correct"] += correct
        counts["auto"] += auto
        counts["auto_correct"] += auto and correct
        counts["cycle_correct"] += correct and result["matched_cycle"] == truth["truth_cycle"]
        counts["review_q"] += matched and not auto
    return counts


def pct(part, whole):
    return f"{100 * part / max(whole, 1):.1f}%"


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--catalog", default="data/master_catalog.csv")
    parser.add_argument("--cmdb", required=True)
    parser.add_argument("--truth", required=True)
    parser.add_argument("--today", help="YYYY-MM-DD, default today")
    args = parser.parse_args()
    today = date.fromisoformat(args.today) if args.today else date.today()

    results = match_rows(read_csv(args.cmdb), read_csv(args.catalog), today=today)
    counts = score(results, read_csv(args.truth))

    print(f"{len(results)} rows")
    print(f"vs ground truth: known-product rows={counts['known']}")
    print(f"  matched {pct(counts['known_matched'], counts['known'])}"
          f" | correct product {pct(counts['known_correct'], counts['known'])}"
          f" | correct cycle {pct(counts['cycle_correct'], counts['known'])}")
    print(f"  auto-accepted {pct(counts['auto'], counts['known'])} of rows,"
          f" precision of auto-accepted {pct(counts['auto_correct'], counts['auto'])}")
    print(f"  review queue {counts['review_q']} rows"
          f" | unknown/in-house rows falsely matched: {counts['unknown_false_match']}/{counts['unknown']}")


if __name__ == "__main__":
    main()
