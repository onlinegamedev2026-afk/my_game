"""Luck Game v3 — FastAPI application.

Changes from v2:
- Single active session per account (Redis-backed). A new login from any device
  or tab immediately invalidates the previous session.
- Agent/User lifecycle: deactivation/removal logs console timestamp and lets
  in-game users finish their round before the session expires.
- SESSION_TIMEOUT_HOUR env var (1-10 h or -1 for no timeout) replaces the
  hard-coded 8-hour expiry.
- 401 responses redirect HTML clients to the login page instead of returning a
  bare error page.
"""
import csv
import io
import logging
import secrets
from datetime import datetime
from decimal import Decimal
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, Form, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.exception_handlers import http_exception_handler as _default_http_exception_handler
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from core.config import settings
from core.database import init_db, init_pool, close_pool, get_pool
from core.logging_config import configure_logging
from core.redis_client import init_redis, close_redis, get_redis
from core.security import read_session, sign_session, generate_csrf_token, verify_csrf_token
from services import session_service
from models.schemas import Actor
from realtime.manager import manager
from services.auth_service import AuthService
from services.captcha_service import make_captcha, verify_captcha
from services.game_orchestrator import GameOrchestrator
from services.hierarchy_service import HierarchyService, is_valid_email
from services.otp_service import (
    create_login_otp, verify_login_otp,
    create_child_email_otp, verify_child_email_otp, consume_child_email_otp,
    require_verified_child_email,
    create_admin_pwd_otp, verify_admin_pwd_otp,
    check_otp_send_rate,
)
from services.wallet_service import WalletService
from tasks.celery_app import send_email_job
from utils.identity import generate_account_id, generate_password
from utils.money import money

configure_logging()
log = logging.getLogger(__name__)

app = FastAPI(title="Luck Game v3")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")
templates.env.globals["app_name"] = settings.app_name


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def startup() -> None:
    init_pool()
    init_db()
    init_redis()
    await manager.start_listener()
    log.info("Luck Game v3 started. env=%s scheduler=%s", settings.app_env, settings.game_scheduler_enabled)


@app.on_event("shutdown")
async def shutdown() -> None:
    await manager.stop_listener()
    await close_redis()
    close_pool()
    log.info("Luck Game v3 shut down cleanly.")


# ---------------------------------------------------------------------------
# Database dependency
# ---------------------------------------------------------------------------

def db():
    with get_pool().connection() as conn:
        conn.autocommit = True
        yield conn


# ---------------------------------------------------------------------------
# Auth dependency
# ---------------------------------------------------------------------------

async def current_actor(request: Request, conn=Depends(db)) -> Actor:
    session_token = request.cookies.get("luck_session")
    session = read_session(session_token)
    if not session:
        raise HTTPException(status_code=401)
    user_id, _role, nonce = session

    if not await session_service.is_session_valid(user_id, nonce):
        raise HTTPException(status_code=401)

    actor = AuthService(conn).get_actor(user_id)
    if not actor:
        raise HTTPException(status_code=401)

    if actor.status != "ACTIVE":
        active_game = GameOrchestrator(conn).active_game_for_player(actor)
        if not active_game or not (request.url.path.startswith("/games") or request.url.path == "/api/me"):
            raise HTTPException(status_code=401)

    return actor


# ---------------------------------------------------------------------------
# Exception handlers
# ---------------------------------------------------------------------------

@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    """Redirect unauthenticated HTML requests to the login page."""
    if exc.status_code == 401:
        wants_json = "application/json" in request.headers.get("accept", "")
        if not wants_json:
            return RedirectResponse("/", status_code=303)
    return await _default_http_exception_handler(request, exc)


# ---------------------------------------------------------------------------
# CSRF helpers
# ---------------------------------------------------------------------------

async def _csrf_token_for(request: Request) -> str:
    session_cookie = request.cookies.get("luck_session", "")
    return generate_csrf_token(session_cookie)


