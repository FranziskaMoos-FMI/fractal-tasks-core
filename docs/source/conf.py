# -*- coding: utf-8 -*-
from pathlib import Path

from sphinx.ext import apidoc

project = "Fractal Tasks Core"
copyright = (
    "2022, Friedrich Miescher Institute for Biomedical Research and "
    "University of Zurich"
)
version = "0.2.5"
language = "en"

extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.autosummary",
    "sphinx.ext.viewcode",
    "sphinx_rtd_theme",
    "sphinx_autodoc_typehints",
    "autodocsumm",
]

autodoc_default_options = {
    "autosummary": True,
}

autodata_content = "both"

source_suffix = ".rst"
exclude_patterns = []
gettext_compact = False

master_doc = "index"

html_theme = "sphinx_rtd_theme"
html_theme_options = {
    "logo_only": True,
    "navigation_depth": 5,
    "collapse_navigation": True,
}
html_context = {}


package_dir = str(
    Path(__file__).parent.absolute() / "../../fractal_tasks_core"
)


# Extensions to theme docs
def setup(app):

    app.connect(
        "builder-inited",
        lambda _: apidoc.main(
            [
                "-o",
                "source/api_files",
                "-d2",
                "-feMT",
                "--templatedir=apidoc_templates",
                package_dir,
            ]
        ),
    )

    # What follows is taken from https://stackoverflow.com/a/68913808,
    # and used to remove each indented block following a line starting
    # with "Copyright"

    from sphinx.application import Sphinx
    from typing import Any, List

    what = None

    def process(
        app: Sphinx,
        what_: str,
        name: str,
        obj: Any,
        options: Any,
        lines: List[str],
    ) -> None:
        if what and what_ not in what:
            return
        orig_lines = lines[:]

        ignoring = False
        new_lines = []
        for i, line in enumerate(orig_lines):
            if line.startswith("Copyright"):
                # We will start ignoring everything indented after this
                ignoring = True
            else:
                # if the line startswith anything but a space stop
                # ignoring the indented region.
                if ignoring and line and not line.startswith(" "):
                    ignoring = False

            if not ignoring:
                new_lines.append(line)

        lines[:] = new_lines
        # make sure there is a blank line at the end
        if lines and lines[-1]:
            lines.append("")

    app.connect("autodoc-process-docstring", process)
    return app
