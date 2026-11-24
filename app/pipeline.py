"""Búsqueda diaria: junta vacantes de todas las fuentes, filtra, puntúa con Claude y manda el resumen."""
from __future__ import annotations

import logging
import smtplib
import threading
import time
import traceback
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from . import ai, config, geo
from .db import Application, Job, Run, SessionLocal, get_setting
from .sources import KEY_READY, SOURCES
from .textutil import contains_phrase, dedupe_key, find_email, norm, parse_date, tokens

log = logging.getLogger("pipeline")
_lock = threading.Lock()


def prefilter(job: dict, prefs: dict) -> int:
    title_t = tokens(job["title"])
    text = job["title"] + " " + job["description"][:6000]
    all_t = tokens(text)
    nt = norm(job["title"])
    for bad in prefs.get("exclude_keywords", []):
        if bad and norm(bad).strip() and norm(bad).strip() in nt:
            return 0
    best = 0.0
    for t in prefs.get("target_titles", []) + prefs.get("search_terms", []):
        tt = set(tokens(t))
        if tt:
            best = max(best, len(tt & set(title_t)) / len(tt))
    score = 50 * best
    kws = prefs.get("keywords", [])
    hits = sum(1 for k in kws if contains_phrase(all_t, k))
    score += min(30, hits * 4)
    locs = [norm(l).split(",")[0].strip() for l in prefs.get("locations", []) if l]
    jl = norm(job["location"])
    if job["remote"]:
        score += 20 if prefs.get("remote_ok", True) else 0
    elif not locs or any(l and l in jl for l in locs):
        score += 20 if (prefs.get("onsite_ok", True) or prefs.get("hybrid_ok", True)) else 5
    return int(min(100, score))


def home_coords(db, prefs: dict):
    """Ubica la casa una sola vez y guarda las coordenadas."""
    from .db import set_setting
    addr = prefs.get("home_address", "")
    if not addr:
        return None
    hc = prefs.get("home_coords")
    if hc and hc.get("addr") == addr:
        return (hc["lat"], hc["lon"])
    c = geo.geocode(addr)
    if c:
        prefs["home_coords"] = {"addr": addr, "lat": c[0], "lon": c[1]}
        set_setting(db, "prefs", prefs)
    return c


def apply_filters(job: Job, prefs: dict, home) -> list[str]:
    """Reglas firmes de Josefina: distancia, pago y modalidad. Devuelve los motivos para descartar."""
    an = dict(job.analysis or {})
    reasons: list[str] = []
    strict = prefs.get("strict_unknown", False)

    # Modalidad
    mode = an.get("work_mode") or ("remote" if job.remote else "unknown")
    if mode == "remote" and not prefs.get("remote_ok"):
        reasons.append("Es trabajo remoto")
    if mode == "onsite" and not prefs.get("onsite_ok", True):
        reasons.append("Es presencial")

    # Pago por hora
    min_h = float(prefs.get("min_hourly") or 0)
    lo, hi = float(an.get("pay_min_hourly") or 0), float(an.get("pay_max_hourly") or 0)
    if not hi:
        p = geo.parse_pay(job.salary) or geo.parse_pay(job.description[:8000])
        if p:
            lo, hi = p
    if hi:
        an["pay_hourly"] = [round(lo, 2), round(hi, 2)]
        if min_h and hi < min_h:
            reasons.append(f"Paga menos de ${min_h:g}/hora (máx ${hi:.2f})")
    elif strict and min_h:
        reasons.append("No dice cuánto paga")

    # Distancia a casa
    max_mi = float(prefs.get("max_miles") or 0)
    addr = an.get("work_address") or job.location
    if home and max_mi and mode != "remote":
        if geo.is_specific(addr):
            c = geo.geocode(addr)
            if c:
                d = geo.miles(home, c)
                an["distance_miles"] = round(d, 1)
                if d > max_mi:
                    reasons.append(f"Queda a {d:.1f} millas (máx {max_mi:g})")
        elif strict:
            reasons.append("No dice la dirección exacta")

    an["filtro"] = reasons
    job.analysis = an
    if reasons and job.status == "nuevo":
        job.status = "descartado"
    return reasons


def _search_terms(prefs: dict) -> list[str]:
    terms = [t for t in prefs.get("search_terms", []) if t.strip()] or \
            [t for t in prefs.get("target_titles", []) if t.strip()]
    return list(dict.fromkeys(terms))[:8]


def run_search(trigger: str = "auto") -> int | None:
    if not _lock.acquire(blocking=False):
        return None
    try:
        return _run(trigger)
    finally:
        _lock.release()


