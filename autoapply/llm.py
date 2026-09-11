"""Local LLM semantic matching for Easy Apply questions.

When a required question matches no answers.yaml key by substring, this layer
asks the local LLM (Ollama) to map it to a known answer key. Every suggestion
passes a deterministic verifier before anything is filled or memorized:

  * the mapped key must exist in answers.yaml with a non-empty value
    (hallucinated keys/values resolve to NONE);
  * confidence must clear `min_confidence`;
  * direction guard: expected-pay questions never resolve to current/past-pay
    keys and vice versa (`never_mix` in llm.yaml);
  * numeric questions only accept numeric answers.

Any failure -> the question stays unknown and flows to Telegram as before.
LLM unreachable/slow -> fail open to the Telegram flow, never fail the run.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.request

log = logging.getLogger("aa.llm")

_PUNCT = re.compile(r"[:*()\[\]/\\]+")
_SPACE = re.compile(r"\s+")

_EXPECTED_MARKERS = ("expect", "desir", "want", "seek")
_PAST_MARKERS = ("current", "previous", "past", "last", "drawn", "present", "existing")


def norm(text: str) -> str:
    return _SPACE.sub(" ", _PUNCT.sub(" ", (text or "").lower())).strip()


def llm_available(cfg) -> bool:
    """True when the Ollama host answers quickly. Never raises."""
    if not cfg.enabled:
        return False
    try:
        req = urllib.request.Request(f"{cfg.host.rstrip('/')}/api/tags")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return bool(json.loads(resp.read() or b"{}"))
    except Exception as exc:
        log.info("llm unavailable (%s); falling back to Telegram flow", exc)
        return False


def _chat(cfg, prompt: str) -> str | None:
    """One strict-JSON chat call. Returns raw content or None on any failure."""
    payload = json.dumps(
        {
            "model": cfg.model,
            "stream": False,
            "format": "json",
            "options": {"temperature": 0},
            "messages": [{"role": "user", "content": prompt}],
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        f"{cfg.host.rstrip('/')}/api/chat",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=cfg.timeout_seconds) as resp:
            body = json.loads(resp.read() or b"{}")
        return (body.get("message") or {}).get("content") or None
    except Exception as exc:
        log.info("llm chat failed (%s); falling back to Telegram flow", exc)
        return None


def _build_prompt(questions: list[str], answers: dict) -> str:
    lines = [
        "You match job-application QUESTIONS to known ANSWER KEYS.",
        'Reply with ONLY a JSON object shaped exactly like the example: {"matches": [{"question": "<exact question text>", "key": "<exact key text or NONE>", "confidence": <0-100>}]}',
        "",
        "Rules:",
        "- The key MUST be copied character-for-character from the KEYS list below. Never invent, rephrase, or derive a key. If no listed key holds the same fact, use NONE.",
        "- previous / past / last-drawn salary is the SAME fact as current CTC. Map those to the current-salary key.",
        "- expected salary/CTC (what the candidate wants) is DIFFERENT from current/last/previous salary. Never map between expected and current/past.",
        "- Only map when the question asks for the same fact the key holds. Else NONE.",
        "- Confidence below 75 means NONE.",
        "",
        "KEYS:",
    ]
    for key, value in answers.items():
        if (value or "").strip():
            lines.append(f"- {key} = {value}")
    lines.append("")
    lines.append("QUESTIONS:")
    for i, q in enumerate(questions, 1):
        lines.append(f"{i}. {q}")
    return "\n".join(lines)


def _parse_matches(raw: str | None, questions: list[str]) -> list[dict]:
    """Parse the model reply into [{question, key, confidence}], tolerating drift."""
    if not raw:
        return []
    try:
        body = json.loads(raw)
    except Exception:
        return []
    items = body.get("matches") if isinstance(body, dict) else body
    if not isinstance(items, list):
        return []
    out = []
    for item in items:
        if not isinstance(item, dict):
            continue
        qtext = str(item.get("question") or "")
        # Bind the entry back to one of OUR questions (exact, else normalized).
        target = next((q for q in questions if q == qtext), None)
        if target is None:
            nq = norm(qtext)
            target = next((q for q in questions if nq and nq == norm(q)), None)
        if target is None:
            continue
        try:
            conf = int(item.get("confidence", 0))
        except (TypeError, ValueError):
            conf = 0
        out.append({"question": target, "key": str(item.get("key") or ""), "confidence": conf})
    return out


def _resolve_key(raw_key: str, answers: dict) -> str | None:
    """Map the model's key field to a real answers key.

    Accepts the exact key text (case-insensitive) or, when the model echoed
    the VALUE instead of the key, the key holding that value. Else None.
    """
    nk = norm(raw_key)
    if not nk or nk == "none":
        return None
    for key in answers:
        if norm(key) == nk:
            return key
    for key, value in answers.items():
        if norm(value or "") and norm(value or "") == nk:
            return key
    return None


def _side(text: str) -> str | None:
    """Classify text as expected-pay vs current/past-pay. None if unclear."""
    n = norm(text)
    if any(m in n for m in _EXPECTED_MARKERS):
        return "expected"
    if any(m in n for m in _PAST_MARKERS):
        return "past"
    return None


def _direction_ok(question: str, key: str, never_mix: list) -> bool:
    """Reject expected<->past pay conflations regardless of model confidence."""
    if not never_mix:
        return True
    q_side, k_side = _side(question), _side(key)
    if q_side is None or k_side is None or q_side == k_side:
        return True
    for group in never_mix:
        norms = {norm(g) for g in group}
        if norm(key) not in norms:
            continue
        mate_sides = {_side(g) for g in group if norm(g) != norm(key)}
        if q_side in mate_sides:
            log.info("llm: blocked direction mix %r -> %r", question, key)
            return False
    return True


_NUMERIC_HINTS = (
    "salary", "ctc", "pay", "rate", "lakh", "inr", "$", "year", "month",
    "experience", "rating", "rate", "scale", "notice", "days", "many", "much",
)


def _type_compatible(question: str, value: str) -> bool:
    """Numeric-looking questions only accept numeric answers."""
    if not (value or "").strip():
        return False
    nq = norm(question)
    if any(h in nq for h in _NUMERIC_HINTS):
        return bool(re.search(r"\d", value))
    return True


def _verify(question: str, key: str, confidence: int, answers: dict, cfg) -> bool:
    if confidence < cfg.min_confidence:
        return False
    value = answers.get(key, "")
    if not (value or "").strip():
        return False
    if not _direction_ok(question, key, cfg.never_mix):
        return False
    return _type_compatible(question, value)


def apply_equivalences(question: str, cfg, answers: dict) -> str | None:
    """Deterministic pre-pass: curated question patterns map straight to a key."""
    nq = norm(question)
    for eq in cfg.equivalences or []:
        if not eq.key or eq.key not in answers:
            continue
        if not (answers.get(eq.key) or "").strip():
            continue
        if any(norm(p) and norm(p) in nq for p in eq.any_of) and _direction_ok(
            question, eq.key, cfg.never_mix
        ) and _type_compatible(question, answers[eq.key]):
            return eq.key
    return None


def semantic_prefill(
    questions: list[str], answers: dict, cfg
) -> dict[str, dict]:
    """Map unknown questions to known answer keys.

    Returns {question: {"key", "value", "confidence", "source"}} for verified
    matches only (`source` is "rule" or "llm"). Anything unverifiable is left
    out so it still flows to Telegram. Never raises.
    """
    out: dict[str, dict] = {}
    if not cfg.enabled or not questions:
        return out
    pending = []
    for q in questions:
        key = apply_equivalences(q, cfg, answers)
        if key:
            out[q] = {
                "key": key,
                "value": answers[key],
                "confidence": 100,
                "source": "rule",
            }
        else:
            pending.append(q)
    if pending and llm_available(cfg):
        raw = _chat(cfg, _build_prompt(pending, answers))
        for item in _parse_matches(raw, pending):
            key = _resolve_key(item["key"], answers)
            if key and _verify(item["question"], key, item["confidence"], answers, cfg):
                out[item["question"]] = {
                    "key": key,
                    "value": answers[key],
                    "confidence": item["confidence"],
                    "source": "llm",
                }
            else:
                log.info("llm: no verified match for %r", item["question"][:60])
    return out
