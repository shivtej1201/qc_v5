"""QC V6 — site quality platform: coverage crawl, real-visitor review, user journeys, fixes.
Blueprint: docs/blueprint/ARCHITECTURE.md."""
import importlib.util as _ilu
import os as _os
import sys as _sys

__version__ = "6.0.0a1"

# The coverage worker reuses the QC V5 engine (`qcv5` package at the repository root). When qc6 runs from a checkout
# rather than an installed wheel, make that package importable without needing PYTHONPATH.
if _ilu.find_spec("qcv5") is None:
    _root = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    if _os.path.isdir(_os.path.join(_root, "qcv5")):
        _sys.path.insert(0, _root)
