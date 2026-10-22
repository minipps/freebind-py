"""Sphinx configuration for the freebind-py documentation."""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

with (ROOT / "pyproject.toml").open("rb") as file:
    _metadata = tomllib.load(file)["project"]

project = _metadata["name"]
author = "minipps"
copyright = f"2026, {author}"

release = _metadata["version"]
version = ".".join(release.split(".")[:2])

extensions = [
    "myst_parser",
    "sphinx.ext.autodoc",
    "sphinx.ext.intersphinx",
    "sphinx.ext.viewcode",
]

myst_enable_extensions = ["colon_fence", "deflist", "fieldlist", "tasklist"]
myst_heading_anchors = 3

autodoc_member_order = "bysource"
autodoc_inherit_docstrings = False

intersphinx_mapping = {"python": ("https://docs.python.org/3", None)}

exclude_patterns = ["_build"]

html_theme = "furo"
html_title = f"{project} {version} documentation"
html_theme_options = {
    "source_repository": "https://github.com/minipps/freebind-py/",
    "source_branch": "main",
    "source_directory": "docs/",
}
