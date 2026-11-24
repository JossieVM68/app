from __future__ import annotations

import hmac
import io
import json
import logging
import secrets
import threading
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import bcrypt
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_, select
from starlette.middleware.sessions import SessionMiddleware

from . import ai, ats, config, docs, pipeline
from .db import (APP_STATUS_LABELS, APP_STATUSES, Application, Event, Job, Run, SessionLocal, Upload, get_setting,
                 init_db, set_setting)
from .sources import KEY_READY, SOURCES, validate_board
from .textutil import dedupe_key, find_email, parse_date

logging.basicConfig(level=logging.INFO)
BASE = Path(__file__).parent
_stop = threading.Event()


@asynccontextmanager
async def lifespan(app: FastAPI):
    if config.IS_PROD and not config.SECRET_KEY:
        raise RuntimeError("SECRET_KEY es obligatoria en producción")
    init_db()
    t = threading.Thread(target=pipeline.scheduler_loop, args=(_stop,), daemon=True)
    t.start()
    yield
    _stop.set()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(SessionMiddleware, secret_key=config.SECRET_KEY or "x", max_age=14 * 24 * 3600,
                   https_only=config.IS_PROD, same_site="lax", session_cookie="jf_session")
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=BASE / "templates")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "same-origin"
    resp.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data:; script-src 'self'; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; "
        "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
    if config.IS_PROD:
        resp.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    if request.url.path not in ("/static",) and not request.url.path.startswith("/static/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


# ---------------- Sesión, CSRF, login ----------------

class Redirect(Exception):
    def __init__(self, url: str):
        self.url = url


@app.exception_handler(Redirect)
async def _redir(_: Request, exc: Redirect):
    return RedirectResponse(exc.url, status_code=303)


def db_session():
    return SessionLocal()


def csrf_token(request: Request) -> str:
    tok = request.session.get("csrf")
    if not tok:
        tok = secrets.token_urlsafe(32)
        request.session["csrf"] = tok
    return tok


async def form_checked(request: Request):
    form = await request.form()
    sent = form.get("csrf") or request.headers.get("x-csrf", "")
    if not sent or not hmac.compare_digest(str(sent), request.session.get("csrf", "")):
        raise HTTPException(403, "Sesión vencida. Recarga la página.")
    return form


def require_login(request: Request) -> None:
    if not request.session.get("uid"):
        raise Redirect("/login")
    # cerrar sesión si la contraseña cambió
    db = db_session()
    try:
        auth = get_setting(db, "auth")
    finally:
        db.close()
    if request.session.get("pv") != auth.get("version"):
        request.session.clear()
        raise Redirect("/login")


def flash(request: Request, msg: str, kind: str = "ok") -> None:
    request.session.setdefault("flash", []).append([kind, msg])


def render(request: Request, name: str, **ctx) -> HTMLResponse:
    msgs = request.session.pop("flash", [])
    ctx.update(request=request, csrf=csrf_token(request), flashes=msgs, ai_ready=ai.available(),
               labels=APP_STATUS_LABELS, statuses=APP_STATUSES, path=request.url.path)
    return templates.TemplateResponse(request, name, ctx)


_fails: dict[str, list[float]] = defaultdict(list)


def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "?")


def _blocked(ip: str) -> bool:
    now = time.time()
    _fails[ip] = [t for t in _fails[ip] if now - t < 900]
    return len(_fails[ip]) >= 5


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    db = db_session()
    try:
        has_pw = bool(get_setting(db, "auth").get("hash"))
    finally:
        db.close()
    if not has_pw and not _google_ready():
        return RedirectResponse("/setup", 303)
    return render(request, "login.html", google=_google_ready(), has_pw=has_pw)


@app.post("/login")
async def login(request: Request):
    form = await form_checked(request)
    ip = client_ip(request)
    if _blocked(ip):
        flash(request, "Demasiados intentos. Espera 15 minutos.", "error")
        return RedirectResponse("/login", 303)
    db = db_session()
    try:
        auth = get_setting(db, "auth")
    finally:
        db.close()
    pw = str(form.get("password", "")).encode()
    if auth.get("hash") and bcrypt.checkpw(pw, auth["hash"].encode()):
        _fails.pop(ip, None)
        request.session.clear()
        request.session["uid"] = "josefina"
        request.session["pv"] = auth.get("version")
        return RedirectResponse("/", 303)
    _fails[ip].append(time.time())
    time.sleep(1)
    flash(request, "Contraseña incorrecta.", "error")
    return RedirectResponse("/login", 303)


def _google_ready() -> bool:
    return bool(config.GOOGLE_CLIENT_ID and config.GOOGLE_CLIENT_SECRET and config.ALLOWED_EMAILS)


