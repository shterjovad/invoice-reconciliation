"""The reviewer web view: a FastAPI app with server-rendered Jinja2 templates.

No client-side framework and no API-only contract. ``app.py`` holds the
factory; ``routes.py`` holds the six routes from
``technical-considerations.md`` section 2.6; ``templates/`` holds the
Jinja2 templates each route renders.
"""

from __future__ import annotations
