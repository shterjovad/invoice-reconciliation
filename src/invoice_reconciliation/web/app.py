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


def create_app(
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
    draft_notes_with_model: bool = True,
) -> FastAPI:
    """Build and return a FastAPI app wired to the database at ``db_path``.

    ``db_path`` is stored on ``app.state.db_path`` and read fresh inside
    every request (see ``routes.get_db_path``) rather than captured once
    into a long-lived connection — the web view and the headless batch
    command read and mutate the same SQLite file, which another process
    (or the next request) may also be writing to.

    ``draft_notes_with_model`` gates whether a discrepancy note's one lazy
    drafting attempt (``routes._ensure_discrepancy_note``) may call the
    live model, stored on ``app.state.draft_notes_with_model`` and read
    back via ``routes.get_draft_notes_with_model``.

    **Defaults to ``True``.** The exercise brief requires the model to
    draft the discrepancy note, so a model-drafted note is the normal path
    and the calculated note is the degraded one. This differs from
    ``db.ingest``'s default-off ``use_extraction``/``use_cache`` flags,
    and deliberately: those guard a whole batch of image calls, while this
    guards one short text call made once per discrepant invoice and then
    persisted.

    Nothing breaks without credentials. ``notes.draft_discrepancy_note``
    catches a missing credential chain and falls back to the calculated
    note, recording ``drafted_by='calculated'`` so the reviewer sees which
    path produced the text. Drafting is lazy and persistent: the note is
    written once and read back afterwards, so a page refresh never bills
    a second call.

    Pass ``False`` to forbid the call outright — tests do this, so a unit
    run never reaches the network.
    """
    app = FastAPI(title="Invoice Reconciliation Review")
    app.state.db_path = db_path
    app.state.draft_notes_with_model = draft_notes_with_model
    app.state.templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")
    app.include_router(router)

    return app
