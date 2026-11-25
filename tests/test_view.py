"""The UI wording and table shaping (cmdb/view.py): no Streamlit needed."""
from cmdb import view


def result(**kw):
    base = dict(vendor="PSF", product="Python", version="3.8.1", matched_product="Python", matched_source="endoflife.date",
                match_method="exact", match_confidence=98, review="", cycle_match="prefix", matched_cycle="3.8",
                lifecycle_status="EOL", eol_date="2024-10-07", days_to_eol=-415, newer_cycle="", dq_flags="")
    base.update(kw)
    return base


UNMATCHED = result(product="Homegrown Tool", matched_product="", match_method="none", match_confidence=0, lifecycle_status="UNMATCHED",
                   eol_date="", days_to_eol="", review="no match: in-house/unknown product?", matched_cycle="", cycle_match="")


def test_results_table_uses_plain_names_and_keeps_order():
    rows = view.results_table([result(), UNMATCHED])
    assert list(rows[0]) == view.RESULT_COLUMNS
    assert rows[0]["Lifecycle"] == "End of life" and rows[0]["Days left"] == -415 and rows[0]["Confidence"] == 98
    assert rows[1]["Lifecycle"] == "Not matched" and rows[1]["Confidence"] is None and rows[1]["Days left"] is None


def test_filter_by_text_status_and_review():
    results = [result(), result(product="Java", matched_product="Java", lifecycle_status="SUPPORTED"), UNMATCHED]
    assert [r["product"] for r in view.filter_results(results, text="python 3.8")] == ["Python"]
    assert [r["product"] for r in view.filter_results(results, statuses=["Supported"])] == ["Java"]
    assert [r["product"] for r in view.filter_results(results, review_only=True)] == ["Homegrown Tool"]
    assert len(view.filter_results(results)) == 3


def test_status_options_are_worst_first_and_only_those_present():
    results = [result(lifecycle_status="SUPPORTED"), result(lifecycle_status="EOL"), UNMATCHED]
    assert view.status_options(results) == ["End of life", "Supported", "Not matched"]


def test_headline_counts():
    h = view.headline([result(), result(lifecycle_status="SUPPORTED", review="check"), UNMATCHED, UNMATCHED])
    assert h["rows"] == 4 and h["matched"] == (2, 50.0) and h["past_eol"] == (1, 25.0) and h["to_check"] == (1, 25.0)
    assert view.headline([])["matched"] == (0, 0.0)


def test_explain_matched_and_unmatched():
    text = " ".join(view.explain(result()))
    assert "Matched to **Python**" in text and "patch release inside cycle 3.8" in text and "415 days ago" in text
    assert "Lifecycle: **End of life**" in text
    assert view.explain(UNMATCHED)[0] == view.METHOD_TEXT["none"] and "To check" in view.explain(UNMATCHED)[1]
    undated = " ".join(view.explain(result(eol_date="", days_to_eol="", lifecycle_status="UNKNOWN")))
    assert "no end-of-life date" in undated
    assert "read from the product name" in " ".join(view.explain(result(cycle_match="from-name:exact")))


def test_top_unmatched_counts_by_product():
    assert view.top_unmatched([UNMATCHED, UNMATCHED, result()]) == [{"Vendor": "PSF", "Product": "Homegrown Tool", "Rows": 2}]
