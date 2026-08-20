"""FastAPI application: web surfaces plus the Telegram webhook, one process.

Polling is gone. Telegram posts updates to a secret path on this same app, so
there is one deploy, one connection pool, and no machine kept awake purely to
hold a long poll open.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.config import get_settings
from app.web import routes_admin, routes_staff
from app.web.templating import STATIC_DIR

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.bot = None

    if settings.bot_token:
        from app.bot.handlers import build_application

        application = build_application(settings.bot_token)
        await application.initialize()
        await application.start()
        app.state.bot = application
        logger.info("Telegram application started in webhook mode")
    else:
        logger.warning("BOT_TOKEN is not set - the Telegram surface is disabled")

    yield

    if app.state.bot is not None:
        await app.state.bot.stop()
        await app.state.bot.shutdown()


app = FastAPI(title="Time Tracker", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.include_router(routes_staff.router)
app.include_router(routes_admin.router)


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.post("/tg/{secret}")
async def telegram_webhook(
    secret: str,
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
):
    settings = get_settings()
    expected = settings.webhook_secret

    # Both the path and the header must match. The header is what Telegram
    # itself signs the request with; the path keeps the endpoint unguessable
    # in logs and scans.
    if not expected or secret != expected or x_telegram_bot_api_secret_token != expected:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    application = request.app.state.bot
    if application is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)

    from telegram import Update

    update = Update.de_json(await request.json(), application.bot)
    await application.process_update(update)
    return JSONResponse({"ok": True})


@app.exception_handler(HTTPException)
async def friendly_errors(request: Request, exc: HTTPException):
    """Send signed-out browsers to the sign-in page instead of raw JSON."""
    wants_html = "text/html" in (request.headers.get("accept") or "")
    if exc.status_code == status.HTTP_401_UNAUTHORIZED and wants_html:
        return RedirectResponse("/signin", status_code=303)
    if exc.status_code == status.HTTP_403_FORBIDDEN and wants_html:
        return HTMLResponse("<h1>Admins only</h1><p><a href='/'>Back to my clock</a></p>", 403)
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
