from datetime import date

from cmdb.matching import build_index, load_aliases, match_one, match_rows, save_alias, search_catalog, search_scored
from cmdb.schema import MATCH_COLUMNS, STANDARD_COLUMNS

TODAY = date(2026, 6, 1)


def cat_row(product_id, product, version, vendor="", source="manual:test", aliases="", category="", **dates):
    row = {column: "" for column in STANDARD_COLUMNS}
    row.update(id=f"{product_id}@{version}", product_id=product_id, product=product, version=version,
               vendor=vendor, source=source, aliases=aliases, category=category)
    row.update(dates)
    return row


def run(catalog, **query):
    query.setdefault("vendor", "")
    query.setdefault("version", "")
    return match_rows([query], catalog, today=TODAY)[0]


def test_output_has_input_and_match_columns():
    catalog = [cat_row("v:acme:widget", "Widget", "1", vendor="acme")]
    result = run(catalog, product="Widget", vendor="Acme", version="1", asset_id="A1")
    assert result["asset_id"] == "A1"
    for column in MATCH_COLUMNS:
        assert column in result
    assert result["product_id"] == "v:acme:widget"
    assert result["match_method"] == "exact"


def test_alias_beats_exact(tmp_path):
    catalog = [cat_row("p:one", "Widget", "1"), cat_row("p:two", "Gadget", "1")]
    path = tmp_path / "aliases.csv"
    save_alias(path, "", "Widget", "p:two")
    index = build_index(catalog)
    result = match_one({"vendor": "", "product": "Widget", "version": "1"}, index, aliases=load_aliases(path), today=TODAY)
    assert result["product_id"] == "p:two"
    assert result["match_method"] == "alias"


def test_alias_none_means_not_a_catalog_product():
    catalog = [cat_row("p:one", "Widget", "1")]
    aliases = {("", "widget"): "-"}
    result = match_rows([{"vendor": "", "product": "Widget", "version": "1"}], catalog, aliases=aliases, today=TODAY)[0]
    assert result["lifecycle_status"] == "NO_PRODUCT"
    assert result["match_method"] == "alias-none"
    assert result["review"] == ""


def test_vendor_conflict_is_flagged():
    catalog = [cat_row("v:acme:widget", "Widget", "1", vendor="acme")]
    result = run(catalog, product="Widget", vendor="Globex", version="1")
    assert result["product_id"] == "v:acme:widget"
    assert "vendor_conflict(Globex!=acme)" in result["dq_flags"]


def test_vendor_picks_between_same_name_products():
    catalog = [cat_row("p:acme", "Widget", "1", vendor="acme"), cat_row("p:globex", "Widget", "1", vendor="globex")]
    result = run(catalog, product="Widget", vendor="Globex Corp")
    assert result["product_id"] == "p:globex"


def test_rollup_rule_maps_package_to_upstream():
    catalog = [cat_row("p:openssl", "OpenSSL", "3.0", vendor="openssl")]
    result = run(catalog, product="libssl3", vendor="Ubuntu Developers", version="3.0.2-0ubuntu1", fw_or_pkg="libssl3")
    assert result["match_method"] == "exact+rollup"
    assert result["product_id"] == "p:openssl"
    assert result["matched_cycle"] == "3.0"


def test_vendor_rule_and_vendor_conflict_blocks_guessed_spelling():
    catalog = [cat_row("p:firefox", "Firefox", "120", vendor="mozilla")]
    ok = run(catalog, product="Mozilla Firefox (hu)", vendor="Mozilla", version="120.0")
    assert ok["product_id"] == "p:firefox"
    assert ok["match_method"].startswith("exact+")
    other_vendor = [cat_row("p:firefox", "Firefox", "120", vendor="acme")]
    wrong = run(other_vendor, product="Mozilla Firefox (hu)", vendor="Mozilla", version="120.0")
    assert wrong["match_method"] == "none"


def test_cycle_prefix_and_exact():
    catalog = [cat_row("p:py", "Python", "3.11"), cat_row("p:py", "Python", "3.12"), cat_row("p:py", "Python", "3")]
    assert run(catalog, product="Python", version="3.11.4")["cycle_match"] == "prefix"
    assert run(catalog, product="Python", version="v3.12")["cycle_match"] == "exact"
    assert run(catalog, product="Python", version="3.12")["cycle_match"] == "name"
    assert run(catalog, product="Python", version="3.12")["matched_cycle"] == "3.12"


def test_major_only_version_is_flagged_for_review():
    catalog = [cat_row("p:app", "App", "4.1", release_date="2020-01-01"), cat_row("p:app", "App", "4.2", release_date="2021-01-01")]
    result = run(catalog, product="App", version="4")
    assert result["cycle_match"] == "major-ambiguous"
    assert result["matched_cycle"] == "4.2"
    assert result["review"].startswith("product ok, version issue")


def test_fuzzy_match_with_typo():
    catalog = [cat_row("p:pg", "PostgreSQL Database", "16", vendor="postgresql")]
    result = run(catalog, product="PostgreSQL Databse", vendor="PostgreSQL", version="16")
    assert result["match_method"] == "fuzzy"
    assert result["product_id"] == "p:pg"


def test_fuzzy_rejects_digit_mismatch():
    catalog = [cat_row("p:ds", "Cisco Catalyst 9300 Switch", "1")]
    result = run(catalog, product="Cisco Catalyst 9400 Switch", vendor="Cisco")
    assert result["match_method"] == "none"
    assert result["lifecycle_status"] == "UNMATCHED"