def _verify_csrf(request: Request, csrf_token: str) -> None:
    session_cookie = request.cookies.get("luck_session", "")
    if not verify_csrf_token(csrf_token, session_cookie):
        raise HTTPException(status_code=403, detail="CSRF token invalid.")


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def back_to(path: str, *, error: str | None = None, notice: str | None = None) -> RedirectResponse:
    params = {}
    if error:
        params["error"] = error
    if notice:
        params["notice"] = notice
    target = path if not params else f"{path}?{urlencode(params)}"
    return RedirectResponse(target, status_code=303)


def queue_email(to_address: str | None, subject: str, body: str) -> dict:
    if not to_address:
        return {"sent": False, "error": "Missing recipient email address."}
    try:
        send_email_job.apply_async(args=[to_address, subject, body])
        return {"sent": True, "queued": True, "to": to_address}
    except Exception:
        return send_email_job(to_address, subject, body)


# ---------------------------------------------------------------------------
# Health / Ready
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/ready")
async def ready():
    errors: list[str] = []
    try:
        with get_pool().connection() as conn:
            conn.execute("SELECT 1")
    except Exception as exc:
        errors.append(f"db: {exc}")
    try:
        r = get_redis()
        await r.ping()
    except Exception as exc:
        errors.append(f"redis: {exc}")
    if errors:
        return JSONResponse({"status": "not_ready", "errors": errors}, status_code=503)
    return {"status": "ready"}


# ---------------------------------------------------------------------------
# Login / Logout
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index(request: Request, error: str = "", notice: str = "", conflict_token: str = ""):
    captcha = await make_captcha()
    return templates.TemplateResponse(
        "login.html",
        {"request": request, "captcha": captcha, "error": error, "notice": notice, "conflict_token": conflict_token},
    )


@app.post("/login")
async def login(
    request: Request,
    role: str = Form(...),
    username: str = Form(...),
    password: str = Form(...),
    captcha_token: str = Form(...),
    captcha_answer: str = Form(...),
    conn=Depends(db),
):
    if not await verify_captcha(captcha_token, captcha_answer):
        return back_to("/", error="Captcha validation failed.")
    auth = AuthService(conn)
    actor = auth.verify_credentials(username, password, role)
    if not actor:
        if auth.credential_failure_reason(username, password, role) == "inactive":
            return back_to("/", error="Your account is inactive. Please contact your agent.")
        return back_to("/", error="Invalid login details.")

    # Check for an existing active session — redirect to conflict confirmation if found.
    if await session_service.get_active_nonce(actor.id):
        conflict_token = await session_service.create_conflict_token(actor.id, actor.role)
        return RedirectResponse(f"/?conflict_token={conflict_token}", status_code=303)

    if role in {"ADMIN", "AGENT"}:
        if not actor.email:
            return back_to("/", error="This account does not have an email for OTP login.")
        otp_token, code = await create_login_otp(actor.id, actor.role)
        delivery = queue_email(actor.email, "Luck Game login OTP", f"Your Luck Game login OTP is {code}. It expires in 30 minutes.")
        return templates.TemplateResponse(
            "otp.html",
            {
                "request": request,
                "otp_token": otp_token,
                "role": role,
                "username": username,
                "error": "",
                "delivery": delivery,
                "dev_otp": code if settings.show_dev_otp else "",
            },
        )
    nonce = secrets.token_urlsafe(16)
    await session_service.set_active_session(actor.id, nonce)
    token = sign_session(actor.id, actor.role, nonce)
    redirect = RedirectResponse("/games", status_code=303)
    redirect.set_cookie("luck_session", token, httponly=True, samesite="lax", secure=settings.cookie_secure)
    return redirect


