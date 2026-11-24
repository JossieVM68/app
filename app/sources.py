"""Fuentes de empleo. Solo APIs públicas u oficiales (nada de scraping de LinkedIn/Indeed, que lo prohíben).

Cada función devuelve una lista de dicts normalizados:
  source, external_id, title, company, location, remote, url, description, salary, posted_at, deadline
"""
from __future__ import annotations

import html as htmlmod
from typing import Callable

import httpx

from . import config
from .textutil import html_to_text, norm, parse_date

TIMEOUT = httpx.Timeout(25.0, connect=10.0)


def _client() -> httpx.Client:
    return httpx.Client(timeout=TIMEOUT, headers={"User-Agent": config.USER_AGENT, "Accept": "application/json"},
                        follow_redirects=True)


def _job(**kw) -> dict:
    base = dict(source="", external_id="", title="", company="", location="", remote=False, url="",
                description="", salary="", posted_at=None, deadline=None)
    base.update(kw)
    base["external_id"] = str(base["external_id"])[:255]
    base["title"] = (base["title"] or "").strip()[:300]
    base["company"] = (base["company"] or "").strip()[:200]
    base["location"] = (base["location"] or "").strip()[:300]
    base["salary"] = (base["salary"] or "")[:200]
    base["description"] = (base["description"] or "")[:30000]
    if not base["remote"]:
        base["remote"] = "remote" in norm(base["location"]) or "remoto" in norm(base["location"])
    return base


def _money(lo, hi, cur="USD") -> str:
    try:
        lo = int(float(lo)) if lo else 0
        hi = int(float(hi)) if hi else 0
    except Exception:
        return ""
    if lo and hi:
        return f"{cur} {lo:,}–{hi:,}"
    if lo or hi:
        return f"{cur} {(lo or hi):,}"
    return ""


def _matches(text: str, terms: list[str]) -> bool:
    if not terms:
        return True
    t = norm(text)
    for term in terms:
        words = [w for w in norm(term).split() if len(w) > 2]
        if words and all(w in t for w in words):
            return True
    return False


# ---------------- Tableros de empresas (ATS públicos) ----------------

def greenhouse(slugs: list[str], terms: list[str], **_) -> list[dict]:
    out = []
    with _client() as c:
        for slug in slugs:
            r = c.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", params={"content": "true"})
            if r.status_code != 200:
                continue
            company = slug
            try:
                info = c.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}")
                if info.status_code == 200:
                    company = info.json().get("name") or slug
            except Exception:
                pass
            for j in r.json().get("jobs", []):
                if not _matches(j.get("title", ""), terms):
                    continue
                out.append(_job(source="greenhouse", external_id=f"{slug}:{j['id']}", title=j.get("title"),
                                company=company, location=(j.get("location") or {}).get("name", ""),
                                url=j.get("absolute_url", ""),
                                description=html_to_text(htmlmod.unescape(j.get("content", ""))),
                                posted_at=parse_date(j.get("first_published") or j.get("updated_at"))))
    return out


def lever(slugs: list[str], terms: list[str], **_) -> list[dict]:
    out = []
    with _client() as c:
        for slug in slugs:
            r = c.get(f"https://api.lever.co/v0/postings/{slug}", params={"mode": "json"})
            if r.status_code != 200:
                continue
            for j in r.json():
                if not _matches(j.get("text", ""), terms):
                    continue
                cats = j.get("categories") or {}
                parts = [j.get("descriptionPlain", "")]
                for lst in j.get("lists", []) or []:
                    parts.append(lst.get("text", ""))
                    parts.append(html_to_text(lst.get("content", "")))
                parts.append(j.get("additionalPlain", ""))
                sal = j.get("salaryRange") or {}
                out.append(_job(source="lever", external_id=j["id"], title=j.get("text"), company=slug.title(),
                                location=cats.get("location", ""),
                                remote=(j.get("workplaceType") == "remote"),
                                url=j.get("hostedUrl", ""), description="\n".join(p for p in parts if p),
                                salary=_money(sal.get("min"), sal.get("max"), sal.get("currency", "USD")),
                                posted_at=parse_date(j.get("createdAt"))))
    return out


def ashby(slugs: list[str], terms: list[str], **_) -> list[dict]:
    out = []
    with _client() as c:
        for slug in slugs:
            r = c.get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}",
                      params={"includeCompensation": "true"})
            if r.status_code != 200:
                continue
            for j in r.json().get("jobs", []):
                if not _matches(j.get("title", ""), terms):
                    continue
                comp = (j.get("compensation") or {}).get("compensationTierSummary") or ""
                out.append(_job(source="ashby", external_id=f"{slug}:{j['id']}", title=j.get("title"),
                                company=slug.replace("-", " ").title(), location=j.get("location", ""),
                                remote=bool(j.get("isRemote")), url=j.get("jobUrl", ""),
                                description=j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml", "")),
                                salary=comp, posted_at=parse_date(j.get("publishedAt"))))
    return out


