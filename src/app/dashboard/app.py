from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.airtable import AirtableClient, AirtableError
from app.config import Settings, get_settings
from app.dashboard.airtable_service import (
    SORTING_MODE_PAGE_ONLY_UNSAFE,
    DashboardAirtableService,
    configured_sorting_mode,
    safe_content_disposition,
)
from app.dashboard.security import DashboardSecurityMiddleware, csrf_input, validate_csrf_token

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
templates.env.globals["csrf_input"] = csrf_input


def create_dashboard_app(
    settings: Settings | None = None,
    airtable: AirtableClient | None = None,
) -> FastAPI:
    resolved_settings = settings or get_settings()
    if len(resolved_settings.dashboard_csrf_secret.encode("utf-8")) < 32:
        raise RuntimeError("DASHBOARD_CSRF_SECRET must be configured and at least 32 bytes")
    sorting_mode = configured_sorting_mode(resolved_settings)
    if sorting_mode == SORTING_MODE_PAGE_ONLY_UNSAFE:
        logger.warning("dashboard global Airtable sorting is not configured sorting_mode=%s", sorting_mode)

    app = FastAPI(title="Voice Inbox Dashboard", version="1.0.0", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(DashboardSecurityMiddleware, settings=resolved_settings)
    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

    service = DashboardAirtableService(
        resolved_settings,
        airtable if airtable is not None else AirtableClient(resolved_settings),
    )

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {"ok": True, "sorting_mode": sorting_mode}

    @app.get("/robots.txt", response_class=PlainTextResponse)
    async def robots() -> str:
        return "User-agent: *\nDisallow: /\n"

    @app.get("/", response_class=HTMLResponse)
    async def overview(request: Request, saved: str = "", error: str = "") -> Response:
        return render(request, "overview.html", {"overview": await to_thread(service.overview), "saved": saved, "error": error})

    @app.get("/records", response_class=HTMLResponse)
    async def records(request: Request) -> Response:
        data = await to_thread(service.list_records, clean_query(request.query_params))
        return render(request, "records.html", {"data": data, "section": "records"})

    @app.get("/needs-review", response_class=HTMLResponse)
    async def needs_review(request: Request, saved: str = "", error: str = "") -> Response:
        query = clean_query(request.query_params)
        query["status"] = "Needs Review"
        data = await to_thread(service.list_records, query)
        return render(request, "records.html", {"data": data, "section": "needs-review", "saved": saved, "error": error})

    @app.get("/queue", response_class=HTMLResponse)
    async def queue(request: Request) -> Response:
        query = clean_query(request.query_params)
        query["queue"] = "1"
        data = await to_thread(service.list_records, query)
        return render(request, "records.html", {"data": data, "section": "queue"})

    @app.get("/processed", response_class=HTMLResponse)
    async def processed(request: Request) -> Response:
        query = clean_query(request.query_params)
        query["status"] = "Processed"
        data = await to_thread(service.list_records, query)
        return render(request, "records.html", {"data": data, "section": "processed"})

    @app.get("/technical", response_class=HTMLResponse)
    async def technical(request: Request) -> Response:
        query = clean_query(request.query_params)
        query["technical"] = "1"
        data = await to_thread(service.list_records, query)
        return render(request, "records.html", {"data": data, "section": "technical"})

    @app.get("/records/{record_id}", response_class=HTMLResponse)
    async def record_detail(request: Request, record_id: str, saved: str = "", error: str = "") -> Response:
        record = await to_thread(service.fetch_record, record_id)
        return render(request, "detail.html", {"record": record, "saved": saved, "error": error})

    @app.post("/records/{record_id}/save")
    async def save_record(request: Request, record_id: str) -> Response:
        form = await request.form(max_fields=30, max_files=0)
        csrf_token = str(form.get("csrf_token") or "")
        if not validate_csrf_token(resolved_settings.dashboard_csrf_secret, csrf_token):
            raise HTTPException(status_code=403, detail="Invalid CSRF token")
        action = str(form.get("action") or "save")
        train = action == "save_train"
        result = await to_thread(service.update_record_from_form, record_id, dict(form), train=train)
        if result.errors:
            record = await to_thread(service.fetch_record, record_id)
            return render(request, "detail.html", {"record": record, "errors": result.errors, "error": "Проверьте поля формы"}, status_code=422)
        suffix = "saved=trained" if train else "saved=1"
        return RedirectResponse(url=f"/records/{record_id}?{suffix}", status_code=303)

    @app.get("/records/{record_id}/attachments/{index}")
    async def attachment(record_id: str, index: int) -> Response:
        filename, content_type, content = await to_thread(service.fetch_attachment, record_id, index)
        return Response(
            content=content,
            media_type=content_type,
            headers={"Content-Disposition": safe_content_disposition(filename)},
        )

    @app.get("/rules", response_class=HTMLResponse)
    async def rules(request: Request, saved: str = "", error: str = "") -> Response:
        data = await to_thread(service.list_rules)
        return render(request, "rules.html", {"data": data, "saved": saved, "error": error})

    @app.post("/rules/{record_id}/active")
    async def set_rule_active(request: Request, record_id: str) -> Response:
        form = await request.form(max_fields=5, max_files=0)
        csrf_token = str(form.get("csrf_token") or "")
        if not validate_csrf_token(resolved_settings.dashboard_csrf_secret, csrf_token):
            raise HTTPException(status_code=403, detail="Invalid CSRF token")
        active = str(form.get("active") or "") == "1"
        await to_thread(service.update_rule_active, record_id, active)
        return RedirectResponse(url="/rules?saved=1", status_code=303)

    @app.exception_handler(AirtableError)
    async def airtable_exception_handler(request: Request, exc: AirtableError) -> Response:
        logger.warning("dashboard airtable error route=%s error_type=%s", request.url.path, type(exc).__name__)
        return render(request, "error.html", {"message": "Airtable временно недоступен или вернул ошибку."}, status_code=502)

    return app


def render(request: Request, template: str, context: dict[str, Any], *, status_code: int = 200) -> Response:
    payload = {"request": request}
    payload.update(context)
    return templates.TemplateResponse(request, template, payload, status_code=status_code)


async def to_thread(func: Any, *args: Any, **kwargs: Any) -> Any:
    return await asyncio.to_thread(func, *args, **kwargs)


def clean_query(query_params: Any) -> dict[str, str]:
    allowed = {"q", "status", "source", "project", "entry_type", "period", "sort", "offset", "page_size", "technical", "queue"}
    return {
        key: str(value)[:500]
        for key, value in query_params.items()
        if key in allowed and str(value).strip()
    }