def _run(trigger: str) -> int:
    db = SessionLocal()
    run = Run(trigger=trigger, log=[])
    db.add(run)
    db.commit()
    logs: list[str] = []
    try:
        prefs = get_setting(db, "prefs")
        srcs = get_setting(db, "sources")
        sched = get_setting(db, "schedule")
        profile = get_setting(db, "profile")
        terms = _search_terms(prefs)
        if not terms:
            logs.append("No hay puestos objetivo configurados. Completa tu perfil primero.")
            run.ok = False
            return run.id

        collected: list[dict] = []
        for key, (name, fn, kind) in SOURCES.items():
            if key not in srcs.get("enabled", []):
                continue
            if kind in ("llave", "ia") and not KEY_READY[key]():
                continue
            center = geo.zip_of(prefs.get("home_address", "")) or (prefs.get("locations") or [""])[0]
            radius_km = max(2, round(float(prefs.get("max_miles") or 25) * 1.609))
            kwargs = {"terms": terms, "locations": prefs.get("locations", []), "center": center,
                      "radius_km": radius_km, "titles": prefs.get("target_titles", []),
                      "min_hourly": float(prefs.get("min_hourly") or 0)}
            if kind == "empresas":
                kwargs["slugs"] = srcs.get(key, [])
                if not kwargs["slugs"]:
                    continue
            t0 = time.time()
            try:
                got = fn(**kwargs)
                collected += got
                logs.append(f"{name}: {len(got)} vacantes ({time.time() - t0:.0f}s)")
            except Exception as e:  # una fuente caída no detiene las demás
                logs.append(f"{name}: error — {type(e).__name__}")
                log.warning("source %s failed: %s", key, traceback.format_exc())
        run.found = len(collected)

        home = home_coords(db, prefs)
        new_ids = []
        seen_keys = set(db.scalars(select(Job.dedupe_key).where(Job.found_at > datetime.utcnow() - timedelta(days=60))))
        for j in collected:
            if not j["title"] or not j["external_id"]:
                continue
            dk = dedupe_key(j["title"], j["company"])
            if dk in seen_keys:
                continue
            exists = db.scalar(select(Job.id).where(Job.source == j["source"], Job.external_id == j["external_id"]))
            if exists:
                continue
            seen_keys.add(dk)
            pf = prefilter(j, prefs)
            job = Job(source=j["source"], external_id=j["external_id"], dedupe_key=dk, title=j["title"],
                      company=j["company"], location=j["location"], remote=j["remote"], url=j["url"] or "",
                      description=j["description"], salary=j["salary"], posted_at=j["posted_at"],
                      deadline=j["deadline"], contact_email=find_email(j["description"]), prefilter=pf,
                      status="nuevo" if pf > 0 else "descartado")
            if job.status == "nuevo":
                apply_filters(job, prefs, None)  # pago y remoto; la distancia se calcula al analizar
            db.add(job)
            db.flush()
            new_ids.append(job.id)
        db.commit()
        run.new = len(new_ids)
        logs.append(f"Nuevas: {len(new_ids)} de {len(collected)} encontradas")

        if ai.available() and profile:
            run.scored = score_pending(db, profile, prefs, sched)
            logs.append(f"Puntuadas con IA: {run.scored}")
        elif not ai.available():
            logs.append("Sin llave de Claude: se usa solo el puntaje básico.")
        run.ok = True
    except Exception as e:
        logs.append(f"Error general: {type(e).__name__}: {e}")
        log.error(traceback.format_exc())
    finally:
        run.log = logs
        run.finished_at = datetime.utcnow()
        db.commit()
        try:
            send_digest(db)
        except Exception:
            log.warning("digest failed: %s", traceback.format_exc())
        db.close()
    return run.id


def score_pending(db, profile: dict, prefs: dict, sched: dict, limit: int | None = None) -> int:
    limit = limit or int(sched.get("max_ai_per_run", 40))
    min_pf = int(sched.get("min_prefilter", 30))
    jobs = list(db.scalars(select(Job).where(Job.score.is_(None), Job.status == "nuevo", Job.prefilter >= min_pf)
                           .order_by(Job.prefilter.desc(), Job.found_at.desc()).limit(limit)))
    done = 0
    home = home_coords(db, prefs)
    for i in range(0, len(jobs), 6):
        batch = jobs[i:i + 6]
        try:
            res = ai.score_jobs(profile, prefs, [dict(id=j.id, title=j.title, company=j.company, location=j.location,
                                                       salary=j.salary, description=j.description) for j in batch])
        except ai.AIError as e:
            log.warning("scoring failed: %s", e)
            break
        by_id = {r["id"]: r for r in res}
        for j in batch:
            r = by_id.get(j.id)
            if not r:
                continue
            j.score = max(0, min(100, int(r["score"])))
            j.analysis = r
            if r.get("deadline") and not j.deadline:
                j.deadline = parse_date(r["deadline"])
            if r.get("contact_email") and not j.contact_email:
                j.contact_email = r["contact_email"][:200]
            apply_filters(j, prefs, home)
            done += 1
        db.commit()
    return done


