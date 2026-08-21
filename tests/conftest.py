"""Pytest configuration: make the ``src`` package importable as top-level modules.

This mirrors the ``sys.path`` shim used inside the src modules so tests can
``import db``, ``import agent``, ``import ingest`` directly, with no database,
litellm, or pybaseball installed.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
for path in (SRC, ROOT):
    if path not in sys.path:
        sys.path.insert(0, path)