@app.post("/login/force")
async def login_force(
    request: Request,
    conflict_token: str = Form(...),
    conn=Depends(db),
):
    """Consume the conflict token, invalidate the old session, and begin a fresh login."""
    payload = await session_service.consume_conflict_token(conflict_token)
    if not payload:
        return back_to("/", error="Session confirmation expired. Please log in again.")

    user_id = payload["user_id"]
    role = payload["role"]

    await session_service.invalidate_session(user_id)
    await manager.kick_user(user_id)

    if role in {"ADMIN", "AGENT"}:
        actor = AuthService(conn).get_actor(user_id)
        if not actor or actor.status != "ACTIVE":
            return back_to("/", error="Account is not active.")
        if not actor.email:
            return back_to("/", error="This account does not have an email for OTP login.")
        otp_token, code = await create_login_otp(actor.id, actor.role)
        delivery = queue_email(actor.email, "Luck Game login OTP", f"Your Luck Game login OTP is {code}. It expires in 30 minutes.")
        return templates.TemplateResponse(
            "otp.html",
            {
                "request": request,
                "otp_token": otp_token,
                "role": role,
                "username": "",
                "error": "",
                "delivery": delivery,
                "dev_otp": code if settings.show_dev_otp else "",
            },
        )

    nonce = secrets.token_urlsafe(16)
    await session_service.set_active_session(user_id, nonce)
    token = sign_session(user_id, role, nonce)
    redirect = RedirectResponse("/games", status_code=303)
    redirect.set_cookie("luck_session", token, httponly=True, samesite="lax", secure=settings.cookie_secure)
    return redirect


@app.post("/login/otp")
async def login_otp(otp_token: str = Form(...), otp_code: str = Form(...), conn=Depends(db)):
    payload = await verify_login_otp(otp_token, otp_code)
    if not payload:
        return back_to("/", error="OTP validation failed.")
    actor = AuthService(conn).get_actor(payload["actor_id"])
    if not actor or actor.role != payload["role"] or actor.status != "ACTIVE":
        return back_to("/", error="Account is not active.")
    nonce = secrets.token_urlsafe(16)
    await session_service.set_active_session(actor.id, nonce)
    session_token = sign_session(actor.id, actor.role, nonce)
    redirect = RedirectResponse("/dashboard", status_code=303)
    redirect.set_cookie("luck_session", session_token, httponly=True, samesite="lax", secure=settings.cookie_secure)
    return redirect


@app.post("/logout")
async def logout(request: Request):
    session_token = request.cookies.get("luck_session")
    session = read_session(session_token)
    if session:
        await session_service.invalidate_session(session[0])
    redirect = RedirectResponse("/", status_code=303)
    redirect.delete_cookie("luck_session")
    return redirect


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    q: str = "",
    role_filter: str = "ALL",
    page: int = 1,
    error: str = "",
    notice: str = "",
    actor: Actor = Depends(current_actor),
    conn=Depends(db),
):
    children, child_total = HierarchyService(conn).list_children_page(actor, q, role_filter, page, 20)
    txs = WalletService(conn).transactions_for_actor(actor)[:20]
    page = max(page, 1)
    csrf = await _csrf_token_for(request)
    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "actor": actor,
            "children": children,
            "transactions": txs,
            "q": q,
            "role_filter": role_filter if role_filter in {"ALL", "USER", "AGENT"} else "ALL",
            "page": page,
            "child_total": child_total,
            "has_prev": page > 1,
            "has_next": page * 20 < child_total,
            "error": error,
            "notice": notice,
            "csrf_token": csrf,
        },
    )


# ---------------------------------------------------------------------------
# Account management
# ---------------------------------------------------------------------------

@app.post("/children")
async def create_child(
    request: Request,
    username: str = Form(...),
    display_name: str = Form(...),
    email: str = Form(""),
    role: str = Form(...),
    password: str = Form(...),
    email_otp_token: str = Form(""),
    csrf_token: str = Form(...),
    actor: Actor = Depends(current_actor),
    conn=Depends(db),
):
    _verify_csrf(request, csrf_token)
    try:
        if role == "AGENT":
            await require_verified_child_email(actor.id, email, email_otp_token)
        HierarchyService(conn).create_child(actor, username, display_name, email, role, password)
        if email_otp_token:
            await consume_child_email_otp(email_otp_token)
        return back_to("/dashboard", notice="Account created successfully.")
    except (PermissionError, ValueError) as exc:
        return back_to("/dashboard", error=str(exc))


