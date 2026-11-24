"""Genera el CV y la carta en Word (.docx) con formato amigable para ATS: una columna, sin tablas ni imágenes."""
from __future__ import annotations

import io
import re

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.oxml import OxmlElement
from docx.shared import Pt, RGBColor, Inches

HEAD = {
    "en": {"summary": "PROFESSIONAL SUMMARY", "skills": "SKILLS", "experience": "PROFESSIONAL EXPERIENCE",
           "education": "EDUCATION", "certifications": "CERTIFICATIONS", "languages": "LANGUAGES"},
    "es": {"summary": "RESUMEN PROFESIONAL", "skills": "HABILIDADES", "experience": "EXPERIENCIA PROFESIONAL",
           "education": "EDUCACIÓN", "certifications": "CERTIFICACIONES", "languages": "IDIOMAS"},
}
INK = RGBColor(0x1F, 0x24, 0x30)


def _doc() -> Document:
    d = Document()
    for s in d.sections:
        s.top_margin = s.bottom_margin = Inches(0.7)
        s.left_margin = s.right_margin = Inches(0.8)
    st = d.styles["Normal"]
    st.font.name = "Calibri"
    st.element.rPr.rFonts.set(qn("w:eastAsia"), "Calibri")
    st.font.size = Pt(10.5)
    st.paragraph_format.space_after = Pt(2)
    return d


def _rule(p) -> None:
    pPr = p._p.get_or_add_pPr()
    bdr = OxmlElement("w:pBdr")
    b = OxmlElement("w:bottom")
    for k, v in (("w:val", "single"), ("w:sz", "6"), ("w:space", "1"), ("w:color", "999999")):
        b.set(qn(k), v)
    bdr.append(b)
    pPr.append(bdr)


def _heading(d: Document, text: str) -> None:
    p = d.add_paragraph()
    p.paragraph_format.space_before = Pt(10)
    p.paragraph_format.space_after = Pt(3)
    r = p.add_run(text)
    r.bold = True
    r.font.size = Pt(11.5)
    r.font.color.rgb = INK
    _rule(p)


def _bullet(d: Document, text: str) -> None:
    p = d.add_paragraph(style="List Bullet")
    p.paragraph_format.space_after = Pt(1)
    p.add_run(text)


def resume_docx(r: dict, lang: str = "en") -> bytes:
    h = HEAD.get(lang, HEAD["en"])
    d = _doc()
    p = d.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = p.add_run(r.get("name", ""))
    run.bold = True
    run.font.size = Pt(18)
    run.font.color.rgb = INK
    if r.get("headline"):
        p = d.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.add_run(r["headline"]).font.size = Pt(11.5)
    c = r.get("contact", {})
    line = " | ".join(x for x in [c.get("location"), c.get("phone"), c.get("email"), c.get("linkedin"),
                                  c.get("website")] if x)
    if line:
        p = d.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.add_run(line).font.size = Pt(10)

    if r.get("summary"):
        _heading(d, h["summary"])
        d.add_paragraph(r["summary"])
    if r.get("skills"):
        _heading(d, h["skills"])
        d.add_paragraph(" • ".join(r["skills"]))
    if r.get("experience"):
        _heading(d, h["experience"])
        for e in r["experience"]:
            p = d.add_paragraph()
            p.paragraph_format.space_before = Pt(5)
            t = p.add_run(e.get("title", ""))
            t.bold = True
            if e.get("company"):
                p.add_run(f" — {e['company']}")
            meta = " | ".join(x for x in [e.get("location"), " – ".join(x for x in [e.get("start"), e.get("end")] if x)] if x)
            if meta:
                m = d.add_paragraph()
                mr = m.add_run(meta)
                mr.italic = True
                mr.font.size = Pt(10)
            for b in e.get("bullets", []):
                _bullet(d, b)
    if r.get("education"):
        _heading(d, h["education"])
        for e in r["education"]:
            p = d.add_paragraph()
            t = p.add_run(e.get("degree", ""))
            t.bold = True
            rest = ", ".join(x for x in [e.get("school"), e.get("location"), e.get("year")] if x)
            if rest:
                p.add_run(f" — {rest}")
            if e.get("details"):
                d.add_paragraph(e["details"])
    if r.get("certifications"):
        _heading(d, h["certifications"])
        for x in r["certifications"]:
            _bullet(d, x)
    if r.get("languages"):
        _heading(d, h["languages"])
        d.add_paragraph(" • ".join(r["languages"]))
    for s in r.get("extra_sections", []):
        if s.get("items"):
            _heading(d, s.get("title", "").upper())
            for x in s["items"]:
                _bullet(d, x)
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def letter_docx(text: str, r: dict) -> bytes:
    d = _doc()
    d.styles["Normal"].font.size = Pt(11)
    p = d.add_paragraph()
    run = p.add_run(r.get("name", ""))
    run.bold = True
    run.font.size = Pt(14)
    c = r.get("contact", {})
    line = " | ".join(x for x in [c.get("location"), c.get("phone"), c.get("email"), c.get("linkedin")] if x)
    if line:
        d.add_paragraph(line)
    d.add_paragraph("")
    for para in re.split(r"\n\s*\n", text.strip()):
        q = d.add_paragraph(para.strip())
        q.paragraph_format.space_after = Pt(8)
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def safe_filename(s: str) -> str:
    s = re.sub(r"[^A-Za-z0-9 _-]+", "", s or "").strip().replace(" ", "_")
    return s[:60] or "documento"
