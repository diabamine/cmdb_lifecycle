import csv
from pathlib import Path

from cmdb.catalog import build_catalog, clean_date, clean_vendor, load_catalog, write_catalog
from cmdb.schema import STANDARD_COLUMNS

FIXTURES = Path(__file__).parent / "fixtures" / "standard"


def by_key(rows):
    return {(r["product_id"], r["version"]): r for r in rows}


def test_esu_rows_fold_into_end_extended():
    rows = by_key(build_catalog(FIXTURES))
    regular = rows[("v:microsoft:Windows Server 2012", "2012")]
    assert regular["end_extended"] == "2026-10-13"  # latest ESU year
    assert ("v:microsoft:Windows Server 2012", "ESU Year 1") not in rows


def test_product_with_only_esu_rows_is_kept():
    rows = by_key(build_catalog(FIXTURES))
    assert ("v:microsoft:SQL 2008", "ESU Year 1") in rows


def test_dedupe_keeps_row_with_more_fields():
    rows = [r for r in build_catalog(FIXTURES) if r["product_id"] == "eol:python" and r["version"] == "3.11"]
    assert len(rows) == 1
    assert rows[0]["latest"] == "3.11.9"


def test_rows_without_product_are_dropped():
    assert not [r for r in build_catalog(FIXTURES) if r["product_id"] == "eol:nothing"]


def test_dates_are_iso_or_empty():
    assert clean_date("2024-05-01T00:00:00Z") == "2024-05-01"
    assert clean_date("2024/05/01") == "2024-05-01"
    assert clean_date("10/24/2023") == ""
    assert clean_date("2024-13-45") == ""
    row = by_key(build_catalog(FIXTURES))[("eol:python", "3.12")]
    assert row["release_date"] == ""
    assert row["eol"] == "2028-10-31"


def test_names_and_aliases_are_cleaned():
    row = by_key(build_catalog(FIXTURES))[("eol:python", "3.11")]
    assert row["product"] == "Python"
    assert row["aliases"] == "py"  # deduped, no empties, product name itself removed


def test_vendor_canonicalisation():
    assert clean_vendor("Red Hat, Inc.") == "redhat"
    assert clean_vendor("Microsoft Corporation") == "microsoft"
    assert clean_vendor("The Software Foundation") == "the software foundation"


def test_ids_unique_and_non_empty():
    ids = [r["id"] for r in build_catalog(FIXTURES)]
    assert all(ids)
    assert len(ids) == len(set(ids))


def test_output_is_deterministic_and_sorted(tmp_path):
    # Same data with reversed row order must give the same catalog
    other = tmp_path / "standard"
    other.mkdir()
    for path in FIXTURES.glob("*.csv"):
        with open(path, newline="") as handle:
            lines = list(csv.reader(handle))
        with open(other / path.name, "w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(lines[0])
            writer.writerows(reversed(lines[1:]))
    first = build_catalog(FIXTURES)
    second = build_catalog(other)
    assert [r["id"] for r in first] == [r["id"] for r in second]
    keys = [(r["source"], r["product_id"], r["version"]) for r in first]
    assert keys == sorted(keys)


def test_write_and_load_roundtrip(tmp_path):
    rows = build_catalog(FIXTURES)
    out = tmp_path / "master.csv"
    write_catalog(rows, out)
    assert load_catalog(out) == rows
    assert list(rows[0]) == STANDARD_COLUMNS
