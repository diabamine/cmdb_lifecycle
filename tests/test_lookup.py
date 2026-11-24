from datetime import date

from cmdb.matching import build_index, lookup
from cmdb.matching.lookup import cycles_for_version, filter_results, split_version
from cmdb.schema import STANDARD_COLUMNS

TODAY = date(2026, 11, 24)


def row(product_id, product, vendor, version, eol, source="endoflife.date", release=""):
    base = dict.fromkeys(STANDARD_COLUMNS, "")
    base.update(id=f"{product_id}@{version}", product_id=product_id, product=product, vendor=vendor, version=version,
                eol=eol, source=source, release_date=release)
    return base


CATALOG = [
    row("eol:python", "Python", "psf", "3.8", "2024-10-07", release="2019-10-14"),
    row("eol:python", "Python", "psf", "3.11", "2027-10-31", release="2022-10-24"),
    row("eol:python", "Python", "psf", "3.12", "2028-10-31", release="2023-10-02"),
    row("eol:python", "Python", "psf", "2.7", "2020-01-01", release="2010-07-03"),
    row("v:microsoft:SQL Server 2019", "SQL Server 2019", "microsoft", "SQL Server 2019", "2030-01-09", source="microsoft"),
    row("v:microsoft:SQL Server 2016", "SQL Server 2016", "microsoft", "SQL Server 2016", "2026-07-14", source="microsoft"),
    row("eol:windows-server", "Windows Server", "microsoft", "2012", "2023-10-10"),
    row("eol:windows-server", "Windows Server", "microsoft", "2012-r2", "2023-10-10"),
]
INDEX = build_index(CATALOG)


def test_split_version_finds_number_in_the_name():
    assert split_version("Python 3.11") == ("python", "3.11")
    assert split_version("Windows Server 2012 R2") == ("windows server", "2012 r2")
    assert split_version("Python") == ("python", "")
    assert split_version("7-Zip") == ("7 zip", "")          # a leading digit is part of the name, not a version


def test_partial_version_lists_every_fitting_cycle():
    releases = INDEX.products["eol:python"]["releases"]
    fits = {r["cycle"]: how for r, how in cycles_for_version(releases, "3")}
    assert fits == {"3.8": "version-prefix", "3.11": "version-prefix", "3.12": "version-prefix"}


def test_patch_version_maps_to_its_cycle():
    releases = INDEX.products["eol:python"]["releases"]
    assert [(r["cycle"], how) for r, how in cycles_for_version(releases, "3.11.4")] == [("3.11", "cycle-prefix")]


def test_no_version_lists_all_cycles_newest_first():
    releases = INDEX.products["eol:python"]["releases"]
    assert [r["cycle"] for r, _ in cycles_for_version(releases, "")] == ["3.12", "3.11", "3.8", "2.7"]


def test_r2_cycle_does_not_also_match_plain_year():
    releases = INDEX.products["eol:windows-server"]["releases"]
    assert [r["cycle"] for r, _ in cycles_for_version(releases, "2012 r2")] == ["2012-r2"]


def test_lookup_detects_version_in_product_name():
    result = lookup(INDEX, vendor="Microsoft", product="SQL Server 2019", today=TODAY)
    assert result["detected_version"] == "2019"
    assert [(r["product"], r["cycle"]) for r in result["rows"]] == [("SQL Server 2019", "SQL Server 2019")]
    assert result["rows"][0]["lifecycle_status"] == "SUPPORTED"


def test_lookup_version_box_wins_over_name():
    result = lookup(INDEX, product="Python 2.7", version="3.12", today=TODAY)
    assert result["version_used"] == "3.12"
    assert [r["cycle"] for r in result["rows"]] == ["3.12"]


def test_lookup_vendor_conflict_is_marked_and_ranked_lower():
    result = lookup(INDEX, vendor="Oracle", product="Python", today=TODAY)
    assert {r["vendor_match"] for r in result["rows"]} == {"conflict"}


def test_unknown_product_returns_nothing():
    assert lookup(INDEX, product="Homegrown Tool", version="1.0", today=TODAY)["rows"] == []


def test_unknown_version_keeps_the_product_visible():
    rows = lookup(INDEX, product="Python", version="9.9", today=TODAY)["rows"]
    assert [(r["cycle"], r["version_fit"]) for r in rows] == [("", "version not in catalog")]


def test_filter_results_by_vendor_product_version():
    results = [
        {"vendor": "PSF", "product": "Python", "version": "3.11.2", "matched_vendor": "psf", "matched_product": "Python", "matched_cycle": "3.11"},
        {"vendor": "PSF", "product": "Python", "version": "3.8.10", "matched_vendor": "psf", "matched_product": "Python", "matched_cycle": "3.8"},
        {"vendor": "", "product": "SQL Server 2019 Standard", "version": "", "matched_vendor": "microsoft", "matched_product": "SQL Server 2019", "matched_cycle": "SQL Server 2019"},
    ]
    assert len(filter_results(results, product="python")) == 2
    assert len(filter_results(results, product="python", version="3")) == 2
    assert [r["version"] for r in filter_results(results, product="python 3.11")] == ["3.11.2"]
    assert len(filter_results(results, product="SQL Server 2019")) == 1       # version found in the input product name
    assert len(filter_results(results, vendor="microsoft")) == 1
    assert filter_results(results, vendor="oracle") == []
    assert len(filter_results([{**results[0], "matched_vendor": "redhat"}], vendor="Red Hat")) == 1