@app.post("/children/email-otp/send")
async def send_child_email_otp(
    request: Request,
    email: str = Form(...),
    role: str = Form("AGENT"),
    actor: Actor = Depends(current_actor),
    conn=Depends(db),
):
    email = email.strip()
    if not HierarchyService.can_create(actor, role):
        raise HTTPException(status_code=403)
    if role != "AGENT":
        return JSONResponse({"required": False, "verified": True})
    if not is_valid_email(email):
        raise HTTPException(status_code=400, detail="Enter a valid agent email first.")
    if HierarchyService(conn).email_exists(email):
        raise HTTPException(status_code=400, detail="This email ID is already used by another account.")
    client_ip = request.client.host if request.client else "unknown"
    retry_after = await check_otp_send_rate(f"{actor.id}:{email}:{client_ip}")
    if retry_after:
        raise HTTPException(status_code=429, detail=f"Please wait {retry_after} seconds before requesting another OTP.")
    token, code = await create_child_email_otp(actor.id, email)
    delivery = queue_email(email, "Luck Game agent email verification OTP", f"Your OTP is {code}. It expires in 30 minutes.")
    return JSONResponse({
        "required": True,
        "token": token,
        "delivery": delivery,
        "dev_otp": code if settings.show_dev_otp else "",
    })


@app.post("/children/email-otp/verify")
async def verify_child_email_otp_route(
    email: str = Form(...),
    otp_token: str = Form(...),
    otp_code: str = Form(...),
    actor: Actor = Depends(current_actor),
):
    ok = await verify_child_email_otp(otp_token, email, otp_code, actor.id)
    if not ok:
        raise HTTPException(status_code=400, detail="OTP validation failed.")
    return JSONResponse({"verified": True})


@app.get("/credentials/generate")
async def generate_credentials(
    display_name: str,
    role: str = "AGENT",
    email: str = "",
    email_otp_token: str = "",
    actor: Actor = Depends(current_actor),
    conn=Depends(db),
):
    if actor.role == "USER":
        raise HTTPException(status_code=403)
    if not HierarchyService.can_create(actor, role):
        raise HTTPException(status_code=403)
    if role == "AGENT":
        if not is_valid_email(email):
            raise HTTPException(status_code=400, detail="Enter a valid agent email first.")
        if HierarchyService(conn).email_exists(email):
            raise HTTPException(status_code=400, detail="This email ID is already used by another account.")
        try:
            await require_verified_child_email(actor.id, email, email_otp_token)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    return JSONResponse({"username": generate_account_id(display_name), "password": generate_password()})


@app.post("/children/{child_id}/status")
async def set_status(
    request: Request,
    child_id: str,
    status: str = Form(...),
    csrf_token: str = Form(...),
    actor: Actor = Depends(current_actor),
    conn=Depends(db),
):
    _verify_csrf(request, csrf_token)
    try:
        HierarchyService(conn).set_status(actor, child_id, status)
        ts = datetime.now().strftime("%H:%M:%S")
        if status == "INACTIVE":
            log.info("%s deactivated at %s", child_id, ts)
        else:
            log.info("%s activated at %s", child_id, ts)
        return back_to("/dashboard", notice="Status updated successfully.")
    except (PermissionError, ValueError) as exc:
        return back_to("/dashboard", error=str(exc))


@app.post("/children/status")
async def set_status_from_form(
    request: Request,
    child_id: str = Form(...),
    status: str = Form(...),
    csrf_token: str = Form(...),
    actor: Actor = Depends(current_actor),
    conn=Depends(db),
):
    _verify_csrf(request, csrf_token)
    try:
        HierarchyService(conn).set_status(actor, child_id, status)
        ts = datetime.now().strftime("%H:%M:%S")
        if status == "INACTIVE":
            log.info("%s deactivated at %s", child_id, ts)
        else:
            log.info("%s activated at %s", child_id, ts)
        return back_to("/dashboard", notice="Status updated successfully.")
    except (PermissionError, ValueError) as exc:
        return back_to("/dashboard", error=str(exc))