def test_unmatched_hardware_is_no_source():
    result = run([cat_row("p:x", "Widget", "1")], product="Zzzz Qqqq", ci_type="hardware")
    assert result["lifecycle_status"] == "NO_SOURCE"


def test_lifecycle_statuses():
    catalog = [
        cat_row("p:a", "Supported", "1", eol="2027-01-01"),
        cat_row("p:a", "Supported", "2", eol="2027-01-01", end_support="2026-01-01"),
        cat_row("p:a", "Supported", "3", eol="2025-01-01"),
        cat_row("p:a", "Supported", "4", eol="2025-01-01", end_extended="2027-01-01"),
        cat_row("p:a", "Supported", "5", is_eol="true"),
        cat_row("p:a", "Supported", "6"),
    ]
    status = {v: run(catalog, product="Supported", version=v)["lifecycle_status"] for v in "123456"}
    assert status == {"1": "SUPPORTED", "2": "MAINTENANCE_ONLY", "3": "EOL", "4": "EXTENDED_SUPPORT_ONLY",
                      "5": "EOL", "6": "UNKNOWN"}


def test_endoflife_cycle_without_eol_date_is_supported():
    catalog = [cat_row("eol:a", "Thing", "1", source="endoflife.date")]
    assert run(catalog, product="Thing", version="1")["lifecycle_status"] == "SUPPORTED"


def test_days_to_eol_and_newer_cycle():
    catalog = [
        cat_row("p:a", "App", "1", release_date="2020-01-01", eol="2026-06-11"),
        cat_row("p:a", "App", "2", release_date="2023-01-01", eol="2030-01-01"),
    ]
    result = run(catalog, product="App", version="1")
    assert result["days_to_eol"] == 10
    assert result["eol_date"] == "2026-06-11"
    assert result["newer_cycle"] == "2"


def test_version_unknown_when_no_cycle_fits():
    catalog = [cat_row("p:a", "App", "1", source="endoflife.date"), cat_row("p:a", "App", "2", source="endoflife.date")]
    result = run(catalog, product="App", version="9.9")
    assert result["lifecycle_status"] == "VERSION_UNKNOWN"
    assert result["review"].startswith("product ok, version issue")


def test_version_taken_from_product_name():
    catalog = [cat_row("v:ms:ws", "Windows Server", "2016", vendor="microsoft", source="microsoft"),
               cat_row("v:ms:ws", "Windows Server", "2019", vendor="microsoft", source="microsoft")]
    result = run(catalog, product="Windows Server 2016", vendor="Microsoft", version="10.0.14393")
    assert result["matched_cycle"] == "2016"
    assert result["cycle_match"].startswith("from-name")


def test_source_mismatch_flag():
    catalog = [
        cat_row("eol:app", "App", "1", source="endoflife.date", eol="2025-01-01"),
        cat_row("v:acme:app", "App", "1", source="acme", vendor="acme", eol="2026-01-01"),
    ]
    result = run(catalog, product="App", vendor="acme", version="1")
    assert result["matched_source"] == "acme"
    assert "source_mismatch_eol(endoflife.date=2025-01-01)" in result["dq_flags"]


def test_index_is_built_once_and_reusable():
    catalog = [cat_row("p:a", "App", "1")]
    index = build_index(catalog)
    rows = [{"vendor": "", "product": "App", "version": "1"}] * 3
    assert [r["product_id"] for r in match_rows(rows, index, today=TODAY)] == ["p:a"] * 3


def test_trace_records_cascade_steps():
    index = build_index([cat_row("p:a", "App", "1")])
    trace = {}
    match_one({"vendor": "", "product": "App", "version": "1"}, index, trace=trace, today=TODAY)
    assert trace["exact"] == ("app", ["p:a"])
    assert "app" in trace["candidates"]


def test_search_catalog_by_name_alias_vendor():
    catalog = [cat_row("p:a", "Apache HTTP Server", "2.4", vendor="apache", aliases="httpd"),
               cat_row("p:b", "Other", "1", vendor="globex")]
    assert {r["product_id"] for r in search_catalog(catalog, "httpd")} == {"p:a"}
    assert {r["product_id"] for r in search_catalog(catalog, "globex")} == {"p:b"}
    assert search_catalog(catalog, "") == []


def test_search_scored_methods_and_confidence():
    index = build_index([cat_row("p:a", "Apache HTTP Server", "2.4", vendor="apache", aliases="httpd"),
                         cat_row("p:b", "Postgresql", "15"), cat_row("p:c", "Postgresql 15 Contrib", "1")])
    alias_hit = search_scored(index, "HTTPD")[0]
    assert (alias_hit["product_id"], alias_hit["method"], alias_hit["confidence"]) == ("p:a", "exact", 98)
    assert alias_hit["verdict"] == "auto-accept"

    by_id = {hit["product_id"]: hit for hit in search_scored(index, "postgresql")}
    assert by_id["p:b"]["method"] == "exact"
    assert by_id["p:c"]["method"] == "contains"
    assert by_id["p:c"]["confidence"] < by_id["p:b"]["confidence"]

    typo = search_scored(index, "postgresq1l")           # one wrong character is a fuzzy hit, not exact
    assert typo and typo[0]["method"] == "fuzzy" and typo[0]["product_id"] == "p:b"
    assert search_scored(index, "") == []
    assert search_scored(index, "zzzzqqqq") == []
