import html as htmlmod
import re
from datetime import date, datetime
from typing import Optional

from bs4 import BeautifulSoup

STOP = set("""a an and the of to for in on at by with from or as is are be this that our your you we will
de la el los las y o en con para por del al un una que se su sus es""".split())


def html_to_text(s: str) -> str:
    if not s:
        return ""
    s = htmlmod.unescape(s)
    if "<" in s and ">" in s:
        soup = BeautifulSoup(s, "html.parser")
        for li in soup.find_all("li"):
            li.insert_before("\n• ")
        for br in soup.find_all(["br", "p", "div", "h1", "h2", "h3", "h4", "ul", "ol"]):
            br.insert_before("\n")
        s = soup.get_text()
    s = re.sub(r"[ \t\r\f\v]+", " ", s)
    s = re.sub(r"\n\s*\n+", "\n\n", s)
    return s.strip()


def norm(s: str) -> str:
    s = (s or "").lower()
    s = (s.replace("á", "a").replace("é", "e").replace("í", "i").replace("ó", "o").replace("ú", "u")
         .replace("ñ", "n").replace("ü", "u"))
    return re.sub(r"[^a-z0-9+#./ ]+", " ", s)


def stem(w: str) -> str:
    for suf in ("ing", "ies", "es", "ed", "s"):
        if len(w) > 4 and w.endswith(suf):
            return w[: -len(suf)]
    return w


def tokens(s: str) -> list[str]:
    return [stem(t) for t in norm(s).split() if t and t not in STOP]


def contains_phrase(haystack_tokens: list[str], phrase: str) -> bool:
    p = tokens(phrase)
    if not p:
        return False
    n = len(p)
    if n == 1:
        return p[0] in set(haystack_tokens)
    for i in range(len(haystack_tokens) - n + 1):
        if haystack_tokens[i:i + n] == p:
            return True
    return False


def parse_date(v) -> Optional[date]:
    if not v:
        return None
    if isinstance(v, date) and not isinstance(v, datetime):
        return v
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, (int, float)):
        ts = v / 1000 if v > 1e11 else v
        try:
            return datetime.utcfromtimestamp(ts).date()
        except Exception:
            return None
    s = str(v).strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%d", "%a, %d %b %Y %H:%M:%S %z", "%m/%d/%Y"):
        try:
            return datetime.strptime(s.replace("Z", "+0000") if "%z" in fmt else s, fmt).date()
        except Exception:
            continue
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        try:
            return date(int(m[1]), int(m[2]), int(m[3]))
        except Exception:
            return None
    return None


def dedupe_key(title: str, company: str) -> str:
    return (" ".join(norm(company).split()) + "|" + " ".join(norm(title).split()))[:300]


EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def find_email(text: str) -> str:
    for m in EMAIL_RE.findall(text or ""):
        low = m.lower()
        if not any(x in low for x in ("noreply", "no-reply", "example.com", "privacy", "accommodation")):
            return m
    return ""