@app.post("/children/regenerate-password")
async def regenerate_child_password(
    request: Request,
    child_id: str = Form(...),
    csrf_token: str = Form(...),
    actor: Actor = Depends(current_actor),
    conn=Depends(db),
):
    _verify_csrf(request, csrf_token)
    try:
        new_password = HierarchyService(conn).regenerate_child_password(actor, child_id)
        return back_to("/dashboard", notice=f"New password for {child_id}: {new_password}")
    except (PermissionError, ValueError) as exc:
        return back_to("/dashboard", error=str(exc))


@app.post("/children/{child_id}/delete")
async def delete_child(
    request: Request,
    child_id: str,
    csrf_token: str = Form(...),
    actor: Actor = Depends(current_actor),
    conn=Depends(db),
):
    _verify_csrf(request, csrf_token)
    try:
        HierarchyService(conn).delete_child_subtree(actor, child_id)
        ts = datetime.now().strftime("%H:%M:%S")
        log.info("%s removed at %s", child_id, ts)
        return back_to("/dashboard", notice="Account subtree removed successfully.")
    except (PermissionError, ValueError) as exc:
        return back_to("/dashboard", error=str(exc))


@app.post("/children/delete")
async def delete_child_from_form(
    request: Request,
    child_id: str = Form(...),
    csrf_token: str = Form(...),
    actor: Actor = Depends(current_actor),
    conn=Depends(db),
):
    _verify_csrf(request, csrf_token)
    try:
        HierarchyService(conn).delete_child_subtree(actor, child_id)
        ts = datetime.now().strftime("%H:%M:%S")
        log.info("%s removed at %s", child_id, ts)
        return back_to("/dashboard", notice="Account subtree removed successfully.")
    except (PermissionError, ValueError) as exc:
        return back_to("/dashboard", error=str(exc))


# ---------------------------------------------------------------------------
# Password management
# ---------------------------------------------------------------------------

@app.post("/password/update")
async def update_password(
    request: Request,
    old_password: str = Form(...),
    new_password: str = Form(...),
    csrf_token: str = Form(...),
    actor: Actor = Depends(current_actor),
    conn=Depends(db),
):
    _verify_csrf(request, csrf_token)
    if actor.role == "ADMIN":
        return back_to("/dashboard", error="Admin password cannot be changed from this page.")
    redirect = "/games" if actor.role == "USER" else "/dashboard"
    new_password = new_password.strip()
    try:
        HierarchyService(conn).update_password(actor, old_password, new_password)
        return back_to(redirect, notice=f"Password updated successfully. Your new password: {new_password}")
    except ValueError as exc:
        return back_to(redirect, error=str(exc))


# ---------------------------------------------------------------------------
# Wallet
# ---------------------------------------------------------------------------

@app.post("/wallet/admin/adjust")
async def adjust_admin_money(
    request: Request,
    direction: str = Form(...),
    amount: Decimal = Form(...),
    csrf_token: str = Form(...),
    actor: Actor = Depends(current_actor),
    conn=Depends(db),
):
    _verify_csrf(request, csrf_token)
    try:
        WalletService(conn).adjust_admin_balance(actor, amount, direction)
        return back_to("/dashboard", notice="Admin balance updated successfully.")
    except (PermissionError, ValueError) as exc:
        return back_to("/dashboard", error=str(exc))


@app.post("/wallet/{child_id}/add")
async def add_money(
    request: Request,
    child_id: str,
    amount: Decimal = Form(...),
    csrf_token: str = Form(...),
    actor: Actor = Depends(current_actor),
    conn=Depends(db),
):
    _verify_csrf(request, csrf_token)
    try:
        WalletService(conn).add_money(actor, child_id, amount)
        return back_to("/dashboard", notice="Units added successfully.")
    except (PermissionError, ValueError) as exc:
        return back_to("/dashboard", error=str(exc))


