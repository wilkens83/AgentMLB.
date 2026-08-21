"""Tests for UI-facing helpers and the app module's importability.

The Streamlit runtime is not exercised here (it needs a running server);
instead we test the pure helpers the UI relies on and confirm ``app.py`` is
importable when Streamlit is available.
"""
import os
import py_compile

import pandas as pd
import pytest

import agent

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_dataframe_to_csv_bytes_roundtrip():
    df = pd.DataFrame({"a": [1, 2], "b": ["x", "y"]})
    raw = agent.dataframe_to_csv_bytes(df)
    assert isinstance(raw, bytes)
    text = raw.decode("utf-8")
    assert "a,b" in text
    assert "1,x" in text


def test_dataframe_to_csv_bytes_handles_none():
    # Must not raise on a None/empty frame (empty-result UI path).
    raw = agent.dataframe_to_csv_bytes(None)
    assert isinstance(raw, bytes)


def test_app_source_compiles():
    # Syntax check that does not require Streamlit to be installed.
    py_compile.compile(os.path.join(ROOT, "src", "app.py"), doraise=True)


def test_app_importable_when_streamlit_present():
    pytest.importorskip("streamlit")
    import app  # noqa: F401  - import side effects only

    assert hasattr(app, "render_result")
    assert hasattr(app, "main")
    assert isinstance(app.RECOMMENDED_QUERIES, list) and app.RECOMMENDED_QUERIES
