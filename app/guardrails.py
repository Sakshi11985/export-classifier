"""Guardrails (L4).

1. Prompt-injection defence on EVERY free-text field that can reach a model (input), plus a
   second scan of tool outputs/retrieved passages before they go back into a model's context.
2. Toxicity detection on input and output (lexicon based; limitations in docs/LIMITATIONS).
3. Output filtering: the service may only *propose* a classification. Any text that claims a
   declaration was filed, a classification is legally binding/approved, or that the service
   itself makes the determination is blocked or rewritten. The output filter is also a
   schema gate: the route must be one of the three permitted routes.
4. Untrusted text is always wrapped in explicit delimiters when placed in a prompt."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .contracts import ROUTES
from .textutil import normalise

# --- injection -----------------------------------------------------------------------------
_INJECTION_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("override_instructions", re.compile(
        r"\b(ignore|disregard|forget|override|bypass|skip|discard)\b[^.\n]{0,40}\b(previous|prior|above|earlier|all|any|your|the|these|system)\b"
        r"[^.\n]{0,30}\b(instruction|instructions|prompt|prompts|rule|rules|guideline|guidelines|constraint|constraints|guardrail|guardrails|polic(?:y|ies))\b")),
    ("reveal_prompt", re.compile(
        r"\b(reveal|show|print|repeat|output|leak|display|tell me)\b[^.\n]{0,40}\b(system|hidden|initial|original|developer)\b[^.\n]{0,20}\b(prompt|instructions?|message)\b")),
    ("role_hijack", re.compile(
        r"\b(you are now|from now on you|act as|pretend (?:to be|you are)|roleplay as|new persona|developer mode|jailbreak|dan mode)\b")),
    ("fake_role_tags", re.compile(r"(<\|?\s*(system|assistant|im_start|im_end|endoftext)\s*\|?>|\[/?(inst|system)\]|^\s*(system|assistant)\s*:|###\s*(system|instruction))", re.M)),
    ("force_output", re.compile(
        r"\b(classify|classification|answer|respond|output|return|set|mark)\b[^.\n]{0,50}\b(as|to|with)\b[^.\n]{0,40}\b(clear[- ]for[- ]filing|heading\s*\d{4}|hs\s*code\s*\d+|regardless|no matter what)\b")),
    ("tool_abuse", re.compile(r"\b(call|invoke|execute|run)\b[^.\n]{0,30}\b(tool|function|command|shell|code|script|sql|curl|wget)\b")),
    ("exfiltrate", re.compile(r"\b(api[ _-]?key|secret|password|credential|token)s?\b[^.\n]{0,40}\b(send|post|print|reveal|show|leak|email)\b|\b(send|post|email|leak)\b[^.\n]{0,40}\b(api[ _-]?key|secret|password|credential)s?\b")),
    ("file_declaration", re.compile(r"\b(file|submit|lodge|approve|certify)\b[^.\n]{0,30}\b(declaration|bill of entry|shipping bill)\b")),
    ("encoded_payload", re.compile(r"(?:[A-Za-z0-9+/]{60,}={0,2})")),
]


_SPACED = re.compile(r"(?:\b\w[\s._\-]){3,}\w\b")


@dataclass
class GuardResult:
    allowed: bool
    categories: list[str] = field(default_factory=list)
    detail: str = ""


def scan_injection(text: str) -> GuardResult:
    t = normalise(text)
    # undo trivial obfuscation: "i g n o r e", "i.g.n.o.r.e" and leetspeak digits
    deobf = _SPACED.sub(lambda m: re.sub(r"[\s._\-]", "", m.group(0)), t)
    leet = t.translate(str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s"}))
    cats: list[str] = []
    for name, rx in _INJECTION_PATTERNS:
        if name == "encoded_payload":
            if rx.search(text):
                cats.append(name)
            continue
        if rx.search(t) or rx.search(leet) or rx.search(deobf):
            cats.append(name)
    return GuardResult(not cats, cats, "prompt-injection patterns: " + ", ".join(cats) if cats else "")


# --- toxicity ------------------------------------------------------------------------------
_TOXIC = [r"fuck\w*", r"shit\w*", r"bastard", r"bitch\w*", r"asshole", r"cunt", r"dickhead", r"motherfucker",
          r"kill (?:you|yourself|them)", r"i will (?:kill|hurt|destroy) you", r"you(?:'re| are) (?:an? )?(?:idiot|moron|stupid|useless)",
          r"\bidiots?\b", r"\bmorons?\b", r"go to hell", r"die in a fire", r"nazi", r"\bretard\w*"]
_TOXIC_RX = re.compile(r"\b(?:" + "|".join(_TOXIC) + r")", re.I)


def scan_toxicity(text: str) -> GuardResult:
    t = normalise(text).translate(str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "@": "a", "$": "s"}))
    m = _TOXIC_RX.search(t)
    return GuardResult(m is None, ["toxicity"] if m else [], "toxic language detected" if m else "")


# --- output filter ---------------------------------------------------------------------------
_FORBIDDEN_CLAIMS = [
    ("filed_declaration", re.compile(r"\b(i|we|the system|this service)\b[^.\n]{0,40}\b(have|has|had)?\s*(filed|submitted|lodged)\b[^.\n]{0,40}\b(declaration|bill of entry|shipping bill)\b|\bdeclaration (?:has been|was|is) (?:filed|submitted|lodged)\b")),
    ("binding_determination", re.compile(r"\b(legally binding|final classification|officially classified|certified (?:as|classification)|guaranteed (?:correct|classification|clearance)|customs (?:has )?approved)\b")),
    ("authority_claim", re.compile(r"\b(i|we) (?:hereby )?(?:determine|certify|rule|decide)\b|\bas (?:a|the) licensed customs broker\b|\bno (?:broker|human|review) (?:is )?(?:needed|required)\b")),
    ("duty_calculation", re.compile(r"\b(duty (?:payable|amount|rate) (?:is|of)|total duty|customs duty of \d)")),
]


def scan_output_text(text: str) -> GuardResult:
    t = normalise(text)
    cats = [name for name, rx in _FORBIDDEN_CLAIMS if rx.search(t)]
    tox = scan_toxicity(text)
    cats += tox.categories
    return GuardResult(not cats, cats, "output contains disallowed claims: " + ", ".join(cats) if cats else "")


def filter_result(result: dict) -> tuple[dict, list[str]]:
    """Final gate on the object returned to callers. Returns (result, violations).
    Never allows an unsupported route or a confident answer from a non-verified run."""
    violations: list[str] = []
    if result.get("route") not in ROUTES:
        violations.append("invalid_route")
        result["route"] = "specialist-classification-review"
    if result.get("route") == "clear-for-filing" and not result.get("verification", {}).get("passed"):
        violations.append("unverified_clear")
        result["route"] = "specialist-classification-review"
    if result.get("route") == "clear-for-filing" and result.get("degraded"):
        violations.append("degraded_clear")
        result["route"] = "specialist-classification-review"
    cls = result.get("classification") or {}
    if isinstance(cls.get("reasoning_summary"), str):
        g = scan_output_text(cls["reasoning_summary"])
        if not g.allowed:
            violations += g.categories
            cls["reasoning_summary"] = "[text withheld by the output filter]"
    basis = result.get("route_basis") or {}
    if isinstance(basis.get("explanation"), str):
        g = scan_output_text(basis["explanation"])
        if not g.allowed:
            violations += g.categories
            basis["explanation"] = "[text withheld by the output filter]"
    result["determination_authority"] = ("This is a proposed classification with supporting evidence. "
                                         "It is not a customs determination and no declaration has been filed; "
                                         "a licensed customs broker decides.")
    if violations:
        result.setdefault("guardrail_violations", []).extend(violations)
    return result, violations


# --- prompt-side helpers -------------------------------------------------------------------
UNTRUSTED_OPEN = "<untrusted_user_data>"
UNTRUSTED_CLOSE = "</untrusted_user_data>"


def wrap_untrusted(text: str) -> str:
    safe = (text or "").replace(UNTRUSTED_OPEN, "").replace(UNTRUSTED_CLOSE, "")
    return f"{UNTRUSTED_OPEN}\n{safe}\n{UNTRUSTED_CLOSE}"


def scan_request_fields(fields: dict) -> GuardResult:
    """Scan every free-text field of a validated request (L4: 'every free-text field')."""
    texts: list[tuple[str, str]] = []
    for k in ("product_description", "product_name", "intended_use", "essential_character_hint"):
        if fields.get(k):
            texts.append((k, str(fields[k])))
    for i, m in enumerate(fields.get("materials") or []):
        texts.append((f"materials[{i}]", str(m)))
    cats, details = [], []
    for name, txt in texts:
        r = scan_injection(txt)
        if not r.allowed:
            cats += r.categories
            details.append(f"{name}: {', '.join(r.categories)}")
        t = scan_toxicity(txt)
        if not t.allowed:
            cats += t.categories
            details.append(f"{name}: toxicity")
    return GuardResult(not cats, sorted(set(cats)), "; ".join(details))
