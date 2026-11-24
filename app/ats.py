"""Revisión ATS: compara el CV adaptado con las palabras clave de la vacante y revisa el formato."""
from __future__ import annotations

import re

from .textutil import contains_phrase, tokens


def resume_to_text(r: dict) -> str:
    if not r:
        return ""
    parts = [r.get("name", ""), r.get("headline", ""), r.get("summary", ""), " ".join(r.get("skills", []))]
    for e in r.get("experience", []):
        parts += [e.get("title", ""), e.get("company", ""), " ".join(e.get("bullets", []))]
    for e in r.get("education", []):
        parts += [e.get("degree", ""), e.get("school", ""), e.get("details", "")]
    parts += r.get("certifications", []) + r.get("languages", [])
    for s in r.get("extra_sections", []):
        parts += [s.get("title", "")] + s.get("items", [])
    return "\n".join(p for p in parts if p)


def check(resume: dict, keywords: list[str], job_title: str = "") -> dict:
    text = resume_to_text(resume)
    toks = tokens(text)
    kws = [k for k in dict.fromkeys(k.strip() for k in keywords) if k]
    matched = [k for k in kws if contains_phrase(toks, k)]
    missing = [k for k in kws if k not in matched]
    coverage = round(100 * len(matched) / len(kws)) if kws else 0

    checks = []

    def add(ok: bool, label: str, tip: str = ""):
        checks.append({"ok": ok, "label": label, "tip": tip})

    c = resume.get("contact", {}) if resume else {}
    add(bool(c.get("email")), "Tiene email de contacto", "Agrega tu email en el perfil.")
    add(bool(c.get("phone")), "Tiene teléfono", "Agrega tu teléfono en el perfil.")
    add(bool(c.get("location")), "Tiene ciudad/estado", "Muchos ATS filtran por ubicación.")
    add(len((resume or {}).get("summary", "")) >= 150, "Resumen profesional sólido")
    add(len((resume or {}).get("skills", [])) >= 8, "Sección de habilidades con 8+ elementos")
    exp = (resume or {}).get("experience", [])
    add(bool(exp) and all(e.get("start") for e in exp), "Experiencia con fechas")
    add(bool(exp) and all(len(e.get("bullets", [])) >= 1 for e in exp), "Cada puesto tiene logros")
    words = len(text.split())
    add(250 <= words <= 1000, f"Largo adecuado ({words} palabras, 1–2 páginas)")
    add(not re.search(r"[☀-➿\U0001F300-\U0001FAFF]", text), "Sin emojis ni símbolos raros")
    if job_title:
        t_ok = contains_phrase(toks, job_title) or len(set(tokens(job_title)) & set(toks)) >= max(1, len(tokens(job_title)) - 1)
        add(t_ok, "Incluye el título del puesto", "El título exacto del puesto ayuda mucho en el ATS.")
    add(True, "Formato DOCX de una columna, sin tablas ni imágenes")

    fmt = round(100 * sum(1 for x in checks if x["ok"]) / len(checks))
    score = round(0.7 * coverage + 0.3 * fmt) if kws else fmt
    return {"score": score, "coverage": coverage, "format": fmt, "matched": matched, "missing": missing,
            "checks": checks}
