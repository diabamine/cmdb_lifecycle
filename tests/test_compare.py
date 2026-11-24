from cmdb.compare import compare_references, comparison_summary, load_component_catalog, reference_coverage
from cmdb.schema import STANDARD_COLUMNS


def fake_match(rows, catalog):
    """Match on exact product name; product_id comes from the catalog row."""
    by_name = {c.get("key", c["product"]).lower(): c for c in catalog}
    out = []
    for r in rows:
        c = by_name.get(r["product"].lower())
        out.append({
            "product_id": c["product_id"] if c else "",
            "matched_product": c["product"] if c else "",
            "match_method": "exact" if c else "none",
            "match_confidence": 100 if c else 0,
            "review": "low-confidence product match" if c and c["product"] == "b" else "",
            "lifecycle_status": "EOL" if c else "NO_SOURCE",
            "eol_date": c["eol"] if c else "",
        })
    return out


def ref(pid, product, eol="", key=None):
    """key = the input product name this catalog entry answers to (default: its own name)."""
    return {"product_id": pid, "product": product, "eol": eol, "key": key or product}


def test_load_component_catalog(tmp_path):
    f = tmp_path / "mylist.csv"
    f.write_text("Vendor,Name,Version,Alias,End of Life\nAcme,Widget,1.0,wdg;Widget Pro,2025-01-01\nAcme,Widget,1.0,,\n")
    rows = load_component_catalog(f)
    assert list(rows[0]) == STANDARD_COLUMNS
    assert rows[0]["product_id"] == "user:mylist:Acme:Widget" and rows[0]["source"] == "mylist"
    assert rows[0]["aliases"] == "wdg|Widget Pro" and rows[0]["eol"] == "2025-01-01"
    assert rows[0]["id"] != rows[1]["id"]


def test_compare_and_summary():
    rows = [{"product": "a"}, {"product": "b"}, {"product": "c"}, {"product": "d"}]
    refs = {
        "r1": [ref("1:a", "a", "2020-01-01"), ref("1:b", "b"), ref("1:d", "d")],
        "r2": [ref("1:a", "a", "2021-01-01"), ref("1:b", "b"), ref("2:d2", "d2", key="d")],
    }
    compared = compare_references(rows, refs, match_fn=fake_match)
    assert [r["agree"] for r in compared] == ["all", "all", "none-matched", "conflict"]
    assert compared[0]["r1:eol_date"] == "2020-01-01"

    summary = comparison_summary(compared)
    counts = {s["name"]: s["count"] for s in summary if s["kind"] == "agree"}
    assert counts == {"all": 2, "partial": 0, "conflict": 1, "none-matched": 1}
    assert [s["name"] for s in summary if s["kind"] == "eol_differs"] == ["a"]


def test_partial():
    refs = {"r1": [ref("1:a", "a")], "r2": [ref("2:z", "z")]}
    compared = compare_references([{"product": "a"}], refs, match_fn=fake_match)
    assert compared[0]["agree"] == "partial"


def test_same_product_with_different_ids_agrees():
    # a user catalog's ids never equal the master's, so agreement must not depend on them
    refs = {"master": [ref("eol:a", "A")], "mine": [ref("user:mine::a", "a")]}
    compared = compare_references([{"product": "a"}], refs, match_fn=fake_match)
    assert compared[0]["agree"] == "all"


def test_reference_coverage():
    rows = [{"product": "a"}, {"product": "b"}, {"product": "c"}, {"product": "d"}]
    refs = {"r1": [ref("1:a", "a"), ref("1:b", "b")], "r2": [ref("2:a", "a")]}
    coverage = {c["reference"]: c for c in reference_coverage(compare_references(rows, refs, match_fn=fake_match))}
    assert coverage["r1"]["matched_pct"] == 50.0 and coverage["r1"]["review_pct"] == 25.0
    assert coverage["r1"]["auto_accept_pct"] == 25.0 and coverage["r1"]["unmatched_pct"] == 50.0
    assert coverage["r2"]["matched"] == 1