def _callback_url() -> str:
    return config.APP_URL.rstrip("/") + "/auth/google/callback"


@app.get("/auth/google")
def google_start(request: Request):
    if not _google_ready():
        return RedirectResponse("/login", 303)
    state = secrets.token_urlsafe(24)
    request.session["g_state"] = state
    from urllib.parse import urlencode
    q = urlencode({"client_id": config.GOOGLE_CLIENT_ID, "redirect_uri": _callback_url(), "response_type": "code",
                   "scope": "openid email", "state": state, "prompt": "select_account"})
    return RedirectResponse("https://accounts.google.com/o/oauth2/v2/auth?" + q, 303)


@app.get("/auth/google/callback")
def google_callback(request: Request, code: str = "", state: str = ""):
    import httpx
    expected = request.session.pop("g_state", "")
    if not _google_ready() or not code or not expected or not hmac.compare_digest(state, expected):
        flash(request, "No se pudo iniciar sesión con Google. Intenta otra vez.", "error")
        return RedirectResponse("/login", 303)
    try:
        tok = httpx.post("https://oauth2.googleapis.com/token", timeout=20, data={
            "code": code, "client_id": config.GOOGLE_CLIENT_ID, "client_secret": config.GOOGLE_CLIENT_SECRET,
            "redirect_uri": _callback_url(), "grant_type": "authorization_code"}).json()
        info = httpx.get("https://openidconnect.googleapis.com/v1/userinfo", timeout=20,
                         headers={"Authorization": f"Bearer {tok['access_token']}"}).json()
    except Exception:
        flash(request, "No se pudo iniciar sesión con Google.", "error")
        return RedirectResponse("/login", 303)
    email = str(info.get("email", "")).lower()
    if not info.get("email_verified") or email not in config.ALLOWED_EMAILS:
        flash(request, "Esa cuenta de Google no tiene acceso a esta app.", "error")
        return RedirectResponse("/login", 303)
    db = db_session()
    try:
        auth = get_setting(db, "auth")
    finally:
        db.close()
    request.session.clear()
    request.session["uid"] = "josefina"
    request.session["pv"] = auth.get("version")
    return RedirectResponse("/", 303)


@app.post("/logout")
async def logout(request: Request):
    await form_checked(request)
    request.session.clear()
    return RedirectResponse("/login", 303)


@app.get("/setup", response_class=HTMLResponse)
def setup_page(request: Request):
    db = db_session()
    try:
        if get_setting(db, "auth").get("hash"):
            return RedirectResponse("/login", 303)
    finally:
        db.close()
    return render(request, "setup.html", need_code=bool(config.SETUP_CODE) or config.IS_PROD)


@app.post("/setup")
async def setup(request: Request):
    form = await form_checked(request)
    ip = client_ip(request)
    if _blocked(ip):
        flash(request, "Demasiados intentos. Espera 15 minutos.", "error")
        return RedirectResponse("/setup", 303)
    db = db_session()
    try:
        if get_setting(db, "auth").get("hash"):
            return RedirectResponse("/login", 303)
        if config.IS_PROD or config.SETUP_CODE:
            code = str(form.get("code", ""))
            if not config.SETUP_CODE or not hmac.compare_digest(code, config.SETUP_CODE):
                _fails[ip].append(time.time())
                flash(request, "El código de activación no es correcto.", "error")
                return RedirectResponse("/setup", 303)
        p1, p2 = str(form.get("password", "")), str(form.get("password2", ""))
        if len(p1) < 10 or p1 != p2:
            flash(request, "La contraseña debe tener al menos 10 caracteres y coincidir.", "error")
            return RedirectResponse("/setup", 303)
        version = secrets.token_hex(8)
        set_setting(db, "auth", {"hash": bcrypt.hashpw(p1.encode(), bcrypt.gensalt(12)).decode(), "version": version})
    finally:
        db.close()
    request.session.clear()
    request.session["uid"] = "josefina"
    request.session["pv"] = version
    flash(request, "¡Listo! Ahora sube tu currículum para empezar.")
    return RedirectResponse("/perfil", 303)


# ---------------- Panel principal ----------------

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    require_login(request)
    db = db_session()
    try:
        d = pipeline.dashboard_data(db)
        profile = get_setting(db, "profile")
        running = db.scalar(select(Run).where(Run.finished_at.is_(None),
                                              Run.started_at > datetime.utcnow() - timedelta(hours=2)).limit(1))
        return render(request, "dashboard.html", d=d, has_profile=bool(profile), running=bool(running))
    finally:
        db.close()