def smartrecruiters(slugs: list[str], terms: list[str], **_) -> list[dict]:
    out = []
    with _client() as c:
        for slug in slugs:
            r = c.get(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings", params={"limit": 100})
            if r.status_code != 200:
                continue
            for j in r.json().get("content", []):
                if not _matches(j.get("name", ""), terms):
                    continue
                desc = ""
                try:
                    d = c.get(j["ref"])
                    if d.status_code == 200:
                        secs = ((d.json().get("jobAd") or {}).get("sections") or {})
                        desc = "\n\n".join(html_to_text((secs.get(k) or {}).get("text", ""))
                                           for k in ("companyDescription", "jobDescription", "qualifications",
                                                     "additionalInformation"))
                except Exception:
                    pass
                loc = j.get("location") or {}
                out.append(_job(source="smartrecruiters", external_id=j["id"], title=j.get("name"),
                                company=(j.get("company") or {}).get("name", slug),
                                location=", ".join(x for x in [loc.get("city"), loc.get("region"), loc.get("country")] if x),
                                remote=bool(loc.get("remote")),
                                url=f"https://jobs.smartrecruiters.com/{slug}/{j['id']}", description=desc,
                                posted_at=parse_date(j.get("releasedDate"))))
    return out


# ---------------- Agregadores sin llave ----------------

def remotive(terms: list[str], **_) -> list[dict]:
    out = []
    with _client() as c:
        for term in terms[:6]:
            r = c.get("https://remotive.com/api/remote-jobs", params={"search": term, "limit": 50})
            if r.status_code != 200:
                continue
            for j in r.json().get("jobs", []):
                out.append(_job(source="remotive", external_id=j["id"], title=j.get("title"),
                                company=j.get("company_name"),
                                location=f"Remote ({j.get('candidate_required_location', '')})", remote=True,
                                url=j.get("url"), description=html_to_text(j.get("description", "")),
                                salary=j.get("salary") or "", posted_at=parse_date(j.get("publication_date"))))
    return out


def remoteok(terms: list[str], **_) -> list[dict]:
    out = []
    with _client() as c:
        r = c.get("https://remoteok.com/api")
        if r.status_code != 200:
            return out
        for j in r.json()[1:]:
            text = f"{j.get('position', '')} {' '.join(j.get('tags') or [])}"
            if not _matches(text, terms):
                continue
            out.append(_job(source="remoteok", external_id=j.get("id"), title=j.get("position"),
                            company=j.get("company"), location=f"Remote {j.get('location') or ''}".strip(),
                            remote=True, url=j.get("url"), description=html_to_text(j.get("description", "")),
                            salary=_money(j.get("salary_min"), j.get("salary_max")),
                            posted_at=parse_date(j.get("date"))))
    return out


def himalayas(terms: list[str], **_) -> list[dict]:
    out = []
    with _client() as c:
        for term in terms[:6]:
            r = c.get("https://himalayas.app/jobs/api/search", params={"q": term, "limit": 50})
            if r.status_code != 200:
                continue
            for j in r.json().get("jobs", []):
                locs = j.get("locationRestrictions") or []
                out.append(_job(source="himalayas", external_id=j.get("guid") or j.get("applicationLink"),
                                title=j.get("title"), company=j.get("companyName"),
                                location="Remote" + (f" ({', '.join(locs)})" if locs else ""), remote=True,
                                url=j.get("applicationLink") or j.get("guid"),
                                description=html_to_text(j.get("description") or j.get("excerpt", "")),
                                salary=_money(j.get("minSalary"), j.get("maxSalary"), j.get("currency") or "USD"),
                                posted_at=parse_date(j.get("pubDate"))))
    return out


def jobicy(terms: list[str], **_) -> list[dict]:
    out = []
    with _client() as c:
        for term in terms[:6]:
            r = c.get("https://jobicy.com/api/v2/remote-jobs", params={"count": 50, "tag": term})
            if r.status_code != 200:
                continue
            for j in r.json().get("jobs", []) or []:
                out.append(_job(source="jobicy", external_id=j.get("id"), title=htmlmod.unescape(j.get("jobTitle", "")),
                                company=j.get("companyName"), location=f"Remote ({j.get('jobGeo', '')})", remote=True,
                                url=j.get("url"), description=html_to_text(j.get("jobDescription", "")),
                                salary=_money(j.get("annualSalaryMin"), j.get("annualSalaryMax"),
                                              j.get("salaryCurrency") or "USD"),
                                posted_at=parse_date(j.get("pubDate"))))
    return out


def themuse(terms: list[str], locations: list[str], **_) -> list[dict]:
    out = []
    params_base = []
    for loc in (locations or [])[:3]:
        params_base.append(("location", loc))
    params_base.append(("location", "Flexible / Remote"))
    with _client() as c:
        for page in range(0, 5):
            r = c.get("https://www.themuse.com/api/public/jobs", params=params_base + [("page", page)])
            if r.status_code != 200:
                break
            data = r.json()
            for j in data.get("results", []):
                if not _matches(j.get("name", ""), terms):
                    continue
                locs = ", ".join(l.get("name", "") for l in j.get("locations", []))
                out.append(_job(source="themuse", external_id=j["id"], title=j.get("name"),
                                company=(j.get("company") or {}).get("name", ""), location=locs,
                                url=(j.get("refs") or {}).get("landing_page", ""),
                                description=html_to_text(j.get("contents", "")),
                                posted_at=parse_date(j.get("publication_date"))))
            if page + 1 >= data.get("page_count", 0):
                break
    return out


# ---------------- Agregadores con llave (opcionales) ----------------

def adzuna(terms: list[str], locations: list[str], center: str = "", radius_km: int = 0, **_) -> list[dict]:
    if not (config.ADZUNA_APP_ID and config.ADZUNA_APP_KEY):
        return []
    out = []
    locs = [center] if center else (locations[:3] or [""])
    with _client() as c:
        for term in terms[:5]:
            for loc in locs:
                params = {"app_id": config.ADZUNA_APP_ID, "app_key": config.ADZUNA_APP_KEY, "what": term,
                          "results_per_page": 50, "max_days_old": 14, "content-type": "application/json"}
                if loc:
                    params["where"] = loc
                    if radius_km:
                        params["distance"] = radius_km
                r = c.get("https://api.adzuna.com/v1/api/jobs/us/search/1", params=params)
                if r.status_code != 200:
                    continue
                for j in r.json().get("results", []):
                    out.append(_job(source="adzuna", external_id=j["id"], title=html_to_text(j.get("title", "")),
                                    company=(j.get("company") or {}).get("display_name", ""),
                                    location=(j.get("location") or {}).get("display_name", ""),
                                    url=j.get("redirect_url"), description=html_to_text(j.get("description", "")),
                                    salary=_money(j.get("salary_min"), j.get("salary_max")),
                                    posted_at=parse_date(j.get("created"))))
    return out


def usajobs(terms: list[str], locations: list[str], **_) -> list[dict]:
    if not (config.USAJOBS_API_KEY and config.USAJOBS_EMAIL):
        return []
    out = []
    headers = {"Host": "data.usajobs.gov", "User-Agent": config.USAJOBS_EMAIL,
               "Authorization-Key": config.USAJOBS_API_KEY}
    with httpx.Client(timeout=TIMEOUT, headers=headers) as c:
        for term in terms[:5]:
            params = {"Keyword": term, "ResultsPerPage": 50}
            if locations:
                params["LocationName"] = locations[0]
            r = c.get("https://data.usajobs.gov/api/search", params=params)
            if r.status_code != 200:
                continue
            for item in r.json().get("SearchResult", {}).get("SearchResultItems", []):
                d = item.get("MatchedObjectDescriptor", {})
                rem = (d.get("PositionRemuneration") or [{}])[0]
                details = (d.get("UserArea") or {}).get("Details") or {}
                desc = "\n\n".join(x for x in [details.get("JobSummary", ""), d.get("QualificationSummary", ""),
                                               "\n".join(details.get("MajorDuties") or [])] if x)
                out.append(_job(source="usajobs", external_id=d.get("PositionID"), title=d.get("PositionTitle"),
                                company=d.get("OrganizationName"), location=d.get("PositionLocationDisplay", ""),
                                url=d.get("PositionURI"), description=desc,
                                salary=_money(rem.get("MinimumRange"), rem.get("MaximumRange")),
                                posted_at=parse_date(d.get("PublicationStartDate")),
                                deadline=parse_date(d.get("ApplicationCloseDate"))))
    return out


def jooble(terms: list[str], locations: list[str], center: str = "", radius_km: int = 0, **_) -> list[dict]:
    if not config.JOOBLE_API_KEY:
        return []
    out = []
    with _client() as c:
        for term in terms[:5]:
            body = {"keywords": term, "location": center or (locations or [""])[0]}
            if radius_km:
                body["radius"] = str(min([4, 8, 16, 26, 40, 80], key=lambda x: abs(x - radius_km)))
            r = c.post(f"https://jooble.org/api/{config.JOOBLE_API_KEY}", json=body)
            if r.status_code != 200:
                continue
            for j in r.json().get("jobs", []):
                out.append(_job(source="jooble", external_id=j.get("id"), title=html_to_text(j.get("title", "")),
                                company=j.get("company", ""), location=j.get("location", ""), url=j.get("link"),
                                description=html_to_text(j.get("snippet", "")), salary=j.get("salary") or "",
                                posted_at=parse_date(j.get("updated"))))
    return out


def serpapi(terms: list[str], locations: list[str], center: str = "", radius_km: int = 0, **_) -> list[dict]:
    """Google Jobs (incluye publicaciones de LinkedIn, Indeed, Glassdoor, etc. de forma legítima)."""
    if not config.SERPAPI_KEY:
        return []
    out = []
    locs = locations[:1] or [""]
    with _client() as c:
        for term in terms[:6]:
            for loc in locs:
                params = {"engine": "google_jobs", "q": term, "api_key": config.SERPAPI_KEY, "hl": "en"}
                if loc:
                    params["location"] = "Chicago, Illinois, United States" if "chicago" in loc.lower() else loc
                if radius_km:
                    params["lrad"] = radius_km
                r = c.get("https://serpapi.com/search.json", params=params)
                if r.status_code != 200:
                    continue
                for j in r.json().get("jobs_results", []):
                    ext = j.get("detected_extensions") or {}
                    opts = j.get("apply_options") or []
                    url = (opts[0].get("link") if opts else "") or j.get("share_link", "")
                    out.append(_job(source="google", external_id=j.get("job_id", "")[:255], title=j.get("title"),
                                    company=j.get("company_name"), location=j.get("location", ""),
                                    remote=bool(ext.get("work_from_home")), url=url,
                                    description=j.get("description", ""), salary=ext.get("salary", "") or "",
                                    posted_at=None))
    return out


def claude_web(terms: list[str], locations: list[str], center: str = "", radius_km: int = 0,
               titles: list[str] | None = None, min_hourly: float = 0, **_) -> list[dict]:
    """Claude busca en la web (Indeed, LinkedIn, sitios de agencias…) vacantes cerca de casa."""
    from . import ai
    import hashlib
    miles = radius_km / 1.609 if radius_km else 10
    items = ai.web_search_jobs(titles or terms, center, (locations or ["Chicago, IL"])[0], miles, min_hourly)
    out = []
    for x in items:
        url = str(x.get("url", ""))
        out.append(_job(source="web", external_id=hashlib.sha1(url.encode()).hexdigest(), title=x.get("title"),
                        company=x.get("company"), location=x.get("address", ""), url=url,
                        description=x.get("summary", ""), salary=x.get("pay", ""),
                        posted_at=parse_date(x.get("posted")), deadline=parse_date(x.get("deadline"))))
    return out


SOURCES: dict[str, tuple[str, Callable, str]] = {
    "claude_web": ("Búsqueda web con IA (Indeed, LinkedIn, agencias…)", claude_web, "ia"),
    # key: (nombre visible, función, tipo)
    "remotive": ("Remotive (remoto)", remotive, "libre"),
    "remoteok": ("RemoteOK (remoto)", remoteok, "libre"),
    "himalayas": ("Himalayas (remoto)", himalayas, "libre"),
    "jobicy": ("Jobicy (remoto)", jobicy, "libre"),
    "themuse": ("The Muse", themuse, "libre"),
    "greenhouse": ("Greenhouse (empresas)", greenhouse, "empresas"),
    "lever": ("Lever (empresas)", lever, "empresas"),
    "ashby": ("Ashby (empresas)", ashby, "empresas"),
    "smartrecruiters": ("SmartRecruiters (empresas)", smartrecruiters, "empresas"),
    "adzuna": ("Adzuna", adzuna, "llave"),
    "usajobs": ("USAJobs (gobierno)", usajobs, "llave"),
    "jooble": ("Jooble", jooble, "llave"),
    "serpapi": ("Google Jobs (LinkedIn, Indeed, Glassdoor…)", serpapi, "llave"),
}

KEY_READY = {
    "claude_web": lambda: __import__("app.ai", fromlist=["available"]).available(),
    "adzuna": lambda: bool(config.ADZUNA_APP_ID and config.ADZUNA_APP_KEY),
    "usajobs": lambda: bool(config.USAJOBS_API_KEY and config.USAJOBS_EMAIL),
    "jooble": lambda: bool(config.JOOBLE_API_KEY),
    "serpapi": lambda: bool(config.SERPAPI_KEY),
}


def validate_board(ats: str, slug: str) -> bool:
    urls = {
        "greenhouse": f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
        "lever": f"https://api.lever.co/v0/postings/{slug}?mode=json&limit=1",
        "ashby": f"https://api.ashbyhq.com/posting-api/job-board/{slug}",
        "smartrecruiters": f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=1",
    }
    if ats not in urls:
        return False
    try:
        with _client() as c:
            r = c.get(urls[ats])
            if r.status_code != 200:
                return False
            if ats == "smartrecruiters":
                return r.json().get("totalFound", 0) > 0
            return True
    except Exception:
        return False
