"""Deterministic offline reasoning policy.

THIS IS NOT A LANGUAGE MODEL. It implements the same chat interface as a real provider so the
whole agent graph (tools, loop bounds, verifier, router, tracing) runs and can be tested with no
API key and no network. It is also the last step of the defined fallback path (A5): when the
provider is unavailable, runs complete on this policy and are marked `degraded`, which forbids a
clear-for-filing answer.

What it does: it reads the transcript (tool results so far), decides which tool(s) to call next,
and when enough has been read it writes the proposal JSON. All tariff facts it uses come from
tool results (corpus text). It contains no tariff codes or rules of its own."""
from __future__ import annotations

import json
import re

from ..entities import IRON_STEEL, extract_entities
from ..textutil import squash

IN_SCOPE = (73, 96)


# ---------------------------------------------------------------------------- transcript facts
class Facts:
    def __init__(self, messages: list[dict]):
        self.fields: dict = {}
        self.strategy = "default"
        self.feedback: list = []
        self.searches: list[dict] = []          # {args, results:[passage]}
        self.article_results: dict[str, dict] = {}
        self.notes: dict[str, dict] = {}
        self.clauses: dict[str, dict] = {}
        self.done: set[str] = set()
        calls: dict[str, dict] = {}
        for m in messages:
            if m["role"] == "user" and "request" in _safe_json(m["content"]):
                j = _safe_json(m["content"])
                self.fields = j["request"]
                self.strategy = j.get("strategy", "default")
                self.feedback = j.get("verifier_feedback", [])
            if m["role"] == "assistant":
                for tc in m.get("tool_calls") or []:
                    f = tc["function"]
                    calls[tc["id"]] = {"name": f["name"], "args": _safe_json(f["arguments"])}
            if m["role"] == "tool":
                c = calls.get(m["tool_call_id"])
                if not c:
                    continue
                res = _safe_json(m["content"])
                key = c["name"] + ":" + json.dumps(c["args"], sort_keys=True)
                self.done.add(key)
                if c["name"] == "search_corpus":
                    self.searches.append({"args": c["args"], "results": res.get("results", [])})
                elif c["name"] == "search_headings_by_article":
                    self.article_results[c["args"].get("article", "")] = res
                elif c["name"] == "get_notes_for_heading":
                    self.notes[c["args"].get("heading", "")] = res
                elif c["name"] == "get_clause" and "id" in res:
                    self.clauses[res["id"]] = res

    def was_done(self, name: str, args: dict) -> bool:
        return (name + ":" + json.dumps(args, sort_keys=True)) in self.done

    def passages(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for s in self.searches:
            for p in s["results"]:
                out[p["id"]] = p
        for r in self.article_results.values():
            for c in r.get("candidates", []):
                if c.get("heading_passage"):
                    out[c["heading_passage"]["id"]] = c["heading_passage"]
        for n in self.notes.values():
            for p in n.get("exclusion_notes", []):
                out[p["id"]] = p
        out.update(self.clauses)
        return out


def _safe_json(s):
    if isinstance(s, dict):
        return s
    try:
        if isinstance(s, str):
            s = re.sub(r"</?untrusted_user_data>", "", s).strip()
        v = json.loads(s)
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


def _call(i: int, name: str, args: dict) -> dict:
    return {"id": f"call_{i}_{name}_{abs(hash(json.dumps(args, sort_keys=True))) % 10**6}", "name": name, "arguments": args}


# ---------------------------------------------------------------------------- helpers
def clause_with(text: str, needle: str, width: int = 260) -> str | None:
    """Verbatim sub-string of `text` (clause delimited by ';') that contains `needle`."""
    i = text.find(needle)
    if i < 0:
        return None
    a = text.rfind(";", 0, i)
    a = 0 if a < 0 else a + 1
    b = text.find(";", i)
    b = len(text) if b < 0 else b
    seg = text[a:b].strip()
    if len(seg) > width:
        lo = max(0, i - a - width // 2)
        seg = text[a + lo:a + lo + width]
    return seg.strip()


def first_sentence(text: str, limit: int = 220) -> str:
    seg = text.strip()
    m = re.search(r"\.\s", seg)
    cut = seg[: m.start() + 1] if m else seg
    if len(cut) > limit:
        cut = cut[:limit].rsplit(" ", 1)[0]
    return cut.strip()


def referenced(text: str) -> tuple[set[int], set[str]]:
    chapters = {int(x) for x in re.findall(r"Chapter (\d{1,2})", text)}
    heads = set(re.findall(r"headings? (\d{4})", text))
    return chapters, heads


def heading_desc(passage: dict) -> str:
    m = re.match(r"Heading \d{4} \(Chapter \d+\): (.*?)(?:\nTariff items:|$)", passage["text"], re.S)
    return (m.group(1).strip() if m else passage["text"][:160])


# ---------------------------------------------------------------------------- decision
def decide(facts: Facts, ctx_fields: dict) -> dict:
    """Pure function: facts gathered from tool results -> proposal dict (Proposal schema)."""
    desc = ctx_fields.get("product_description", "")
    ent = extract_entities(desc, ctx_fields)
    passages = facts.passages()
    materials = ent.materials
    base = {"article": ent.primary.text if ent.primary else "", "materials": materials, "heading": None,
            "gri_applied": None, "reasoning_summary": "", "citations": [], "alternatives": [],
            "needs_clarification": [], "out_of_scope": False, "tie": False, "confidence": "low"}

    if facts.strategy == "baseline_single_pass":
        return _decide_baseline(facts, base, passages)
    if facts.strategy == "material_first":
        return _decide_material_first(facts, base, passages, ent)

    # candidates that NAME each article (strong), per article; head-noun-only searches fold into the article
    per_article: dict[str, list[dict]] = {}
    for a in ent.articles:
        res = facts.article_results.get(a.text) or {}
        per_article[a.text] = [dict(c, article=a.text) for c in res.get("candidates", []) if c["strong"]]
    for art, res in facts.article_results.items():
        if art in per_article or not ent.articles:
            continue
        for c in res.get("candidates", []):
            if c["strong"]:
                per_article[ent.articles[0].text].append(dict(c, article=ent.articles[0].text))
    for k, lst in per_article.items():
        best: dict[str, dict] = {}
        for c in lst:
            if c["heading"] not in best or c["score"] > best[c["heading"]]["score"]:
                best[c["heading"]] = c
        per_article[k] = sorted(best.values(), key=lambda c: (-c["score"], c["heading"]))
    cands = sorted({c["heading"]: c for lst in per_article.values() for c in lst}.values(),
                   key=lambda c: (-c["score"], c["heading"]))

    # ---------------- no heading names the article
    if not cands:
        if not materials:
            base["needs_clarification"] = ["material of the product is not stated",
                                           "article type is not specific enough to match a heading"]
            base["reasoning_summary"] = ("No heading in Chapters 73 or 96 names this article and the material is not "
                                         "stated, so the classification cannot be decided from the description.")
            base["citations"] = _gri_citations(facts, ["GRI-1"])
            return base
        base["out_of_scope"] = True
        mats = ", ".join(materials)
        if set(materials) & IRON_STEEL:
            base["reasoning_summary"] = (f"The article is of {mats} but no heading in the indexed chapters names it. "
                                         "Only a residual heading could apply, which cannot be confirmed without "
                                         "checking other chapters; specialist review is required.")
        else:
            base["reasoning_summary"] = (f"No heading in Chapters 73 (articles of iron or steel) or 96 names this "
                                         f"article, and its material ({mats}) is outside Chapter 73. "
                                         "The product appears to fall outside the indexed chapters.")
        base["citations"] = _gri_citations(facts, ["GRI-1"]) + _chapter_citation(facts, passages)
        return base

    def apply_notes(cs: list[dict]) -> tuple[list[dict], list[dict]]:
        """GRI 1: notes override headings. A heading is excluded only when a retrieved exclusion note sends
        goods of the chapter/heading that names the SAME article elsewhere."""
        excl, alts = set(), []
        for c in cs:
            for other in cs:
                if other["heading"] == c["heading"] or other["chapter"] == c["chapter"]:
                    continue
                note = _find_exclusion(facts, c["heading"], [other])
                if note:
                    excl.add(c["heading"])
                    alts.append({"heading": c["heading"], "status": "excluded_by_note",
                                 "basis": f"{note['note_id']} excludes goods of Chapter {other['chapter']}",
                                 "note_id": note["note_id"], "_quote": note["quote"], "_by": note["by"]})
                    break
        return [c for c in cs if c["heading"] not in excl], alts

    # ---------------- sets / composite goods: GRI 3
    if ent.is_set:
        winners, alts_all = [], []
        for a in ent.articles:
            rem, alts = apply_notes(per_article.get(a.text, []))
            alts_all += alts
            if rem:
                winners.append(rem[0])
        hs = list(dict.fromkeys(w["heading"] for w in winners))
        if len(hs) >= 2:
            hint = (ctx_fields.get("essential_character_hint") or "").strip()
            base.update({"tie": True, "heading": None, "gri_applied": "GRI-3(a)", "confidence": "low"})
            base["alternatives"] = [{"heading": h, "status": "tied",
                                     "basis": "each heading names only part of the items in the set (GRI 3(a), last sentence)"}
                                    for h in hs]
            base["reasoning_summary"] = (
                f"The goods are a set / composite of articles named in different headings ({', '.join(hs)}). Under "
                "GRI 3(a) headings that each refer to only part of a set are equally specific. "
                + ("An essential-character hint was supplied but it must be confirmed by a classifier. " if hint else
                   "The request does not establish which component gives the set its essential character (GRI 3(b)), "
                   "so the headings remain equally supported and the system does not choose between them. "))
            base["citations"] = _gri_citations(facts, ["GRI-1", "GRI-3(a)", "GRI-3(b)"]) + \
                [c for h in hs for c in _heading_citation(facts, passages, h)]
            return base
        # one component names a heading, the other(s) do not: cannot be settled automatically
        cands = [c for c in cands if winners and c["heading"] == winners[0]["heading"]] or cands
        base["confidence"] = "low"

    # ---------------- single article: terms of the headings (capacity limits), then notes, then choose
    pool = per_article.get(ent.articles[0].text, []) if ent.articles else cands
    pool = pool or cands
    cap = ctx_fields.get("capacity_litres")
    if cap is None:
        mcap = re.search(r"(\d+(?:\.\d+)?)\s*(?:l|lit(?:re|er)s?)\b", desc.lower())
        cap = float(mcap.group(1)) if mcap else None
    cap_alts: list[dict] = []
    if cap is not None:
        keep = []
        for c in pool:
            m = re.search(r"capacity (not )?exceeding ([\d.]+) ?l\b", c.get("heading_description", ""))
            if m and not ((cap <= float(m.group(2))) if m.group(1) else (cap > float(m.group(2)))):
                cap_alts.append({"heading": c["heading"], "status": "not_applicable",
                                 "basis": f"capacity: heading text says 'capacity {'not ' if m.group(1) else ''}exceeding "
                                          f"{m.group(2)} l'; the request has {cap:g} l"})
            else:
                keep.append(c)
        pool = keep or pool
        if not keep:
            cap_alts = []
    remaining, alternatives = apply_notes(pool)
    alternatives = cap_alts + alternatives
    # material-based competitors that surfaced in broad searches, removed by the same notes
    for comp in _other_heading_hits(facts, exclude={c["heading"] for c in pool}):
        note = _find_exclusion(facts, comp["heading"], [c for c in remaining if c["chapter"] != comp["chapter"]])
        if note:
            alternatives.append({"heading": comp["heading"], "status": "excluded_by_note",
                                 "basis": f"{note['note_id']} excludes goods of the chapter that names the article",
                                 "note_id": note["note_id"], "_quote": note["quote"], "_by": note["by"]})
    if not remaining:
        base["reasoning_summary"] = "Every heading that names the article is excluded by a note; specialist review required."
        base["out_of_scope"] = True
        return base
    top = remaining[0]
    runner = remaining[1] if len(remaining) > 1 else None
    if runner and (top["score"] - runner["score"]) < 0.05:
        hs = [top["heading"], runner["heading"]]
        if cap is None and all(re.search(r"capacity (not )?exceeding", c.get("heading_description", "")) for c in (top, runner)):
            base["needs_clarification"] = [f"capacity in litres is needed to choose between headings {hs[0]} and {hs[1]}, "
                                           "whose texts differ by capacity"]
            base["reasoning_summary"] = (f"Headings {hs[0]} and {hs[1]} both name the article and differ only by the "
                                         "capacity stated in their text; the request does not give a capacity.")
            base["citations"] = _gri_citations(facts, ["GRI-1"]) + \
                [c for h in hs for c in _heading_citation(facts, passages, h)]
            return base
        base.update({"tie": True, "gri_applied": "GRI-3(a)", "confidence": "low"})
        base["alternatives"] = [{"heading": h, "status": "tied", "basis": "equal support after GRI 3(a)"} for h in hs]
        base["reasoning_summary"] = (f"Headings {hs[0]} and {hs[1]} are equally supported after applying GRI 3(a); "
                                     "the system does not choose between them.")
        base["citations"] = _gri_citations(facts, ["GRI-1", "GRI-3(a)"]) + \
            [c for h in hs for c in _heading_citation(facts, passages, h)]
        return base
    gri = "GRI-1" if not runner else "GRI-3(a)"
    for r in remaining[1:]:
        alternatives.append({"heading": r["heading"], "status": "less_specific",
                             "basis": "names the article less completely than the chosen heading"})
    alternatives = alternatives[:5]
    cits = _gri_citations(facts, ["GRI-1"] + (["GRI-3(a)"] if runner else [])) + _heading_citation(facts, passages, top["heading"])
    seen_notes = set()
    for alt in alternatives:
        if alt.get("note_id") and alt["note_id"] not in seen_notes and alt.get("_quote"):
            cits.append({"id": alt["note_id"], "quote": alt["_quote"]})
            seen_notes.add(alt["note_id"])
    for alt in alternatives:
        alt.pop("_quote", None)
        alt.pop("_by", None)
    conf = "high" if not runner else "medium"
    if base["confidence"] == "low" and ent.is_set:
        conf = "low"
    base.update({
        "heading": top["heading"], "gri_applied": gri, "alternatives": alternatives,
        "citations": cits, "confidence": conf,
        "article": top.get("article", base["article"]),
        "reasoning_summary": (
            f"Under GRI 1 the terms of heading {top['heading']} ({top['heading_description'].rstrip('.')}) name the "
            f"article, so the heading is applied before any classification by material."
            + (" Competing material-based headings are removed by the section/chapter notes cited."
               if any(a['status'] == 'excluded_by_note' for a in alternatives) else "")
            + (f" Several headings remained, so GRI 3(a) (most specific description) selects {top['heading']}."
               if runner else "")),
    })
    return base


def _other_heading_hits(facts: Facts, exclude: set[str]) -> list[dict]:
    out, seen = [], set(exclude)
    for s in facts.searches:
        for p in s["results"]:
            if p["type"] == "heading":
                h = p["id"].split("-")[1]
                if h in seen:
                    continue
                seen.add(h)
                out.append({"heading": h, "chapter": p.get("chapter")})
    return out[:4]


def _find_exclusion(facts: Facts, heading: str, named_elsewhere: list[dict]) -> dict | None:
    """Does a retrieved exclusion note for `heading` send goods of the chapter / heading that names the
    article somewhere else? Returns {note_id, quote, by} using only note text that was retrieved."""
    n = facts.notes.get(heading)
    if not n:
        return None
    for note in n.get("exclusion_notes", []):
        chapters, heads = referenced(note["text"])
        for other in named_elsewhere:
            if other["chapter"] in chapters or other["heading"] in heads:
                needle = f"Chapter {other['chapter']}" if other["chapter"] in chapters else f"heading {other['heading']}"
                q = clause_with(note["text"], needle)
                if q:
                    return {"note_id": note["id"], "quote": q, "by": other["heading"]}
    return None


def _gri_citations(facts: Facts, ids: list[str]) -> list[dict]:
    out = []
    for i in ids:
        p = facts.clauses.get(i)
        if p:
            out.append({"id": i, "quote": first_sentence(p["text"])})
    return out


def _heading_citation(facts: Facts, passages: dict, h: str) -> list[dict]:
    p = passages.get(f"HEADING-{h}")
    if not p:
        return []
    return [{"id": p["id"], "quote": heading_desc(p)}]


def _chapter_citation(facts: Facts, passages: dict) -> list[dict]:
    p = passages.get("CHAPTER-73")
    return [{"id": p["id"], "quote": p["text"][:60]}] if p else []


def _decide_baseline(facts: Facts, base: dict, passages: dict) -> dict:
    """Single retrieval pass: take the top heading returned for the raw description (spec s.5 baseline)."""
    for s in facts.searches:
        for p in s["results"]:
            if p["type"] == "heading":
                h = p["id"].split("-")[1]
                base.update({"heading": h, "gri_applied": "GRI-1", "confidence": "medium",
                             "reasoning_summary": f"Top retrieved heading for the description is {h}.",
                             "citations": [{"id": p["id"], "quote": heading_desc(p)}]})
                return base
    base["out_of_scope"] = True
    base["reasoning_summary"] = "Nothing relevant retrieved."
    return base


def _decide_material_first(facts: Facts, base: dict, passages: dict, ent) -> dict:
    """Fault-injection strategy: classifies by material only (the failure described in spec s.4.1)."""
    for s in facts.searches:
        for p in s["results"]:
            if p["type"] == "heading" and p.get("chapter") == 73:
                h = p["id"].split("-")[1]
                cits = _gri_citations(facts, ["GRI-1"]) + [{"id": p["id"], "quote": heading_desc(p)}]
                base.update({"heading": h, "gri_applied": "GRI-1", "confidence": "high", "citations": cits,
                             "reasoning_summary": f"Material is steel, so Chapter 73 heading {h} applies."})
                return base
    return _decide_baseline(facts, base, passages)


# ---------------------------------------------------------------------------- planning
class OfflinePolicy:
    def respond(self, role: str, messages: list[dict], tools, ctx) -> dict:
        if role == "verifier":
            return {"content": json.dumps({"supported": True, "issues": [], "judge": "offline-policy"})}
        facts = Facts(messages)
        calls = self._plan(facts, ctx)
        if calls:
            return {"content": None, "tool_calls": calls}
        proposal = decide(facts, facts.fields)
        return {"content": json.dumps(proposal)}

    # each call to _plan returns the next batch of independent tool calls, or [] when ready to answer
    def _plan(self, f: Facts, ctx) -> list[dict]:
        k = len(f.done)
        desc = f.fields.get("product_description", "")
        ent = extract_entities(desc, f.fields)
        strat = f.strategy

        if strat == "baseline_single_pass":
            q = {"query": desc[:300], "top_k": 5}
            return [] if f.was_done("search_corpus", q) else [_call(k, "search_corpus", q)]

        if strat == "material_first":
            mats = " ".join(ent.materials) or "steel"
            q = {"query": mats, "top_k": 5, "types": ["heading"], "chapter": 73}
            if not f.was_done("get_clause", {"id": "GRI-1"}):
                return [_call(k, "search_corpus", q), _call(k + 1, "get_clause", {"id": "GRI-1"})] \
                    if not f.was_done("search_corpus", q) else [_call(k, "get_clause", {"id": "GRI-1"})]
            return []

        # ---- turn 1: read the request
        if not f.was_done("get_request_fields", {}):
            return [_call(k, "get_request_fields", {})]
        desc_q = {"query": desc[:300], "top_k": 5}
        arts = [a.text for a in ent.articles]
        heads = {a.tokens[-1] for a in ent.articles if len(a.tokens) > 1} if strat in ("article_first", "exhaustive_notes") else set()

        # ---- turn 2: provision that appears to apply + provisions that NAME the article
        turn2 = []
        if not f.was_done("search_corpus", desc_q):
            turn2.append(("search_corpus", desc_q))
        for a in arts + sorted(heads):
            if not f.was_done("search_headings_by_article", {"article": a}):
                turn2.append(("search_headings_by_article", {"article": a}))
        if turn2:
            return [_call(k + i, n, a) for i, (n, a) in enumerate(turn2)]

        # ---- turn 3: do any notes modify those provisions?
        cand_heads: list[str] = []
        for a in arts + sorted(heads):
            for c in (f.article_results.get(a) or {}).get("candidates", []):
                if c["strong"] and c["heading"] not in cand_heads:
                    cand_heads.append(c["heading"])
        for h in _other_heading_hits(f, set())[: (4 if strat in ("article_first", "exhaustive_notes") else 2)]:
            if h["heading"] not in cand_heads:
                cand_heads.append(h["heading"])
        turn3 = [("get_notes_for_heading", {"heading": h}) for h in cand_heads[:5]
                 if not f.was_done("get_notes_for_heading", {"heading": h})]
        if turn3:
            return [_call(k + i, n, a) for i, (n, a) in enumerate(turn3)]

        # ---- turn 4: search again using what was read (chapters named by exclusion notes) + material competitor
        turn4 = []
        for h, nres in f.notes.items():
            h_ch = int(f.passages().get(f"HEADING-{h}", {}).get("chapter") or 0) or None
            for note in nres.get("exclusion_notes", []):
                chapters, _ = referenced(note["text"])
                for ch in sorted(chapters & set(IN_SCOPE)):
                    if ch != h_ch and arts:
                        q = {"query": arts[0], "top_k": 4, "chapter": ch}
                        if not f.was_done("search_corpus", q) and ("search_corpus", q) not in turn4:
                            turn4.append(("search_corpus", q))
        if set(ent.materials) & IRON_STEEL and arts:
            q = {"query": f"{' '.join(ent.materials[:1])} {arts[0]}", "top_k": 3, "types": ["heading"], "chapter": 73}
            if not f.was_done("search_corpus", q):
                turn4.append(("search_corpus", q))
        if turn4:
            return [_call(k + i, n, a) for i, (n, a) in enumerate(turn4)]
        # notes for any new competitor heading discovered in turn 4
        turn4b = []
        for h in [x["heading"] for x in _other_heading_hits(f, set())][:6]:
            if f.passages().get(f"HEADING-{h}", {}).get("chapter") == 73 and not f.was_done("get_notes_for_heading", {"heading": h}) \
                    and len(turn4b) < 1:
                turn4b.append(("get_notes_for_heading", {"heading": h}))
        if turn4b:
            return [_call(k + i, n, a) for i, (n, a) in enumerate(turn4b)]

        # ---- turn 5: the rules
        need = ["GRI-1"]
        strong_heads = {c["heading"] for a in arts for c in (f.article_results.get(a) or {}).get("candidates", []) if c["strong"]}
        if ent.is_set or len(strong_heads) > 1:
            need += ["GRI-3(a)", "GRI-3(b)"]
        turn5 = [("get_clause", {"id": g}) for g in need if not f.was_done("get_clause", {"id": g})]
        if turn5:
            return [_call(k + i, n, a) for i, (n, a) in enumerate(turn5)]
        return []