@app.post("/buscar")
async def buscar(request: Request):
    require_login(request)
    await form_checked(request)
    threading.Thread(target=pipeline.run_search, args=("manual",), daemon=True).start()
    flash(request, "Buscando vacantes nuevas… tarda unos minutos. Puedes seguir usando la app.")
    return RedirectResponse(request.headers.get("referer") or "/", 303)


@app.get("/api/estado-busqueda")
def estado_busqueda(request: Request):
    require_login(request)
    db = db_session()
    try:
        r = db.scalar(select(Run).order_by(Run.id.desc()).limit(1))
        if not r:
            return JSONResponse({"running": False})
        return JSONResponse({"running": r.finished_at is None and r.started_at > datetime.utcnow() - timedelta(hours=2),
                             "new": r.new, "scored": r.scored, "log": r.log or []})
    finally:
        db.close()


# ---------------- Vacantes ----------------

@app.get("/vacantes", response_class=HTMLResponse)
def vacantes(request: Request, q: str = "", min: int = 0, estado: str = "activas", fuente: str = "", page: int = 1):
    require_login(request)
    db = db_session()
    try:
        stmt = select(Job)
        if estado == "activas":
            stmt = stmt.where(Job.status.in_(["nuevo", "guardado"]))
        elif estado in ("nuevo", "guardado", "descartado", "aplicando"):
            stmt = stmt.where(Job.status == estado)
        if q:
            like = f"%{q}%"
            stmt = stmt.where(or_(Job.title.ilike(like), Job.company.ilike(like), Job.location.ilike(like)))
        if min:
            stmt = stmt.where(Job.score >= min)
        if fuente:
            stmt = stmt.where(Job.source == fuente)
        total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
        per = 30
        jobs = list(db.scalars(stmt.order_by(Job.score.desc().nulls_last(), Job.prefilter.desc(), Job.found_at.desc())
                               .offset((page - 1) * per).limit(per)))
        sources = sorted(set(db.scalars(select(Job.source).distinct())))
        return render(request, "jobs.html", jobs=jobs, q=q, min=min, estado=estado, fuente=fuente, page=page,
                      total=total, pages=max(1, (total + per - 1) // per), sources=sources)
    finally:
        db.close()


@app.get("/vacantes/{job_id}", response_class=HTMLResponse)
def vacante(request: Request, job_id: int):
    require_login(request)
    db = db_session()
    try:
        job = db.get(Job, job_id)
        if not job:
            raise HTTPException(404)
        prefs = get_setting(db, "prefs")
        return render(request, "job.html", job=job, prefs=prefs)
    finally:
        db.close()


@app.post("/vacantes/{job_id}/estado")
async def vacante_estado(request: Request, job_id: int):
    require_login(request)
    form = await form_checked(request)
    db = db_session()
    try:
        job = db.get(Job, job_id)
        st = str(form.get("estado"))
        if job and st in ("nuevo", "guardado", "descartado"):
            job.status = st
            db.commit()
            flash(request, {"guardado": "Guardada ⭐", "descartado": "Descartada", "nuevo": "Restaurada"}[st])
    finally:
        db.close()
    return RedirectResponse(str(form.get("next") or f"/vacantes/{job_id}"), 303)


@app.post("/vacantes/{job_id}/descripcion")
async def vacante_desc(request: Request, job_id: int):
    require_login(request)
    form = await form_checked(request)
    db = db_session()
    try:
        job = db.get(Job, job_id)
        text = str(form.get("description", "")).strip()
        if job and text:
            job.description = text[:30000]
            job.contact_email = job.contact_email or find_email(text)
            job.score = None
            db.commit()
            _score_one(request, db, job)
    finally:
        db.close()
    return RedirectResponse(f"/vacantes/{job_id}", 303)


def _score_one(request: Request, db, job: Job) -> None:
    profile = get_setting(db, "profile")
    if not profile:
        flash(request, "Primero sube tu currículum en Perfil.", "error")
        return
    try:
        res = ai.score_jobs(profile, get_setting(db, "prefs"),
                            [dict(id=job.id, title=job.title, company=job.company, location=job.location,
                                  salary=job.salary, description=job.description)])
        if res:
            r = res[0]
            job.score = max(0, min(100, int(r["score"])))
            job.analysis = r
            if r.get("deadline") and not job.deadline:
                job.deadline = parse_date(r["deadline"])
            if r.get("contact_email") and not job.contact_email:
                job.contact_email = r["contact_email"][:200]
            prefs = get_setting(db, "prefs")
            reasons = pipeline.apply_filters(job, prefs, pipeline.home_coords(db, prefs))
            db.commit()
            flash(request, f"Puntaje actualizado: {job.score}"
                  + (f". Ojo: {'; '.join(reasons)}." if reasons else ""), "warn" if reasons else "ok")
    except ai.AIError as e:
        flash(request, str(e), "error")


@app.post("/vacantes/{job_id}/puntuar")
async def vacante_puntuar(request: Request, job_id: int):
    require_login(request)
    await form_checked(request)
    db = db_session()
    try:
        job = db.get(Job, job_id)
        if job:
            _score_one(request, db, job)
    finally:
        db.close()
    return RedirectResponse(f"/vacantes/{job_id}", 303)


@app.post("/vacantes/nueva")
async def vacante_nueva(request: Request):
    require_login(request)
    form = await form_checked(request)
    title = str(form.get("title", "")).strip()
    company = str(form.get("company", "")).strip()
    desc = str(form.get("description", "")).strip()
    if not title or not desc:
        flash(request, "Escribe al menos el puesto y la descripción.", "error")
        return RedirectResponse("/vacantes", 303)
    db = db_session()
    try:
        job = Job(source="manual", external_id=secrets.token_hex(8), dedupe_key=dedupe_key(title, company),
                  title=title[:300], company=company[:200], location=str(form.get("location", ""))[:300],
                  url=str(form.get("url", "")), description=desc[:30000], contact_email=find_email(desc),
                  deadline=parse_date(form.get("deadline")), prefilter=100, status="guardado")
        job.remote = "remote" in job.location.lower() or "remoto" in job.location.lower()
        db.add(job)
        db.commit()
        if ai.available():
            _score_one(request, db, job)
        return RedirectResponse(f"/vacantes/{job.id}", 303)
    finally:
        db.close()


def _pick_lang(prefs: dict, job: Job, requested: str) -> str:
    if requested in ("en", "es"):
        return requested
    if prefs.get("doc_lang") in ("en", "es"):
        return prefs["doc_lang"]
    return (job.analysis or {}).get("posting_language") or "en"


def _generate(db, app_row: Application, hint: str = "") -> None:
    profile = get_setting(db, "profile")
    prefs = get_setting(db, "prefs")
    job = app_row.job
    kws = (job.analysis or {}).get("keywords") or prefs.get("keywords", [])
    pkg = ai.build_package(profile, prefs, dict(title=job.title, company=job.company, location=job.location,
                                                description=job.description), app_row.lang, kws, hint)
    app_row.package = pkg
    app_row.ats = ats.check(pkg["resume"], kws, job.title)
    db.add(Event(application_id=app_row.id, kind="documentos",
                 note=f"CV y carta generados (ATS {app_row.ats['score']}%)"))
    db.commit()


@app.post("/vacantes/{job_id}/preparar")
async def preparar(request: Request, job_id: int):
    require_login(request)
    form = await form_checked(request)
    db = db_session()
    try:
        job = db.get(Job, job_id)
        if not job:
            raise HTTPException(404)
        if not get_setting(db, "profile"):
            flash(request, "Primero sube tu currículum en Perfil.", "error")
            return RedirectResponse("/perfil", 303)
        a = job.application
        if not a:
            a = Application(job_id=job.id, lang=_pick_lang(get_setting(db, "prefs"), job, str(form.get("lang", ""))),
                            contact_email=job.contact_email or "")
            db.add(a)
            job.status = "aplicando"
            db.commit()
            db.refresh(a)
        elif form.get("lang") in ("en", "es"):
            a.lang = str(form.get("lang"))
        try:
            _generate(db, a)
            flash(request, "Tu paquete está listo: CV adaptado, carta y emails.")
        except ai.AIError as e:
            flash(request, str(e), "error")
        return RedirectResponse(f"/aplicaciones/{a.id}", 303)
    finally:
        db.close()


# ---------------- Aplicaciones (CRM) ----------------

@app.get("/aplicaciones", response_class=HTMLResponse)
def aplicaciones(request: Request):
    require_login(request)
    db = db_session()
    try:
        apps = list(db.scalars(select(Application).order_by(Application.created_at.desc())))
        cols = {k: [a for a in apps if a.status == k] for k, _ in APP_STATUSES}
        return render(request, "applications.html", cols=cols, apps=apps, today=pipeline.today_local())
    finally:
        db.close()


def _get_app(db, app_id: int) -> Application:
    a = db.get(Application, app_id)
    if not a:
        raise HTTPException(404)
    return a


@app.get("/aplicaciones/{app_id}", response_class=HTMLResponse)
def aplicacion(request: Request, app_id: int):
    require_login(request)
    db = db_session()
    try:
        a = _get_app(db, app_id)
        p = a.package or {}
        to = a.contact_email or a.job.contact_email or ""
        gmail = ("https://mail.google.com/mail/?view=cm&fs=1&to=" + quote(to) + "&su=" + quote(p.get("email_subject", ""))
                 + "&body=" + quote(p.get("email_body", "")))
        gmail_fu = ("https://mail.google.com/mail/?view=cm&fs=1&to=" + quote(to) + "&su="
                    + quote(p.get("followup_subject", "")) + "&body=" + quote(p.get("followup_body", "")))
        return render(request, "application.html", a=a, job=a.job, p=p, gmail=gmail, gmail_fu=gmail_fu,
                      today=pipeline.today_local())
    finally:
        db.close()


@app.post("/aplicaciones/{app_id}/regenerar")
async def regenerar(request: Request, app_id: int):
    require_login(request)
    form = await form_checked(request)
    db = db_session()
    try:
        a = _get_app(db, app_id)
        if form.get("lang") in ("en", "es"):
            a.lang = str(form.get("lang"))
        hint = str(form.get("hint", ""))[:1000]
        if form.get("missing") and a.ats.get("missing"):
            hint += (" Include these posting keywords ONLY where they truthfully apply to her real experience: "
                     + ", ".join(a.ats["missing"]))
        try:
            _generate(db, a, hint)
            flash(request, f"Documentos actualizados. Puntaje ATS: {a.ats['score']}%")
        except ai.AIError as e:
            flash(request, str(e), "error")
    finally:
        db.close()
    return RedirectResponse(f"/aplicaciones/{app_id}", 303)


@app.post("/aplicaciones/{app_id}/estado")
async def app_estado(request: Request, app_id: int):
    require_login(request)
    form = await form_checked(request)
    st = str(form.get("estado"))
    if st not in APP_STATUS_LABELS:
        raise HTTPException(400)
    db = db_session()
    try:
        a = _get_app(db, app_id)
        today = pipeline.today_local()
        a.status = st
        if st == "aplicada" and not a.applied_at:
            a.applied_at = today
            a.follow_up_at = today + timedelta(days=7)
        if st == "seguimiento":
            a.follow_up_at = today + timedelta(days=10)
        if st in ("rechazada", "retirada", "oferta"):
            a.follow_up_at = None
        db.add(Event(application_id=a.id, kind="estado", note=f"Estado: {APP_STATUS_LABELS[st]}"))
        db.commit()
        flash(request, f"Marcada como: {APP_STATUS_LABELS[st]}")
    finally:
        db.close()
    return RedirectResponse(str(form.get("next") or f"/aplicaciones/{app_id}"), 303)


@app.post("/aplicaciones/{app_id}/datos")
async def app_datos(request: Request, app_id: int):
    require_login(request)
    form = await form_checked(request)
    db = db_session()
    try:
        a = _get_app(db, app_id)
        a.contact_name = str(form.get("contact_name", ""))[:200]
        a.contact_email = str(form.get("contact_email", ""))[:200]
        a.notes = str(form.get("notes", ""))[:10000]
        a.follow_up_at = parse_date(form.get("follow_up_at"))
        a.applied_at = parse_date(form.get("applied_at"))
        dl = parse_date(form.get("deadline"))
        a.job.deadline = dl
        iv = str(form.get("interview_at", "")).strip()
        new_iv = None
        if iv:
            try:
                new_iv = datetime.fromisoformat(iv)
            except ValueError:
                pass
        if new_iv and new_iv != a.interview_at:
            db.add(Event(application_id=a.id, kind="entrevista", note=f"Entrevista: {new_iv:%d/%m/%Y %H:%M}"))
            if a.status in ("preparando", "aplicada", "seguimiento"):
                a.status = "entrevista"
        a.interview_at = new_iv
        db.commit()
        flash(request, "Guardado.")
    finally:
        db.close()
    return RedirectResponse(f"/aplicaciones/{app_id}", 303)


@app.post("/aplicaciones/{app_id}/textos")
async def app_textos(request: Request, app_id: int):
    require_login(request)
    form = await form_checked(request)
    db = db_session()
    try:
        a = _get_app(db, app_id)
        p = dict(a.package or {})
        for k in ("cover_letter", "email_subject", "email_body", "followup_subject", "followup_body", "linkedin_note"):
            if k in form:
                p[k] = str(form.get(k))[:20000]
        a.package = p
        db.commit()
        flash(request, "Textos guardados.")
    finally:
        db.close()
    return RedirectResponse(f"/aplicaciones/{app_id}#textos", 303)


@app.post("/aplicaciones/{app_id}/nota")
async def app_nota(request: Request, app_id: int):
    require_login(request)
    form = await form_checked(request)
    db = db_session()
    try:
        a = _get_app(db, app_id)
        note = str(form.get("note", "")).strip()
        if note:
            db.add(Event(application_id=a.id, kind="nota", note=note[:3000]))
            db.commit()
    finally:
        db.close()
    return RedirectResponse(f"/aplicaciones/{app_id}#historial", 303)


@app.post("/aplicaciones/{app_id}/borrar")
async def app_borrar(request: Request, app_id: int):
    require_login(request)
    await form_checked(request)
    db = db_session()
    try:
        a = _get_app(db, app_id)
        a.job.status = "guardado"
        db.delete(a)
        db.commit()
        flash(request, "Aplicación eliminada. La vacante quedó en Guardadas.")
    finally:
        db.close()
    return RedirectResponse("/aplicaciones", 303)


def _docx_response(data: bytes, name: str) -> Response:
    return Response(data, media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    headers={"Content-Disposition": f'attachment; filename="{name}.docx"'})


@app.get("/aplicaciones/{app_id}/cv.docx")
def app_cv(request: Request, app_id: int):
    require_login(request)
    db = db_session()
    try:
        a = _get_app(db, app_id)
        r = (a.package or {}).get("resume")
        if not r:
            raise HTTPException(404)
        name = docs.safe_filename(f"{r.get('name', 'CV')} Resume {a.job.company}")
        return _docx_response(docs.resume_docx(r, a.lang), name)
    finally:
        db.close()


@app.get("/aplicaciones/{app_id}/carta.docx")
def app_carta(request: Request, app_id: int):
    require_login(request)
    db = db_session()
    try:
        a = _get_app(db, app_id)
        p = a.package or {}
        if not p.get("cover_letter"):
            raise HTTPException(404)
        name = docs.safe_filename(f"{p['resume'].get('name', '')} Cover Letter {a.job.company}")
        return _docx_response(docs.letter_docx(p["cover_letter"], p["resume"]), name)
    finally:
        db.close()


# ---------------- Perfil ----------------

@app.get("/perfil", response_class=HTMLResponse)
def perfil(request: Request):
    require_login(request)
    db = db_session()
    try:
        up = db.scalar(select(Upload).order_by(Upload.id.desc()).limit(1))
        return render(request, "profile.html", profile=get_setting(db, "profile"), prefs=get_setting(db, "prefs"),
                      upload=up)
    finally:
        db.close()


def _extract_text(filename: str, data: bytes) -> tuple[str, bytes | None]:
    fn = filename.lower()
    if fn.endswith(".pdf"):
        from pypdf import PdfReader
        try:
            txt = "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(data)).pages)
        except Exception:
            txt = ""
        return txt, data
    if fn.endswith(".docx"):
        from docx import Document
        d = Document(io.BytesIO(data))
        lines = [p.text for p in d.paragraphs]
        for t in d.tables:
            for row in t.rows:
                lines.append(" | ".join(c.text for c in row.cells))
        return "\n".join(lines), None
    return data.decode("utf-8", errors="ignore"), None


@app.post("/perfil/cv")
async def perfil_cv(request: Request):
    require_login(request)
    form = await form_checked(request)
    f = form.get("file")
    notes = str(form.get("notes", ""))[:8000]
    pasted = str(form.get("pasted", ""))[:40000]
    db = db_session()
    try:
        text, pdf = "", None
        if f is not None and getattr(f, "filename", ""):
            data = await f.read()
            if len(data) > 8 * 1024 * 1024:
                flash(request, "El archivo es muy grande (máx 8 MB).", "error")
                return RedirectResponse("/perfil", 303)
            if not f.filename.lower().endswith((".pdf", ".docx", ".txt")):
                flash(request, "Sube el CV en PDF, Word (.docx) o texto.", "error")
                return RedirectResponse("/perfil", 303)
            db.add(Upload(filename=f.filename[:255], content_type=f.content_type or "", data=data))
            db.commit()
            text, pdf = _extract_text(f.filename, data)
        elif pasted.strip():
            text = pasted
        else:
            up = db.scalar(select(Upload).order_by(Upload.id.desc()).limit(1))
            if up:
                text, pdf = _extract_text(up.filename, up.data)
        if not text.strip() and not pdf:
            flash(request, "Sube tu CV o pega el texto.", "error")
            return RedirectResponse("/perfil", 303)
        try:
            prof = ai.build_profile(text, pdf, notes)
        except ai.AIError as e:
            flash(request, str(e), "error")
            return RedirectResponse("/perfil", 303)
        prof["notes"] = notes
        set_setting(db, "profile", prof)
        prefs = get_setting(db, "prefs")
        about = prof.get("about", {})
        if not prefs.get("target_titles"):
            prefs["target_titles"] = about.get("target_roles", [])[:10]
        if not prefs.get("search_terms"):
            prefs["search_terms"] = about.get("search_terms", [])[:8]
        if not prefs.get("keywords"):
            prefs["keywords"] = about.get("keywords", [])[:40]
        if not prefs.get("home_address") and prof.get("resume", {}).get("contact", {}).get("location"):
            prefs["home_address"] = prof["resume"]["contact"]["location"]
        set_setting(db, "prefs", prefs)
        flash(request, "¡Perfil creado! Revisa que todo esté correcto y ajusta tus preferencias.")
    finally:
        db.close()
    return RedirectResponse("/perfil", 303)


def _num(v, default: float) -> float:
    try:
        return float(str(v or "").replace(",", "").replace("$", "").strip() or default)
    except ValueError:
        return default


def _lines(v) -> list[str]:
    return [x.strip() for x in str(v or "").replace(";", "\n").splitlines() if x.strip()]


@app.post("/perfil/preferencias")
async def perfil_prefs(request: Request):
    require_login(request)
    form = await form_checked(request)
    db = db_session()
    try:
        prefs = get_setting(db, "prefs")
        prefs.update({
            "target_titles": _lines(form.get("target_titles")),
            "search_terms": _lines(form.get("search_terms")),
            "keywords": _lines(form.get("keywords")),
            "exclude_keywords": _lines(form.get("exclude_keywords")),
            "locations": _lines(form.get("locations")),
            "remote_ok": bool(form.get("remote_ok")),
            "hybrid_ok": bool(form.get("hybrid_ok")),
            "onsite_ok": bool(form.get("onsite_ok")),
            "min_hourly": _num(form.get("min_hourly"), 0),
            "max_miles": _num(form.get("max_miles"), 0),
            "home_address": str(form.get("home_address", "")).strip()[:300],
            "strict_unknown": bool(form.get("strict_unknown")),
            "doc_lang": str(form.get("doc_lang", "auto")),
            "extra_context": str(form.get("extra_context", ""))[:4000],
        })
        set_setting(db, "prefs", prefs)
        flash(request, "Preferencias guardadas.")
    finally:
        db.close()
    return RedirectResponse("/perfil#preferencias", 303)


@app.post("/perfil/editar")
async def perfil_editar(request: Request):
    require_login(request)
    form = await form_checked(request)
    db = db_session()
    try:
        prof = get_setting(db, "profile")
        try:
            prof["resume"] = json.loads(str(form.get("resume_json", "")))
        except json.JSONDecodeError:
            flash(request, "El formato del CV no es válido. No se guardó.", "error")
            return RedirectResponse("/perfil#avanzado", 303)
        set_setting(db, "profile", prof)
        flash(request, "CV base actualizado.")
    finally:
        db.close()
    return RedirectResponse("/perfil", 303)


@app.get("/perfil/cv-base.docx")
def perfil_cv_docx(request: Request):
    require_login(request)
    db = db_session()
    try:
        prof = get_setting(db, "profile")
        if not prof.get("resume"):
            raise HTTPException(404)
        return _docx_response(docs.resume_docx(prof["resume"], "en"), docs.safe_filename(prof["resume"].get("name", "CV")))
    finally:
        db.close()


# ---------------- Fuentes ----------------

@app.get("/fuentes", response_class=HTMLResponse)
def fuentes(request: Request):
    require_login(request)
    db = db_session()
    try:
        srcs = get_setting(db, "sources")
        counts = dict(db.execute(select(Job.source, func.count(Job.id)).group_by(Job.source)).all())
        runs = list(db.scalars(select(Run).order_by(Run.id.desc()).limit(10)))
        info = [(k, v[0], v[2], (KEY_READY[k]() if k in KEY_READY else True), counts.get(k if k != "serpapi" else "google", 0))
                for k, v in SOURCES.items()]
        return render(request, "sources.html", srcs=srcs, info=info, runs=runs, sched=get_setting(db, "schedule"))
    finally:
        db.close()


@app.post("/fuentes")
async def fuentes_save(request: Request):
    require_login(request)
    form = await form_checked(request)
    db = db_session()
    try:
        srcs = get_setting(db, "sources")
        srcs["enabled"] = [k for k in SOURCES if form.get(f"en_{k}")]
        for ats_key in ("greenhouse", "lever", "ashby", "smartrecruiters"):
            srcs[ats_key] = list(dict.fromkeys(x.lower() for x in _lines(form.get(ats_key)) if x.replace("-", "").replace("_", "").isalnum()))
        set_setting(db, "sources", srcs)
        sched = get_setting(db, "schedule")
        try:
            sched["every_hours"] = max(12, min(72, int(form.get("every_hours", 24))))
            sched["hour_local"] = max(0, min(23, int(form.get("hour_local", 6))))
            sched["max_ai_per_run"] = max(5, min(150, int(form.get("max_ai_per_run", 40))))
        except ValueError:
            pass
        set_setting(db, "schedule", sched)
        flash(request, "Fuentes guardadas.")
    finally:
        db.close()
    return RedirectResponse("/fuentes", 303)


@app.post("/fuentes/sugerir")
async def fuentes_sugerir(request: Request):
    require_login(request)
    await form_checked(request)
    db = db_session()
    try:
        profile = get_setting(db, "profile")
        if not profile:
            flash(request, "Primero sube tu currículum.", "error")
            return RedirectResponse("/fuentes", 303)
        try:
            sugg = ai.suggest_companies(profile, get_setting(db, "prefs"))
        except ai.AIError as e:
            flash(request, str(e), "error")
            return RedirectResponse("/fuentes", 303)
        srcs = get_setting(db, "sources")
        added = []
        from concurrent.futures import ThreadPoolExecutor
        cands = [(s["ats"].lower().strip(), s["slug"].lower().strip(), s["company"]) for s in sugg
                 if s.get("slug") and s.get("ats", "").lower().strip() in ("greenhouse", "lever", "ashby", "smartrecruiters")]
        cands = [c for c in cands if c[1] not in srcs.get(c[0], [])]
        with ThreadPoolExecutor(8) as ex:
            oks = list(ex.map(lambda c: validate_board(c[0], c[1]), cands))
        for (a, slug, company), ok in zip(cands, oks):
            if ok:
                srcs.setdefault(a, []).append(slug)
                added.append(company)
        set_setting(db, "sources", srcs)
        flash(request, f"Se agregaron {len(added)} empresas verificadas: {', '.join(added[:15])}"
              + ("…" if len(added) > 15 else "") if added else "No se pudo verificar ninguna empresa nueva.")
    finally:
        db.close()
    return RedirectResponse("/fuentes", 303)


# ---------------- Ajustes ----------------

@app.get("/ajustes", response_class=HTMLResponse)
def ajustes(request: Request):
    require_login(request)
    db = db_session()
    try:
        return render(request, "settings.html", goal=get_setting(db, "goal"),
                      digest_ready=bool(config.SMTP_HOST and config.DIGEST_TO))
    finally:
        db.close()


@app.post("/ajustes/meta")
async def ajustes_meta(request: Request):
    require_login(request)
    form = await form_checked(request)
    db = db_session()
    try:
        goal = get_setting(db, "goal")
        try:
            goal["interviews"] = max(1, int(form.get("interviews", 5)))
            goal["daily_applications"] = max(1, int(form.get("daily_applications", 5)))
        except ValueError:
            pass
        d = parse_date(form.get("deadline"))
        goal["deadline"] = d.isoformat() if d else None
        set_setting(db, "goal", goal)
        flash(request, "Meta guardada. ¡Tú puedes!")
    finally:
        db.close()
    return RedirectResponse("/ajustes", 303)


@app.post("/ajustes/password")
async def ajustes_password(request: Request):
    require_login(request)
    form = await form_checked(request)
    db = db_session()
    try:
        auth = get_setting(db, "auth")
        cur = str(form.get("current", "")).encode()
        p1, p2 = str(form.get("password", "")), str(form.get("password2", ""))
        if not bcrypt.checkpw(cur, auth["hash"].encode()):
            flash(request, "La contraseña actual no es correcta.", "error")
        elif len(p1) < 10 or p1 != p2:
            flash(request, "La nueva contraseña debe tener 10+ caracteres y coincidir.", "error")
        else:
            version = secrets.token_hex(8)
            set_setting(db, "auth", {"hash": bcrypt.hashpw(p1.encode(), bcrypt.gensalt(12)).decode(), "version": version})
            request.session["pv"] = version
            flash(request, "Contraseña cambiada. Se cerró la sesión en otros dispositivos.")
    finally:
        db.close()
    return RedirectResponse("/ajustes", 303)


# ---------------- Disparador externo opcional ----------------

@app.post("/cron/run")
def cron_run(request: Request):
    import os
    tok = os.getenv("CRON_TOKEN", "")
    sent = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    if not tok or not hmac.compare_digest(sent, tok):
        raise HTTPException(403)
    threading.Thread(target=pipeline.run_search, args=("auto",), daemon=True).start()
    return {"started": True}
