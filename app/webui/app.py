import os
import secrets
import logging
from pathlib import Path

from fastapi import FastAPI, Request, Form, Depends, HTTPException, status
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import config
import graph_client
import signature_engine

log = logging.getLogger(__name__)

app = FastAPI(title="EXO Signature Service")
security = HTTPBasic()

_STATIC_DIR = Path(__file__).parent / "static"
_TEMPLATE_DIR = Path(__file__).parent / "templates"

app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))
templates.env.globals["version"] = config.VERSION

# Counters (in-memory, reset on restart)
_stats = {"processed": 0, "fallback": 0, "errors": 0}


def get_stats() -> dict:
    return _stats


def increment_stat(key: str) -> None:
    _stats[key] = _stats.get(key, 0) + 1


def _check_auth(credentials: HTTPBasicCredentials = Depends(security)):
    correct_user = secrets.compare_digest(credentials.username.encode(), config.WEBUI_USERNAME.encode())
    correct_pass = secrets.compare_digest(credentials.password.encode(), config.WEBUI_PASSWORD.encode())
    if not (correct_user and correct_pass):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


@app.get("/health")
async def health():
    return JSONResponse({"status": "ok", "service": "exo-signature-service"})


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, user: str = Depends(_check_auth)):
    return templates.TemplateResponse(
        request=request, name="dashboard.html",
        context={"stats": get_stats(), "active": "dashboard"},
    )


@app.get("/template", response_class=HTMLResponse)
async def template_editor(request: Request, user: str = Depends(_check_auth)):
    html_path = Path(config.TEMPLATE_DIR) / "signature.html"
    txt_path = Path(config.TEMPLATE_DIR) / "signature.txt"

    html_content = html_path.read_text() if html_path.exists() else ""
    txt_content = txt_path.read_text() if txt_path.exists() else ""

    return templates.TemplateResponse(
        request=request, name="template_editor.html",
        context={
            "html_content": html_content,
            "txt_content": txt_content,
            "active": "template",
            "saved": request.query_params.get("saved"),
        },
    )


@app.post("/template", response_class=HTMLResponse)
async def template_save(
    request: Request,
    html_content: str = Form(""),
    txt_content: str = Form(""),
    user: str = Depends(_check_auth),
):
    html_path = Path(config.TEMPLATE_DIR) / "signature.html"
    txt_path = Path(config.TEMPLATE_DIR) / "signature.txt"

    html_path.write_text(html_content)
    txt_path.write_text(txt_content)

    # Invalidate Jinja2 template cache
    signature_engine._reload_env()

    log.info("Templates saved by user %s", user)
    return RedirectResponse(url="/template?saved=1", status_code=303)


@app.get("/preview", response_class=HTMLResponse)
async def preview(request: Request, email: str = "", user: str = Depends(_check_auth)):
    user_data = graph_client.UserData()
    error = None

    if email:
        try:
            user_data = await graph_client.get_user(email)
        except Exception as exc:
            error = str(exc)

    sig_html, sig_txt = signature_engine.render(user_data)

    return templates.TemplateResponse(
        request=request, name="preview.html",
        context={
            "email": email,
            "sig_html": sig_html,
            "sig_txt": sig_txt,
            "error": error,
            "active": "preview",
        },
    )


@app.get("/config-view", response_class=HTMLResponse)
async def config_view(request: Request, user: str = Depends(_check_auth)):
    cfg = {
        "EXO_SMARTHOST": config.EXO_SMARTHOST,
        "EXO_PORT": config.EXO_PORT,
        "SMTP_PORT": config.SMTP_PORT,
        "SMTP_TLS_CERT": config.SMTP_TLS_CERT,
        "SMTP_TLS_KEY": config.SMTP_TLS_KEY,
        "WEBUI_PORT": config.WEBUI_PORT,
        "WEBUI_USERNAME": config.WEBUI_USERNAME,
        "FALLBACK_ON_ERROR": config.FALLBACK_ON_ERROR,
        "LOG_LEVEL": config.LOG_LEVEL,
        "TEMPLATE_DIR": config.TEMPLATE_DIR,
        "TENANT_ID": config.TENANT_ID[:8] + "…" if config.TENANT_ID else "",
        "CLIENT_ID": config.CLIENT_ID[:8] + "…" if config.CLIENT_ID else "",
    }
    return templates.TemplateResponse(
        request=request, name="config.html",
        context={"cfg": cfg, "active": "config"},
    )
