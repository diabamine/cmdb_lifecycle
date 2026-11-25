"""Smoke tests: the app runs end to end on a tiny catalog and CMDB file, with the real matcher (no fakes)."""
import ast
import io
from pathlib import Path

import pytest

pytest.importorskip("streamlit")
from streamlit.delta_generator import DeltaGenerator
from streamlit.testing.v1 import AppTest

from cmdb.catalog import write_catalog
from cmdb.schema import STANDARD_COLUMNS

APP = str(Path(__file__).resolve().parent.parent / "streamlit_app.py")
CMDB = "vendor,product,version\nPython Software Foundation,Python,3.8.10\nPSF,Python,3.11.2\nAcme,Homegrown Tool,1.0\n"
CUSTOM = "vendor,product,version,eol\nPSF,Python,3.8,2030-01-01\n"


def catalog_row(version, eol):
    row = dict.fromkeys(STANDARD_COLUMNS, "")
    row.update(id=f"eol:python@{version}", product_id="eol:python", source="endoflife.date", vendor="psf",
               product="Python", category="lang", version=version, eol=eol)
    return row


class FakeUpload:
    def __init__(self, name, text):
        self.name = name
        self._data = text.encode()

    def getvalue(self):
        return self._data


@pytest.fixture
def app(monkeypatch, tmp_path):
    (tmp_path / "data").mkdir()
    write_catalog([catalog_row("3.8", "2024-10-07"), catalog_row("3.11", "2027-10-24")], tmp_path / "data" / "master_catalog.csv")
    (tmp_path / "cmdb_extract.csv").write_text(CMDB)
    monkeypatch.chdir(tmp_path)
    return AppTest.from_file(APP, default_timeout=60)


def metrics_of(app):
    return {m.label: (m.value, m.delta) for m in app.metric}


def test_default_matches_sample_against_master(app):
    app.run()
    assert not app.exception
    assert [t.label for t in app.tabs] == ["Results", "Coverage", "Look up"]
    metrics = metrics_of(app)
    assert metrics["Matched"] == ("67%", "2 of 3 rows")                        # the in-house tool stays unmatched
    assert metrics["With an end-of-life date"] == ("67%", "2 of 3 rows")
    assert metrics["Already end of life"][1] == "1 of 3 rows"                  # Python 3.8 ended in 2024


def test_without_any_catalog_the_app_asks_for_one(app, tmp_path):
    (tmp_path / "data" / "master_catalog.csv").unlink()
    app.run()
    assert not app.exception
    assert "Upload a catalog" in app.info[0].value
    assert "ingest" not in app.info[0].value        # the app does not know how catalogs get built


def upload_custom_catalog(monkeypatch, text=CUSTOM, name="mine.csv"):
    original = DeltaGenerator.file_uploader

    def fake_uploader(self, label, *args, **kwargs):
        # AppTest cannot upload files: answer only the catalog uploader
        if "catalog" in label:
            return [FakeUpload(name, text)]
        return original(self, label, *args, **kwargs)

    monkeypatch.setattr(DeltaGenerator, "file_uploader", fake_uploader)


def test_custom_catalog_alone_is_enough(app, tmp_path, monkeypatch):
    (tmp_path / "data" / "master_catalog.csv").unlink()
    upload_custom_catalog(monkeypatch)
    app.run()
    assert not app.exception
    assert metrics_of(app)["Matched"] == ("67%", "2 of 3 rows")


def test_custom_catalog_is_added_next_to_the_default(app, monkeypatch):
    upload_custom_catalog(monkeypatch)
    app.run()
    assert not app.exception
    coverage = next(df.value for df in app.dataframe if "Matched %" in df.value.columns)
    assert list(coverage["Catalog"]) == ["master", "mine"]
    assert list(coverage["Matched %"].round(1)) == [66.7, 66.7]


def test_results_search_and_status_filter(app):
    app.run()
    assert any("3 of 3 rows" in c.value for c in app.caption)
    app.text_input(key="results_text").set_value("homegrown").run()
    assert not app.exception
    assert any("1 of 3 rows" in c.value for c in app.caption)
    app.text_input(key="results_text").set_value("").run()
    app.multiselect(key="results_status").set_value(["End of life"]).run()
    assert any("1 of 3 rows" in c.value for c in app.caption)
    shown = next(df.value for df in app.dataframe if "Matched to" in df.value.columns)
    assert list(shown["Matched to"]) == ["Python"] and list(shown["End of life"]) == ["2024-10-07"]


def test_look_up_one_item_with_the_widget(app):
    app.run()
    assert not app.exception                                            # empty fields show the catalog browser
    app.text_input(key="one_vendor").set_value("PSF")
    app.text_input(key="one_product").set_value("Python")
    app.text_input(key="one_version").set_value("3.11.2").run()
    assert not app.exception
    best = next(df.value for df in app.dataframe if "Catalog" in df.value.columns and "Matched to" in df.value.columns)
    assert list(best["Matched to"]) == ["Python"] and list(best["End of life"]) == ["2027-10-24"]
    cycles = next(df.value for df in app.dataframe if "Cycle" in df.value.columns)
    assert list(cycles["Cycle"]) == ["3.11"]


def test_look_up_reads_the_version_from_the_product_name(app):
    app.run()
    app.text_input(key="one_product").set_value("Python 3.11").run()
    assert not app.exception
    assert any("read from the product name" in c.value for c in app.caption)


def test_catalog_browser_filters_by_text(app):
    app.run()
    app.text_input(key="browse_text").set_value("python").run()
    assert not app.exception
    assert any("1 of 1 products" in c.value for c in app.caption)
    app.text_input(key="browse_text").set_value("zzz").run()
    assert any("0 of 1 products" in c.value for c in app.caption)


def test_app_does_not_depend_on_ingestion_or_catalog_building():
    import subprocess
    import sys
    code = ("import sys\nsys.modules['streamlit'] = None\n"
            "import cmdb.inputs, cmdb.view, cmdb.matching, cmdb.metrics, cmdb.matching.lookup\n"
            "print(sorted(m for m in sys.modules if m.startswith('cmdb.ingest') or m == 'cmdb.catalog'))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=Path(APP).parent)
    assert out.stdout.strip() == "[]", out.stdout + out.stderr
    imported = set()
    for node in ast.walk(ast.parse(Path(APP).read_text())):
        if isinstance(node, ast.ImportFrom):
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
    assert not {m for m in imported if m.startswith("cmdb.ingest") or m == "cmdb.catalog"}, imported
