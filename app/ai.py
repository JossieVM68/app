"""Todo lo que usa a Claude: leer el CV, puntuar vacantes, adaptar CV/carta/emails, sugerir empresas."""
from __future__ import annotations

import base64
import json
import os
from typing import Any

import anthropic

from .config import ANTHROPIC_MODEL


class AIError(Exception):
    pass


_client: anthropic.Anthropic | None = None


def available() -> bool:
    return bool(os.getenv("ANTHROPIC_API_KEY"))


def client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(max_retries=3)
    return _client


# ---------- utilidades de esquema (structured outputs exige additionalProperties:false y required) ----------

def S() -> dict:
    return {"type": "string"}


def I() -> dict:
    return {"type": "integer"}


def B() -> dict:
    return {"type": "boolean"}


def A(items: dict) -> dict:
    return {"type": "array", "items": items}


def O(props: dict) -> dict:
    return {"type": "object", "properties": props, "required": list(props.keys()), "additionalProperties": False}


RESUME_SCHEMA = O({
    "name": S(),
    "headline": S(),
    "contact": O({"email": S(), "phone": S(), "location": S(), "linkedin": S(), "website": S()}),
    "summary": S(),
    "skills": A(S()),
    "experience": A(O({"title": S(), "company": S(), "location": S(), "start": S(), "end": S(),
                       "bullets": A(S())})),
    "education": A(O({"degree": S(), "school": S(), "location": S(), "year": S(), "details": S()})),
    "certifications": A(S()),
    "languages": A(S()),
    "extra_sections": A(O({"title": S(), "items": A(S())})),
})


def _call(system: str, content: Any, schema: dict, effort: str = "medium", max_tokens: int = 32000) -> dict:
    if not available():
        raise AIError("Falta la llave de Claude (ANTHROPIC_API_KEY). Agrégala en Render para usar esta función.")
    try:
        with client().beta.messages.stream(
            model=ANTHROPIC_MODEL,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": content}],
            output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        ) as stream:
            msg = stream.get_final_message()
    except anthropic.AuthenticationError as e:
        raise AIError("La llave de Claude no es válida.") from e
    except anthropic.RateLimitError as e:
        raise AIError("Claude está ocupado (límite de uso). Intenta en unos minutos.") from e
    except anthropic.APIStatusError as e:
        raise AIError(f"Error de Claude ({e.status_code}).") from e
    except anthropic.APIConnectionError as e:
        raise AIError("No se pudo conectar con Claude.") from e

    if msg.stop_reason == "refusal":
        raise AIError("Claude no pudo procesar esta solicitud.")
    if msg.stop_reason == "max_tokens":
        raise AIError("La respuesta fue demasiado larga. Intenta de nuevo.")
    text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise AIError("Respuesta inválida de Claude. Intenta de nuevo.") from e


# ---------- 1) Leer el CV y construir el perfil ----------

PROFILE_SYSTEM = """You are an expert career strategist and executive resume writer helping ONE person, Josefina, \
find a job. You will read her resume (and any notes she adds) and build her master profile.

Rules:
- Extract her real information faithfully. Never invent employers, titles, dates, degrees, certifications, \
metrics or skills. If something is missing, leave the field as an empty string or empty list.
- "resume" is her complete master resume, cleaned up, in the resume's original language. Bullets should be \
achievement-oriented but only restate what the source supports.
- "about" is written in Spanish, warm and clear, for Josefina herself to read and confirm.
- target_roles / search_terms: realistic job titles employers actually post (in English for the US market, \
plus Spanish variants if she is bilingual), from most to least likely fit.
- keywords: hard skills, tools, certifications and domain terms an ATS would look for in her field."""

PROFILE_SCHEMA = O({
    "resume": RESUME_SCHEMA,
    "about": O({
        "resumen": S(),
        "personalidad": S(),
        "fortalezas": A(S()),
        "valores_y_motivaciones": S(),
        "logros_clave": A(S()),
        "nivel": S(),
        "industrias": A(S()),
        "target_roles": A(S()),
        "search_terms": A(S()),
        "keywords": A(S()),
        "idiomas": A(S()),
        "recomendaciones": A(S()),
    }),
})