@app.post("/wallet/add")
async def add_money_from_form(
    request: Request,
    child_id: str = Form(...),
    amount: Decimal = Form(...),
    csrf_token: str = Form(...),
    actor: Actor = Depends(current_actor),
    conn=Depends(db),
):
    _verify_csrf(request, csrf_token)
    try:
        WalletService(conn).add_money(actor, child_id, amount)
        return back_to("/dashboard", notice="Units added successfully.")
    except (PermissionError, ValueError) as exc:
        return back_to("/dashboard", error=str(exc))


@app.post("/wallet/{child_id}/deduct")
async def deduct_money(
    request: Request,
    child_id: str,
    amount: Decimal = Form(...),
    csrf_token: str = Form(...),
    actor: Actor = Depends(current_actor),
    conn=Depends(db),
):
    _verify_csrf(request, csrf_token)
    try:
        WalletService(conn).deduct_money(actor, child_id, amount)
        return back_to("/dashboard", notice="Units deducted successfully.")
    except (PermissionError, ValueError) as exc:
        return back_to("/dashboard", error=str(exc))


@app.post("/wallet/deduct")
async def deduct_money_from_form(
    request: Request,
    child_id: str = Form(...),
    amount: Decimal = Form(...),
    csrf_token: str = Form(...),
    actor: Actor = Depends(current_actor),
    conn=Depends(db),
):
    _verify_csrf(request, csrf_token)
    try:
        WalletService(conn).deduct_money(actor, child_id, amount)
        return back_to("/dashboard", notice="Units deducted successfully.")
    except (PermissionError, ValueError) as exc:
        return back_to("/dashboard", error=str(exc))


# ---------------------------------------------------------------------------
# Downloads
# ---------------------------------------------------------------------------

