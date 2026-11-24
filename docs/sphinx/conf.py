"""Sphinx configuration for the DynaCamp documentation.

Build:  uv run --group docs sphinx-build -b html docs/sphinx docs/sphinx/_build/html
"""

import sys
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
warnings.filterwarnings("ignore", category=SyntaxWarning)  # pydcs' generated tables

project = "DynaCamp"
author = "DynaCamp contributors"
copyright = "2026, DynaCamp contributors"
release = "0.1.0"

extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.autosummary",
    "sphinx.ext.napoleon",
    "sphinx.ext.viewcode",
    "sphinx.ext.intersphinx",
    "myst_parser",
]

source_suffix = {".rst": "restructuredtext", ".md": "markdown"}
templates_path = ["_templates"]
exclude_patterns = ["_build"]
language = "en"

# -- API reference: every module, class and function, from the docstrings --
autosummary_generate = True
autosummary_imported_members = False
autodoc_default_options = {
    "members": True,
    "private-members": True,
    "show-inheritance": True,
    "member-order": "bysource",
}
autoclass_content = "both"  # class docstring + __init__ docstring
autodoc_typehints = "description"
autodoc_typehints_description_target = "documented_params"
autodoc_preserve_defaults = True
# pydcs (DCS mission generation) is optional: do not fail the docs without it.
autodoc_mock_imports = ["dcs"]

napoleon_google_docstring = True
napoleon_numpy_docstring = False
napoleon_use_ivar = True
napoleon_attr_annotations = True

intersphinx_mapping = {"python": ("https://docs.python.org/3", None)}

# -- Markdown guides (README, architecture, HTTP API, web) --
myst_heading_anchors = 3
suppress_warnings = ["myst.header", "myst.xref_missing"]

# -- HTML --
html_theme = "furo"
html_title = "DynaCamp"
html_static_path = ["_static"]