def build_profile(resume_text: str, pdf_bytes: bytes | None, notes: str) -> dict:
    content: list[dict] = []
    if pdf_bytes:
        content.append({"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                                       "data": base64.standard_b64encode(pdf_bytes).decode()}})
    text = ""
    if resume_text:
        text += f"<resume>\n{resume_text}\n</resume>\n\n"
    if notes:
        text += f"<notas_de_josefina>\n{notes}\n</notas_de_josefina>\n\n"
    text += "Build Josefina's master profile."
    content.append({"type": "text", "text": text})
    return _call(PROFILE_SYSTEM, content, PROFILE_SCHEMA, effort="high")


# ---------- 2) Puntuar vacantes ----------

SCORE_SYSTEM = """You are a meticulous recruiter screening job postings for ONE candidate, Josefina. \
Score how well each posting fits her REAL profile and preferences, 0-100:
90-100 = excellent fit, apply today; 75-89 = strong; 60-74 = possible with tailoring; 40-59 = stretch; <40 = poor.
Consider: required qualifications vs. her actual experience, seniority, location/remote preferences, salary floor, \
language requirements, and dealbreakers. Be honest — do not inflate.
Write "resumen", "fortalezas" and "brechas" in Spanish, short and concrete.
"keywords": the 10-20 most important ATS keywords/phrases from the posting (exact wording from the posting).
"deadline": application deadline as YYYY-MM-DD if the posting states one, else "".
"contact_email": recruiter/application email if the posting lists one, else "".
"posting_language": "en" or "es".
"work_address": the most specific workplace address the posting gives (street + city + ZIP if present, \
otherwise neighborhood/city), else "".
"work_mode": "onsite", "hybrid", "remote" or "unknown".
"pay_min_hourly"/"pay_max_hourly": pay converted to US dollars PER HOUR (annual / 2080), 0 if not stated.
Her hard requirements are in <preferencias>: min_hourly pay, max_miles from home, onsite/hybrid only. \
A posting that clearly violates them must score below 40."""

SCORE_SCHEMA = O({
    "results": A(O({
        "id": I(), "score": I(), "resumen": S(), "fortalezas": A(S()), "brechas": A(S()),
        "keywords": A(S()), "deadline": S(), "contact_email": S(), "posting_language": S(),
        "work_address": S(), "work_mode": S(), "pay_min_hourly": {"type": "number"},
        "pay_max_hourly": {"type": "number"},
    }))
})


def profile_block(profile: dict, prefs: dict) -> str:
    p = {k: v for k, v in prefs.items() if k not in ("home_address", "home_coords")}
    return ("<perfil_de_josefina>\n" + json.dumps(profile, ensure_ascii=False) + "\n</perfil_de_josefina>\n"
            "<preferencias>\n" + json.dumps(p, ensure_ascii=False) + "\n</preferencias>")


def score_jobs(profile: dict, prefs: dict, jobs: list[dict]) -> list[dict]:
    """jobs: [{id, title, company, location, salary, description}] (máx ~8 por llamada)."""
    posts = []
    for j in jobs:
        posts.append(f"<posting id=\"{j['id']}\">\nTitle: {j['title']}\nCompany: {j['company']}\n"
                     f"Location: {j['location']}\nSalary: {j.get('salary') or 'n/a'}\n\n"
                     f"{(j.get('description') or '')[:9000]}\n</posting>")
    content = [
        {"type": "text", "text": profile_block(profile, prefs), "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": "\n\n".join(posts) + "\n\nScore every posting above. Return one result per id."},
    ]
    return _call(SCORE_SYSTEM, content, SCORE_SCHEMA, effort="low", max_tokens=16000)["results"]


# ---------- 3) Paquete de aplicación: CV adaptado + carta + emails ----------

PACKAGE_SYSTEM = """You are an elite resume writer and ATS optimization specialist. Create a complete, \
ready-to-send application package for Josefina for ONE specific job posting.

ABSOLUTE RULES (honesty):
- Use ONLY facts from her master profile. Never invent employers, job titles, dates, degrees, certifications, \
tools, numbers or achievements. Never claim a skill she does not have.
- You MAY: reorder, rephrase, emphasize relevant experience, merge or trim bullets, choose which skills to list, \
and mirror the posting's exact terminology wherever it truthfully describes her experience \
(e.g. if she did "client onboarding" and the posting says "customer onboarding", use the posting's wording).
- List anything required that she truly lacks in "brechas_honestas" (Spanish), never hide it in the resume.

ATS RULES for the resume:
- Standard section content only (summary, skills, experience, education, certifications, languages).
- Summary: 3-4 lines aimed squarely at this role, including the exact job title when truthful.
- Skills: 10-18 items, prioritizing exact keyword phrases from the posting that she genuinely has.
- Experience bullets: start with strong verbs, include the posting's keywords naturally, quantify only with real \
numbers from her profile. 3-6 bullets for recent roles, fewer for older ones.
- Plain text only: no emojis, no special symbols, no tables.
- Keep it to 1-2 pages.

LANGUAGE: write the resume, cover letter and emails in the language given in <idioma>.

COVER LETTER: 250-350 words, specific to this company and role, warm but professional, reflects her real \
personality and motivation, no clichés ("I am writing to express my interest..."), ends with a clear call to action. \
No placeholders except none — sign with her name.

EMAILS: "email_subject"/"email_body" = short email to send the application to a recruiter/hiring manager \
(mention the resume and cover letter are attached). "followup_subject"/"followup_body" = polite follow-up for \
7 days later. "linkedin_note" = connection note under 280 characters.
"keywords_usadas": posting keywords you included. "preparacion_entrevista": 5-8 likely interview questions for \
this role, each with a one-line tip on which of HER real experiences to use (Spanish)."""

PACKAGE_SCHEMA = O({
    "resume": RESUME_SCHEMA,
    "cover_letter": S(),
    "email_subject": S(),
    "email_body": S(),
    "followup_subject": S(),
    "followup_body": S(),
    "linkedin_note": S(),
    "keywords_usadas": A(S()),
    "brechas_honestas": A(S()),
    "preparacion_entrevista": A(S()),
})


def build_package(profile: dict, prefs: dict, job: dict, lang: str, keywords: list[str],
                  improve_hint: str = "") -> dict:
    lang_name = "English" if lang == "en" else "Spanish (español)"
    posting = (f"<posting>\nTitle: {job['title']}\nCompany: {job['company']}\nLocation: {job['location']}\n\n"
               f"{job['description'][:20000]}\n</posting>\n"
               f"<keywords_ats>{json.dumps(keywords, ensure_ascii=False)}</keywords_ats>\n"
               f"<idioma>{lang_name}</idioma>")
    if improve_hint:
        posting += f"\n<mejorar>{improve_hint}</mejorar>"
    content = [
        {"type": "text", "text": profile_block(profile, prefs), "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": posting + "\n\nCreate the full application package."},
    ]
    return _call(PACKAGE_SYSTEM, content, PACKAGE_SCHEMA, effort="high", max_tokens=48000)


# ---------- 4) Buscar vacantes en la web (búsqueda web de Claude) ----------

WEB_SYSTEM = """You are a job researcher finding CURRENT, real job openings for one candidate. Use web search to \
find postings published in the last 30 days that match her target roles, located within the given radius of her \
ZIP code (in-person or hybrid), paying at least the given hourly minimum when pay is stated. Look at job boards \
(Indeed, LinkedIn, Glassdoor, ZipRecruiter, SimplyHired, Idealist, Head Start / Early Head Start agencies, \
community organizations, school networks, city/county/state job sites, nonprofit career pages). Prefer employer \
career pages when you can find them. Only include postings you actually saw in search results; never invent. \
Return up to 25 postings.

At the end, output ONLY a JSON array inside <jobs></jobs> tags. Each item:
{"title":"", "company":"", "address":"street, city, ZIP if known else neighborhood/city", "pay":"as written", \
"url":"link to the posting", "posted":"YYYY-MM-DD or ''", "deadline":"YYYY-MM-DD or ''", \
"summary":"6-10 sentences with duties, requirements, schedule, benefits — use the posting's own wording"}"""


def web_search_jobs(titles: list[str], zip_code: str, area: str, miles: float, min_hourly: float) -> list[dict]:
    if not available():
        return []
    import re as _re
    q = (f"Target roles: {', '.join(titles[:10])}\nHome ZIP: {zip_code or 'n/a'} ({area})\n"
         f"Max distance: {miles:g} miles\nMinimum pay: ${min_hourly:g}/hour\nWork mode: in-person or hybrid only\n"
         "Find matching openings now.")
    messages: list = [{"role": "user", "content": q}]
    tools = [{"type": "web_search_20260209", "name": "web_search", "max_uses": 12}]
    resp = None
    try:
        for _ in range(4):
            with client().beta.messages.stream(model=ANTHROPIC_MODEL, max_tokens=32000, system=WEB_SYSTEM,
                                               messages=messages, tools=tools, output_config={"effort": "medium"},
                                               betas=["server-side-fallback-2026-07-01"],
                                               fallbacks="default") as stream:
                resp = stream.get_final_message()
            if resp.stop_reason != "pause_turn":
                break
            messages = [{"role": "user", "content": q}, {"role": "assistant", "content": resp.content}]
    except anthropic.APIError:
        return []
    if resp is None or resp.stop_reason == "refusal":
        return []
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    m = _re.search(r"<jobs>\s*(\[.*?\])\s*</jobs>", text, _re.S)
    if not m:
        return []
    try:
        items = json.loads(m.group(1))
    except json.JSONDecodeError:
        return []
    return [x for x in items if isinstance(x, dict) and x.get("title") and x.get("url")]


# ---------- 5) Sugerir empresas con tableros públicos ----------

COMPANIES_SYSTEM = """You help Josefina find employers to watch. Suggest real companies that are likely to hire for \
her target roles, in her locations or remote, AND that publish jobs on Greenhouse, Lever, Ashby or SmartRecruiters. \
For each, give the board slug as it appears in the public URL (e.g. boards.greenhouse.io/<slug>, \
jobs.lever.co/<slug>, jobs.ashbyhq.com/<slug>, jobs.smartrecruiters.com/<slug>). Suggest 40 varied companies \
(large and mid-size, different industries that fit her). Slugs will be verified automatically."""

COMPANIES_SCHEMA = O({"companies": A(O({"company": S(), "ats": S(), "slug": S()}))})


def suggest_companies(profile: dict, prefs: dict) -> list[dict]:
    content = [{"type": "text", "text": profile_block(profile, prefs) + "\n\nSuggest companies."}]
    return _call(COMPANIES_SYSTEM, content, COMPANIES_SCHEMA, effort="medium", max_tokens=16000)["companies"]
