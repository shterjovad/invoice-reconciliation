"""The FastAPI application factory for the reviewer web view.

``create_app()`` builds a new app rather than exposing a module-level
singleton, so a test can construct one against its own scratch database
(by way of ``app.state.db_path``) without import-time side effects or
cross-test state leaking through a shared instance.

The app serves server-rendered Jinja2 templates only. There is no
client-side framework and no JSON API contract: every route in
``routes.py`` returns HTML (``HTMLResponse``) or, for
``/invoices/{id}/image``, the raw image bytes.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from invoice_reconciliation.db.ingest import DEFAULT_DB_PATH
from invoice_reconciliation.web.routes import router

__all__ = ["create_app"]

_WEB_DIR = Path(__file__).resolve().parent
_TEMPLATES_DIR = _WEB_DIR / "templates"
_STATIC_DIR = _WEB_DIR / "static"


def create_app(*, db_path: str | Path = DEFAULT_DB_PATH) -> FastAPI:
    """Build and return a FastAPI app wired to the database at ``db_path``.

    ``db_path`` is stored on ``app.state.db_path`` and read fresh inside
    every request (see ``routes.get_db_path``) rather than captured once
    into a long-lived connection — the web view and the headless batch
    command read and mutate the same SQLite file, which another process
    (or the next request) may also be writing to.
    """
    app = FastAPI(title="Invoice Reconciliation Review")
    app.state.db_path = db_path
    app.state.templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")
    app.include_router(router)

    return app
