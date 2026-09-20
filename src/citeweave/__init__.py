"""CiteWeave: time-valid legal citation graph workbench.

All parsing rules live in this package (server side).  The web UI is only a
view over the services exposed by :mod:`citeweave.service` and
:mod:`citeweave.web.server`.
"""

PARSER_VERSION = "citeweave-parser/1.0.0"
SCHEMA_VERSION = "citeweave-schema/1"
__all__ = ["PARSER_VERSION", "SCHEMA_VERSION"]