@app.get("/download/transactions")
async def download_transactions(actor: Actor = Depends(current_actor), conn=Depends(db)):
    rows = WalletService(conn).transactions_for_actor(actor)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["created_at", "type", "amount", "net_amount", "from_wallet", "to_wallet", "status"])
    for row in rows:
        writer.writerow([row["created_at"], row["transaction_type"], row["amount"], row["net_amount"],
                         row["from_wallet_id"], row["to_wallet_id"], row["status"]])
    output.seek(0)
    return StreamingResponse(iter([output.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=transactions.csv"})


@app.get("/download/children")
async def download_children(actor: Actor = Depends(current_actor), conn=Depends(db)):
    children = HierarchyService(conn).list_children(actor)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["id", "username", "display_name", "role", "status", "balance"])
    for child in children:
        writer.writerow([child.id, child.username, child.display_name, child.role, child.status, f"{child.balance:.3f}"])
    output.seek(0)
    return StreamingResponse(iter([output.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=children.csv"})


# ---------------------------------------------------------------------------
# Game pages
# ---------------------------------------------------------------------------

@app.get("/game", response_class=HTMLResponse)
@app.get("/games", response_class=HTMLResponse)
async def games(
    request: Request,
    error: str = "",
    notice: str = "",
    actor: Actor = Depends(current_actor),
    conn=Depends(db),
):
    active = GameOrchestrator(conn).active_game_for_player(actor)
    csrf = await _csrf_token_for(request)
    return templates.TemplateResponse(
        "games.html",
        {
            "request": request,
            "actor": actor,
            "games": GameOrchestrator.available_games(),
            "active_game": active,
            "error": error,
            "notice": notice,
            "csrf_token": csrf,
        },
    )


@app.get("/games/{game_key}", response_class=HTMLResponse)
async def game_console(
    game_key: str,
    request: Request,
    error: str = "",
    notice: str = "",
    actor: Actor = Depends(current_actor),
    conn=Depends(db),
):
    try:
        orchestrator = GameOrchestrator(conn, game_key)
    except ValueError:
        raise HTTPException(status_code=404)
    active = orchestrator.active_game_for_player(actor)
    if active and active["game_key"] != game_key:
        return back_to("/games", error=f"You already have an active {active['title']} round.")
    if game_key == "andar-bahar":
        template = "andar_bahar.html"
    elif game_key == "color-guessing":
        template = "color_guessing.html"
    else:
        template = "tin_patti.html"
    csrf = await _csrf_token_for(request)
    return templates.TemplateResponse(
        template,
        {
            "request": request,
            "actor": actor,
            "game": {"key": game_key, "title": orchestrator.definition["title"]},
            "error": error,
            "notice": notice,
            "csrf_token": csrf,
        },
    )


# ---------------------------------------------------------------------------
# Game API
# ---------------------------------------------------------------------------

@app.get("/api/me")
async def api_me(actor: Actor = Depends(current_actor), conn=Depends(db)):
    refreshed = AuthService(conn).get_actor(actor.id)
    if not refreshed:
        raise HTTPException(status_code=401)
    active = GameOrchestrator(conn).active_game_for_player(refreshed)
    return JSONResponse({
        "id": refreshed.username,
        "display_name": refreshed.display_name,
        "role": refreshed.role,
        "balance": f"{refreshed.balance:.3f}",
        "active_game": active,
    })


@app.get("/api/games/{game_key}/my-bets")
async def api_game_my_bets(game_key: str, actor: Actor = Depends(current_actor), conn=Depends(db)):
    try:
        orchestrator = GameOrchestrator(conn, game_key)
    except ValueError:
        raise HTTPException(status_code=404)
    return JSONResponse({"bets": orchestrator.player_bets_for_current_cycle(actor)})


@app.post("/games/{game_key}/betting/open")
async def open_betting(game_key: str, actor: Actor = Depends(current_actor)):
    return back_to(f"/games/{game_key}", error="Betting opens automatically every cycle.")


@app.post("/games/{game_key}/bet")
async def bet(
    game_key: str,
    request: Request,
    side: str = Form(...),
    amount: Decimal = Form(...),
    csrf_token: str = Form(""),
    actor: Actor = Depends(current_actor),
    conn=Depends(db),
):
    wants_json = "application/json" in request.headers.get("accept", "")
    if not wants_json:
        _verify_csrf(request, csrf_token)
    if actor.status != "ACTIVE":
        msg = "Your account is inactive. Please contact your agent."
        if wants_json:
            return JSONResponse({"ok": False, "error": msg}, status_code=400)
        return back_to(f"/games/{game_key}", error=msg)
    try:
        orchestrator = GameOrchestrator(conn, game_key)
        await orchestrator.place_bet(actor, side, amount)
        if wants_json:
            return JSONResponse({"ok": True, "message": "Bet placed successfully.", "bets": orchestrator.player_bets_for_current_cycle(actor)})
        return back_to(f"/games/{game_key}", notice="Bet placed successfully.")
    except (PermissionError, ValueError) as exc:
        if wants_json:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        return back_to(f"/games/{game_key}", error=str(exc))


@app.post("/games/{game_key}/start")
async def start_game(game_key: str, actor: Actor = Depends(current_actor)):
    return back_to(f"/games/{game_key}", error="Rounds start automatically after betting closes.")


# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------

@app.websocket("/ws/games/{game_key}")
async def game_ws(game_key: str, websocket: WebSocket):
    with get_pool().connection() as conn:
        conn.autocommit = True
        actor = None
        session_token = websocket.cookies.get("luck_session")
        session = read_session(session_token)
        if session:
            user_id, _role, nonce = session
            if await session_service.is_session_valid(user_id, nonce):
                actor = AuthService(conn).get_actor(user_id)
        await manager.connect(websocket, actor.role if actor else None, actor.id if actor else None)
        include_totals = bool(actor and actor.role == "ADMIN")
        try:
            orchestrator = GameOrchestrator(conn, game_key)
        except ValueError:
            await websocket.close(code=4004)
            return
        try:
            state = await orchestrator.current_state(include_totals)
            await websocket.send_json({"event": "server_state", "data": state})
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            manager.disconnect(websocket)


@app.websocket("/ws/game")
async def legacy_game_ws(websocket: WebSocket):
    await game_ws("tin-patti", websocket)
