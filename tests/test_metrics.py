import json

from cmdb.metrics import append_run_log, catalog_coverage, matching_metrics, source_freshness


def test_catalog_coverage():
    catalog = [
        {"product_id": "a:x", "source": "a", "vendor": "V", "eol": "2020-01-01", "release_date": "2019-01-01", "category": "os", "is_eol": ""},
        {"product_id": "a:x", "source": "a", "vendor": "", "eol": "", "release_date": "", "category": "os", "is_eol": ""},
        {"product_id": "b:y", "source": "b", "vendor": "W", "eol": "", "release_date": "", "category": "hardware", "is_eol": "true"},
    ]
    rows = {r["source"]: r for r in catalog_coverage(catalog)}
    assert rows["a"]["cycles"] == 2 and rows["a"]["pct_eol_date"] == 50.0 and rows["a"]["pct_vendor"] == 50.0
    assert rows["b"]["products_flag_only"] == 1 and rows["b"]["pct_hardware"] == 100.0
    assert rows["ALL"]["products"] == 2 and rows["ALL"]["cycles"] == 3


def test_matching_metrics_with_truth():
    results = [
        {"asset_id": "1", "vendor": "A", "source": "s", "matched_product": "Python", "review": "", "matched_cycle": "3.11", "match_method": "exact", "lifecycle_status": "EOL"},
        {"asset_id": "2", "vendor": "B", "source": "s", "matched_product": "Foo", "review": "low", "matched_cycle": "1", "match_method": "fuzzy", "lifecycle_status": "OK"},
        {"asset_id": "3", "vendor": "B", "source": "s", "matched_product": "", "review": "no match", "matched_cycle": "", "match_method": "none", "lifecycle_status": "NO_SOURCE"},
        {"asset_id": "4", "vendor": "C", "source": "s", "matched_product": "Bar", "review": "", "matched_cycle": "", "match_method": "exact", "lifecycle_status": "OK"},
    ]
    truth = [
        {"asset_id": "1", "truth_product": "python", "truth_cycle": "3.11"},
        {"asset_id": "2", "truth_product": "bar", "truth_cycle": "1"},
        {"asset_id": "3", "truth_product": "baz", "truth_cycle": "1"},
        {"asset_id": "4", "truth_product": "", "truth_cycle": ""},
    ]
    m = matching_metrics(results, truth)
    assert m["rows"] == 4 and m["match_rate"] == 75.0 and m["auto_accept_rate"] == 50.0
    assert m["review_queue_rate"] == 25.0 and m["unmatched_rate"] == 25.0
    assert m["by_match_method"]["exact"] == 2 and m["by_source"] == {"s": 4}
    assert m["unmatched_top_vendors"] == [("B", 1)]
    t = m["truth"]
    assert t["known_rows"] == 3 and t["product_recall"] == 33.3 and t["cycle_accuracy"] == 33.3
    assert t["auto_accept_precision"] == 100.0 and t["unknown_false_match"] == 1


def test_matching_metrics_empty():
    m = matching_metrics([])
    assert m["rows"] == 0 and m["match_rate"] == 0.0 and "by_source" not in m


def test_run_log_and_freshness(tmp_path):
    log = tmp_path / "logs" / "runs.jsonl"
    append_run_log(log, {"match_rate": 90.0}, "a")
    append_run_log(log, {"match_rate": 95.0}, "b")
    lines = [json.loads(x) for x in log.read_text().splitlines()]
    assert [x["label"] for x in lines] == ["a", "b"] and "timestamp" in lines[0]

    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"created_utc": "2026-11-13T18:32:09+00:00", "files": {"f.json": {"url": "u"}}}))
    assert source_freshness(manifest)[0]["file"] == "f.json"
    assert source_freshness(tmp_path / "missing.json") == []


def test_lifecycle_metrics_counts_version_and_eol():
    from cmdb.metrics import lifecycle_metrics
    results = [
        {"matched_product": "Python", "matched_cycle": "3.8", "eol_date": "2024-10-07", "lifecycle_status": "EOL", "cycle_match": "exact"},
        {"matched_product": "Python", "matched_cycle": "", "eol_date": "", "lifecycle_status": "VERSION_UNKNOWN", "cycle_match": "no-version"},
        {"matched_product": "", "matched_cycle": "", "eol_date": "", "lifecycle_status": "UNMATCHED", "cycle_match": ""},
    ]
    got = lifecycle_metrics(results)
    assert got["matched_rows"] == 2
    assert got["version_identified_rate"] == 50.0
    assert got["eol_date_rate"] == 50.0
    assert got["past_eol_rows"] == 1
    assert got["by_cycle_match"] == {"exact": 1, "no-version": 1}
