"""Cohesive implementation modules behind the :mod:`api.models` facade.

Cold imports of an individual part initialize the facade first. This keeps the
parts importable for inspection and tooling while retaining one shared namespace
for historical ``api.models`` monkeypatch and reload compatibility.
"""

import importlib
import sys


if "api.models" not in sys.modules:
    importlib.import_module("api.models")
