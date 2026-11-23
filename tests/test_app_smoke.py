"""Smoke test: the app renders every tab without errors, with the pipeline functions replaced by trivial fakes."""
import sys
import types
from pathlib import Path

import pytest

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest

APP = str(Path(__file__).resolve().parent.parent / "streamlit_app.py")
CATALOG = [{"id": "py@3.11", "product_id": "eol:python", "source": "endoflife.date", "vendor": "PSF",
            "product": "Python", "aliases": "", "category": "lang", "version": "3.11", "eol": "2027-10-24"}]


def fake_module(monkeypatch, name, **functions):
    module = types.ModuleType(name)
    for key, value in functions.items():
        setattr(module, key, value)
    monkeypatch.setitem(sys.modules, name, module)


@pytest.fixture
def app(monkeypatch, tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "master_catalog.csv").write_text("id\n")
    monkeypatch.chdir(tmp_path)
    match = {"product_id": "eol:python", "matched_product": "Python", "match_confidence": 100, "review": ""}
    fake_module(monkeypatch, "cmdb.catalog", load_catalog=lambda path: CATALOG)
    fake_module(monkeypatch, "cmdb.matching.matcher", build_index=lambda catalog: {},
                match_one=lambda query, index, trace=None: match,
                match_rows=lambda rows, catalog, aliases=None, today=None: [{**r, **match} for r in rows],
                search_catalog=lambda catalog, text, limit=50: CATALOG,
                load_aliases=lambda path: {}, save_alias=lambda *args: None)
    fake_module(monkeypatch, "cmdb.metrics", catalog_coverage=lambda catalog: [{"source": "endoflife.date", "products": 1}],
                matching_metrics=lambda results, truth=None: {})
    fake_module(monkeypatch, "cmdb.compare", load_component_catalog=lambda path: [],
                compare_references=lambda rows, references: [], comparison_summary=lambda compared: [])
    return AppTest.from_file(APP, default_timeout=30)


def test_tabs_render_and_search_works(app):
    app.run()
    assert not app.exception
    assert len(app.tabs) == 4
    app.text_input[0].set_value("python").run()
    assert not app.exception
    assert len(app.dataframe) >= 2  # product table and cycle drill-down


def test_missing_catalog_shows_error(app, tmp_path):
    (tmp_path / "data" / "master_catalog.csv").unlink()
    app.run()
    assert "python -m cmdb.ingest" in app.error[0].value
