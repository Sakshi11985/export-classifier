"""PDF -> structured chunks.

Chunking strategy (L1): one chunk per GRI rule, per section/chapter note, per
heading (with its tariff items) and per heading's export-policy entry. Every
chunk carries a stable identifier (e.g. GRI-1, SEC-XV-NOTE-1, HEADING-9617,
POLICY-9617) so notes stay linked to the headings they modify and a clause can
be fetched by identifier. Nothing here is typed in by hand: every word of every
chunk is extracted from the supplied PDFs."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import pdfplumber

IN_SCOPE_CHAPTERS = {73, 96}

_WS = re.compile(r"\s+")


def clean(text: str | None) -> str:
    return _WS.sub(" ", (text or "").replace("­", "")).strip()


@dataclass
class Chunk:
    id: str
    type: str  # gri | note | heading | policy | policy_condition | chapter | section
    text: str
    metadata: dict = field(default_factory=dict)

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()[:16]


class NeedsOCRError(RuntimeError):
    pass


def text_layer_report(pdf_path: Path) -> dict:
    """Detect whether the PDF has a usable text layer (spec section 3)."""
    with pdfplumber.open(pdf_path) as pdf:
        pages = len(pdf.pages)
        chars = [len((p.extract_text() or "").strip()) for p in pdf.pages]
    empty = sum(1 for c in chars if c < 20)
    return {"pages": pages, "empty_pages": empty, "has_text_layer": empty < pages * 0.5 or pages == 0}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


# --------------------------------------------------------------------------- GRI
_GRI_START = re.compile(r"THE\s+GENERAL\s+RULES\s+FOR\s+THE\s+INTERPRETATION", re.I)
_GRI_END = re.compile(r"THE\s+GENERAL\s+EXPLANATORY\s+NOTES", re.I)


def parse_gri(pdf_path: Path) -> list[Chunk]:
    pages: list[str] = []
    with pdfplumber.open(pdf_path) as pdf:
        for p in pdf.pages:
            pages.append(p.extract_text() or "")
    full = "\n".join(pages)
    starts = [m for m in _GRI_START.finditer(full)]
    if not starts:
        raise ValueError("GRI start marker not found in " + str(pdf_path))
    start = starts[-1].end()  # the heading that precedes the numbered rules
    end_m = _GRI_END.search(full, start)
    if not end_m:
        raise ValueError("GRI end marker not found in " + str(pdf_path))
    body = full[start:end_m.start()]
    # drop the lead-in sentence
    lead = re.search(r"following principles:\s*", body)
    if lead:
        body = body[lead.end():]
    rule_re = re.compile(r"(?m)^\s*([1-6])\.\s+")
    marks = list(rule_re.finditer(body))
    chunks: list[Chunk] = []
    expected = 1
    picked = []
    for m in marks:  # keep only the in-sequence 1..6 markers
        if int(m.group(1)) == expected:
            picked.append(m)
            expected += 1
    if len(picked) != 6:
        raise ValueError(f"expected 6 GRI rules, found {len(picked)}")
    for i, m in enumerate(picked):
        n = int(m.group(1))
        seg = body[m.end(): picked[i + 1].start() if i + 1 < len(picked) else len(body)]
        sub_re = re.compile(r"(?m)(?:^|\s)\(([abc])\)\s+(?=[A-Z])")
        subs = list(sub_re.finditer(seg))
        page_no = _find_page(pages, clean(seg)[:40])
        if n in (2, 3, 5) and subs:
            for j, sm in enumerate(subs):
                txt = seg[sm.end(): subs[j + 1].start() if j + 1 < len(subs) else len(seg)]
                cid = f"GRI-{n}({sm.group(1)})"
                chunks.append(Chunk(cid, "gri", clean(txt), {
                    "title": f"General Rule for Interpretation {n}({sm.group(1)})",
                    "rule": n, "sub": sm.group(1), "source": pdf_path.name, "page": page_no,
                }))
        else:
            chunks.append(Chunk(f"GRI-{n}", "gri", clean(seg), {
                "title": f"General Rule for Interpretation {n}", "rule": n,
                "source": pdf_path.name, "page": page_no,
            }))
    return chunks


def _find_page(pages: list[str], needle: str) -> int | None:
    probe = clean(needle)[:25]
    for i, t in enumerate(pages):
        if probe and probe in clean(t):
            return i + 1
    return None


# ------------------------------------------------------------------- ITC(HS)
@dataclass
class _Row:
    cells: list[str]
    page: int


def _iter_rows(pdf_path: Path) -> Iterable[_Row]:
    with pdfplumber.open(pdf_path) as pdf:
        for pno, page in enumerate(pdf.pages, start=1):
            for table in page.extract_tables():
                for row in table:
                    cells = [clean(c) for c in row] + [""] * (8 - len(row))
                    yield _Row(cells, pno)


_SECTION_RE = re.compile(r"^Section\s+([IVXL]+),\s*(.+)$")
_CHAPTER_RE = re.compile(r"^CHAPTER\s+(\d+)\s*(.*)$")


def parse_itchs(pdf_path: Path) -> list[Chunk]:
    section = section_title = chapter = chapter_title = None
    mode = None
    notes: list[dict] = []
    conds: list[dict] = []
    tariff: list[dict] = []
    chapters: dict[int, dict] = {}
    sections: dict[str, dict] = {}
    for r in _iter_rows(pdf_path):
        c = r.cells
        c0 = c[0]
        m = _SECTION_RE.match(c0)
        if m:
            section, section_title = m.group(1), clean(m.group(2))
            chapter = None
            mode = None
            sections.setdefault(section, {"title": section_title, "chapters": set(), "page": r.page})
            continue
        m = _CHAPTER_RE.match(c0)
        if m:
            chapter = int(m.group(1))
            chapter_title = clean(m.group(2))
            mode = None
            chapters.setdefault(chapter, {"title": chapter_title, "section": section, "page": r.page})
            if section:
                sections[section]["chapters"].add(chapter)
            continue
        if c0 == "Main Notes":
            mode = "notes"
            continue
        if c0 == "Policy Conditions":
            mode = "policy_cond"
            continue
        if c0 == "Chapter Number":
            mode = "tariff"
            continue
        if c0 == "Sl. No.":
            continue
        if mode == "notes" and c0.isdigit() and c[1]:
            notes.append({"n": int(c0), "text": c[1], "section": section if chapter is None else None,
                          "chapter": chapter, "page": r.page, "src_section": section})
        elif mode == "policy_cond" and c0.isdigit() and c[1]:
            conds.append({"n": int(c0), "text": c[1], "chapter": chapter, "page": r.page})
        elif mode == "tariff" and c0.isdigit() and c[1].isdigit():
            tariff.append({"chapter": int(c0), "code": c[1], "desc": c[2], "dashes": c[3], "policy": c[4],
                           "condition": c[5], "notif_no": c[6], "notif_date": c[7], "page": r.page,
                           "section": section})
    return _build_itchs_chunks(pdf_path, notes, conds, tariff, chapters, sections)


def _roman_ok(s: str | None) -> bool:
    return bool(s)


def _build_itchs_chunks(pdf_path, notes, conds, tariff, chapters, sections) -> list[Chunk]:
    src = pdf_path.name
    # sections that contain an in-scope chapter
    sec_of_scope: dict[str, int] = {}
    for ch, info in chapters.items():
        if ch in IN_SCOPE_CHAPTERS and info["section"]:
            sec_of_scope[info["section"]] = ch
    chunks: dict[str, Chunk] = {}

    def add(ch: Chunk):
        if ch.id in chunks:
            if chunks[ch.id].text != ch.text and not ch.text.startswith(chunks[ch.id].text[:200]):
                # keep the first (fullest) occurrence; duplicates are page-boundary repeats
                pass
            return
        chunks[ch.id] = ch

    for ch, info in chapters.items():
        if ch not in IN_SCOPE_CHAPTERS:
            continue
        add(Chunk(f"CHAPTER-{ch}", "chapter", f"Chapter {ch}: {info['title']} (Section {info['section']})",
                  {"chapter": ch, "section": info["section"], "title": f"Chapter {ch}", "source": src,
                   "page": info["page"]}))
    for sec, ch in sec_of_scope.items():
        add(Chunk(f"SECTION-{sec}", "section", f"Section {sec}: {sections[sec]['title']}",
                  {"section": sec, "chapter": None, "title": f"Section {sec}", "source": src,
                   "page": sections[sec]["page"]}))

    for n in notes:
        if n["chapter"] is None:
            sec = n["section"]
            if sec not in sec_of_scope:
                continue
            nid = f"SEC-{sec}-NOTE-{n['n']}"
            add(Chunk(nid, "note", n["text"], {
                "scope": "section", "section": sec, "chapter": None, "note_no": n["n"],
                "title": f"Section {sec} Note {n['n']}", "source": src, "page": n["page"]}))
        else:
            if n["chapter"] not in IN_SCOPE_CHAPTERS:
                continue
            ch = n["chapter"]
            add(Chunk(f"CH{ch}-NOTE-{n['n']}", "note", n["text"], {
                "scope": "chapter", "section": chapters[ch]["section"], "chapter": ch, "note_no": n["n"],
                "title": f"Chapter {ch} Note {n['n']}", "source": src, "page": n["page"]}))

    for cnd in conds:
        ch = cnd["chapter"]
        if ch not in IN_SCOPE_CHAPTERS:
            continue
        add(Chunk(f"CH{ch}-POLICYCOND-{cnd['n']}", "policy_condition", cnd["text"], {
            "chapter": ch, "cond_no": cnd["n"], "title": f"Chapter {ch} Policy Condition {cnd['n']}",
            "source": src, "page": cnd["page"]}))

    # tariff rows -> headings + items
    by_heading: dict[str, dict] = {}
    order: list[str] = []
    for t in tariff:
        if t["chapter"] not in IN_SCOPE_CHAPTERS:
            continue
        code = t["code"]
        if len(code) == 2:
            continue
        h = code[:4]
        d = by_heading.setdefault(h, {"chapter": t["chapter"], "desc": "", "items": [], "page": t["page"]})
        if h not in order:
            order.append(h)
        if len(code) == 4:
            d["desc"] = t["desc"]
        else:
            d["items"].append(t)
    for h in order:
        d = by_heading[h]
        ch = d["chapter"]
        sec = chapters[ch]["section"]
        desc = d["desc"]
        if not desc and d["items"]:  # headings written as a single 8-digit row (e.g. 9604, 9611)
            desc = d["items"][0]["desc"]
        item_lines = []
        searchable = []
        for it in d["items"]:
            if len(it["code"]) == 8 or not it["policy"]:
                pass
            item_lines.append(f"- {it['code']} {it['dashes']} {it['desc']}".strip())
            searchable.append(it["desc"])
        sec_notes = [cid for cid in chunks if cid.startswith(f"SEC-{sec}-NOTE-")]
        ch_notes = [cid for cid in chunks if cid.startswith(f"CH{ch}-NOTE-")]
        text = f"Heading {h} (Chapter {ch}): {desc}"
        if item_lines:
            text += "\nTariff items:\n" + "\n".join(item_lines)
        add(Chunk(f"HEADING-{h}", "heading", text, {
            "heading": h, "chapter": ch, "section": sec, "desc": desc,
            "item_codes": [it["code"] for it in d["items"] if len(it["code"]) == 8],
            "linked_notes": sec_notes + ch_notes, "title": f"Heading {h}", "source": src,
            "page": d["page"], "searchable": " ".join([desc] + searchable)}))
        # policy chunk
        pol_lines, linked_cond = [], []
        items8 = [it for it in d["items"] if len(it["code"]) == 8] or d["items"]
        for it in items8:
            line = f"{it['code']}: export policy {it['policy'] or 'not stated'}"
            if it["condition"]:
                line += f"; {it['condition']}"
                mm = re.search(r"Policy Condition (\d+)", it["condition"])
                if mm:
                    linked_cond.append(f"CH{ch}-POLICYCOND-{mm.group(1)}")
            if it["notif_no"]:
                line += f" (Notification {it['notif_no']} dated {it['notif_date']})"
            pol_lines.append(line)
        if not pol_lines and d["desc"]:
            continue
        add(Chunk(f"POLICY-{h}", "policy",
                  f"DGFT ITC(HS) export policy for heading {h} ({desc}):\n" + "\n".join(pol_lines), {
                      "heading": h, "chapter": ch, "title": f"Export policy for heading {h}",
                      "linked_conditions": sorted(set(linked_cond)),
                      "items": [{"code": it["code"], "description": it["desc"], "policy": it["policy"],
                                 "condition": it["condition"], "notification": it["notif_no"],
                                 "notification_date": it["notif_date"]} for it in items8],
                      "source": src, "page": d["page"]}))
    return list(chunks.values())


def parse_corpus(corpus_dir: Path) -> tuple[list[Chunk], list[dict]]:
    """Parse every supplied PDF. Returns (chunks, manifest)."""
    corpus_dir = Path(corpus_dir)
    manifest: list[dict] = []
    merged: dict[str, Chunk] = {}
    pdfs = sorted(corpus_dir.glob("*.pdf"))
    if not pdfs:
        raise FileNotFoundError(f"no PDFs found in {corpus_dir}")
    for pdf in pdfs:
        rep = text_layer_report(pdf)
        if not rep["has_text_layer"]:
            raise NeedsOCRError(
                f"{pdf.name} has no usable text layer. Run: ocrmypdf --force-ocr '{pdf}' '{pdf}' "
                f"(requires the ocrmypdf package) and re-run ingestion.")
        name = pdf.name.lower()
        if "general_rules" in name or "interpretation" in name:
            kind, chunks = "gri", parse_gri(pdf)
        elif "itchs" in name or "notification" in name:
            kind, chunks = "itchs", parse_itchs(pdf)
        else:
            continue
        manifest.append({"file": pdf.name, "kind": kind, "sha256": sha256_file(pdf),
                         "bytes": pdf.stat().st_size, "pages": rep["pages"],
                         "has_text_layer": rep["has_text_layer"], "chunks": len(chunks)})
        for c in chunks:
            if c.id not in merged:
                merged[c.id] = c
    # cross-link: heading chunks get both notes lists now that all notes exist
    notes_by_section: dict[str, list[str]] = {}
    notes_by_chapter: dict[int, list[str]] = {}
    for c in merged.values():
        if c.type == "note":
            if c.metadata["scope"] == "section":
                notes_by_section.setdefault(c.metadata["section"], []).append(c.id)
            else:
                notes_by_chapter.setdefault(c.metadata["chapter"], []).append(c.id)
    for c in merged.values():
        if c.type == "heading":
            c.metadata["linked_notes"] = (sorted(notes_by_section.get(c.metadata["section"], []),
                                                 key=_note_sort) +
                                          sorted(notes_by_chapter.get(c.metadata["chapter"], []), key=_note_sort))
    return list(merged.values()), manifest


def _note_sort(cid: str) -> int:
    return int(cid.rsplit("-", 1)[1])
