"""Carga perfil, vacantes y paquetes preparados por el asistente directamente en la base de datos.

Uso:  python tools/cargar.py privado/datos.json
Lee EXTERNAL_DB de .env (nunca se sube a GitHub).
"""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
for line in (ROOT / ".env").read_text().splitlines():
    if line.startswith("EXTERNAL_DB="):
        os.environ["DATABASE_URL"] = line.split("=", 1)[1].strip()

from sqlalchemy import select  # noqa: E402

from app import ats, geo, pipeline  # noqa: E402
from app.db import Application, Event, Job, SessionLocal, get_setting, init_db, set_setting  # noqa: E402
from app.textutil import dedupe_key, parse_date  # noqa: E402


def main(path: str) -> None:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    init_db()
    db = SessionLocal()
    if "profile" in data:
        set_setting(db, "profile", data["profile"])
        print("perfil cargado")
    if "prefs" in data:
        prefs = get_setting(db, "prefs")
        prefs.update(data["prefs"])
        set_setting(db, "prefs", prefs)
        print("preferencias cargadas")
    if "goal" in data:
        g = get_setting(db, "goal")
        g.update(data["goal"])
        set_setting(db, "goal", g)
    prefs = get_setting(db, "prefs")
    home = pipeline.home_coords(db, prefs)
    print("casa ubicada:", bool(home))
    for item in data.get("jobs", []):
        j = item["job"]
        ext = j.get("external_id") or dedupe_key(j["title"], j["company"])
        job = db.scalar(select(Job).where(Job.source == "asistente", Job.external_id == ext))
        if job is None:
            job = Job(source="asistente", external_id=ext, dedupe_key=dedupe_key(j["title"], j["company"]),
                      title=j["title"])
            db.add(job)
        job.title, job.company, job.location = j["title"], j["company"], j["location"]
        job.url, job.description, job.salary = j.get("url", ""), j["description"], j.get("salary", "")
        job.deadline = parse_date(j.get("deadline"))
        job.contact_email = j.get("contact_email", "")
        job.prefilter, job.score = 100, j["score"]
        job.analysis = j["analysis"]
        job.status = "nuevo"
        reasons = pipeline.apply_filters(job, prefs, home)
        db.flush()
        print(f"- {job.title} @ {job.company}: score {job.score}, "
              f"{job.analysis.get('distance_miles', '?')} mi, filtro={reasons}")
        if reasons:
            continue
        pkg = item.get("package")
        if pkg:
            a = job.application or Application(job_id=job.id, lang=item.get("lang", "en"))
            if a.id is None:
                db.add(a)
                db.flush()
            a.package = pkg
            a.contact_email = job.contact_email
            a.ats = ats.check(pkg["resume"], job.analysis.get("keywords", []), job.title)
            job.status = "aplicando"
            db.add(Event(application_id=a.id, kind="documentos",
                         note=f"Tu asistente preparó CV y carta (ATS {a.ats['score']}%)"))
            print(f"    paquete listo, ATS {a.ats['score']}% faltan: {a.ats['missing']}")
    db.commit()
    db.close()


if __name__ == "__main__":
    main(sys.argv[1])