# ---------------- Resumen diario por correo ----------------

def today_local() -> date:
    return datetime.now(ZoneInfo(config.APP_TZ)).date()


def dashboard_data(db) -> dict:
    today = today_local()
    goal = get_setting(db, "goal")
    apps = list(db.scalars(select(Application)))
    interviews = sum(1 for a in apps if a.status in ("entrevista", "oferta"))
    applied = sum(1 for a in apps if a.applied_at)
    applied_today = sum(1 for a in apps if a.applied_at == today)
    followups = [a for a in apps if a.follow_up_at and a.follow_up_at <= today and a.status in ("aplicada",)]
    upcoming_int = sorted([a for a in apps if a.interview_at and a.interview_at.date() >= today],
                          key=lambda a: a.interview_at)
    deadlines = list(db.scalars(select(Job).where(Job.deadline.is_not(None), Job.deadline >= today,
                                                  Job.deadline <= today + timedelta(days=10),
                                                  Job.status.in_(["nuevo", "guardado", "aplicando"]))
                                .order_by(Job.deadline)))
    deadlines = [j for j in deadlines if not (j.application and j.application.applied_at)]
    top = list(db.scalars(select(Job).where(Job.status.in_(["nuevo", "guardado"]), Job.score >= 70)
                          .order_by(Job.score.desc(), Job.found_at.desc()).limit(8)))
    new_today = db.scalar(select(func.count(Job.id)).where(Job.found_at >= datetime.utcnow() - timedelta(hours=26),
                                                          Job.status != "descartado")) or 0
    gdl = parse_date(goal.get("deadline"))
    days_left = (gdl - today).days if gdl else None
    last_run = db.scalar(select(Run).order_by(Run.id.desc()).limit(1))
    return dict(today=today, goal=goal, interviews=interviews, applied=applied, applied_today=applied_today,
                followups=followups, upcoming_interviews=upcoming_int, deadlines=deadlines, top=top,
                new_today=new_today, days_left=days_left, last_run=last_run, apps=apps)


def send_digest(db) -> bool:
    if not (config.SMTP_HOST and config.SMTP_USER and config.SMTP_PASSWORD and config.DIGEST_TO):
        return False
    d = dashboard_data(db)
    lines = [f"Hola Josefina, este es tu resumen del {d['today'].strftime('%d/%m/%Y')}.", "",
             f"Meta: {d['interviews']} de {d['goal'].get('interviews', 5)} entrevistas"
             + (f" — quedan {d['days_left']} días" if d['days_left'] is not None else ""),
             f"Vacantes nuevas: {d['new_today']}", ""]
    if d["top"]:
        lines.append("Mejores vacantes para ti:")
        for j in d["top"]:
            lines.append(f"  • {j.score} — {j.title} en {j.company} ({j.location})")
        lines.append("")
    if d["followups"]:
        lines.append("Toca mandar seguimiento a:")
        for a in d["followups"]:
            lines.append(f"  • {a.job.title} — {a.job.company} (aplicaste el {a.applied_at:%d/%m})")
        lines.append("")
    if d["deadlines"]:
        lines.append("Fechas límite próximas:")
        for j in d["deadlines"]:
            lines.append(f"  • {j.deadline:%d/%m} — {j.title} en {j.company}")
        lines.append("")
    lines.append(f"Abre tu panel: {config.APP_URL}")
    msg = EmailMessage()
    msg["Subject"] = f"Tu búsqueda de empleo — {d['new_today']} nuevas, {len(d['followups'])} seguimientos"
    msg["From"] = config.SMTP_USER
    msg["To"] = config.DIGEST_TO
    msg.set_content("\n".join(lines))
    with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=30) as s:
        s.starttls()
        s.login(config.SMTP_USER, config.SMTP_PASSWORD)
        s.send_message(msg)
    return True


# ---------------- Programador interno ----------------

def _due(db) -> bool:
    sched = get_setting(db, "schedule")
    every = float(sched.get("every_hours", 24))
    hour = int(sched.get("hour_local", 6))
    now_local = datetime.now(ZoneInfo(config.APP_TZ))
    last = db.scalar(select(Run).where(Run.trigger == "auto").order_by(Run.id.desc()).limit(1))
    if last is None:
        return now_local.hour >= hour
    elapsed_h = (datetime.utcnow() - last.started_at).total_seconds() / 3600
    return elapsed_h >= every - 0.5 and now_local.hour >= hour


def scheduler_loop(stop: threading.Event) -> None:
    stop.wait(60)
    while not stop.is_set():
        try:
            db = SessionLocal()
            try:
                due = _due(db)
            finally:
                db.close()
            if due:
                run_search("auto")
        except Exception:
            log.error("scheduler: %s", traceback.format_exc())
        stop.wait(30 * 60)
