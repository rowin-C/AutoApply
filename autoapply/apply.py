"""Step 2 — LinkedIn Easy Apply engine (conservative).

Policy: never guess. LinkedIn auto-fills what it can from your profile and
resume. For each required question the form still asks, the engine only
answers when the question label matches a key in config/answers.yaml. If any
required field can't be answered confidently, the application is abandoned
and the listing is marked `needs_manual` with the offending field labels —
the daily Telegram report surfaces those for you to handle.

── Safety rails ──────────────────────────────────────────────────────────────
  * No free-text guessing; unknown field -> skip, tell the human.
  * Daily apply budget from caps.yaml; only successful submits spend it.
  * Resume upload: only used when the form shows an empty "add a resume"
    state AND a resume path exists in profile.yaml `resume_files`.
  * `--dry-run` opens the flow, fills answers, and reports what it WOULD do
    without ever clicking Submit or recording anything.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field

from sqlmodel import select

from .db import (
    ApplyPath,
    Listing,
    ListingStatus,
    _today_iso,
    get_session,
    register_spend,
    remaining_budget,
)
from .errors import AccountBlocked, CaptchaDetected, LoginRequired
from .settings import Settings

log = logging.getLogger("aa.apply")

MAX_STEPS = 9

# Locate the Easy Apply modal whatever it is: role=dialog, the legacy class,
# or LinkedIn's current anonymous divs whose text opens with "Apply to <company>"
# and carries the "1/N pages" step marker. Fixed/portal containers have
# offsetParent === null, so visibility is judged by layout box.
_MODAL_PRO = r"""
  const shown = (e) => {
    if (!e) return false;
    const s = getComputedStyle(e);
    if (s.display === 'none' || s.visibility === 'hidden') return false;
    const r = e.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };
  const modal = [...document.querySelectorAll('[role="dialog"], .jobs-easy-apply-modal, div, aside')]
    .find((e) => shown(e) && /^apply to/i.test((e.innerText || '').trim())
      && (e.getAttribute('role') === 'dialog' || /pages/.test((e.innerText || '').slice(0, 140))));
"""

def _apply_js(js: str) -> str:
    return js.replace("@@MODAL@@", _MODAL_PRO)


@dataclass
class ApplyReport:
    applied: int = 0
    unknown: int = 0
    guarded: int = 0
    errors: int = 0
    budget_left: int = 0
    dry_run: bool = False
    unknown_fields: list[tuple[str, list[str]]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    applied_detail: list[dict] = field(default_factory=list)
    # {company, title, filled: [{label, value, source}]} for the audit summary


# Resolve every required-but-empty field in the open Easy Apply modal.
# Returns the labels it couldn't answer (should be empty for a submit).
_RESOLVE_JS = r"""
(args) => {
@@MODAL@@
  if (!modal) return { ok: false, unknown: [] };
  const answers = args.answers || {};
  const norm = (s) => (s || '').toLowerCase().replace(/[:*()[\]]/g, ' ').replace(/\s+/g, ' ').trim();
  const answerFor = (label) => {
    const n = norm(label);
    for (const k of Object.keys(answers)) {
      const kn = norm(k);
      if (kn.length >= 3 && n.includes(kn) && (answers[k] || '').trim() !== '') return { v: answers[k], k: k };
    }
    return null;
  };
  const labelOf = (el) => {
    if (el.labels && el.labels[0]) return el.labels[0].innerText.trim();
    const wrap = el.closest('.fb-dash-form-element-wrapper, .jobs-easy-apply-form-element, .artdeco-form-element-fill, [data-test-form-element]');
    if (wrap) {
      const lab = wrap.querySelector('label');
      if (lab) return lab.innerText.trim();
      const spans = wrap.querySelectorAll('span');
      for (const sp of spans) { const t = sp.innerText.trim(); if (t) return t; }
    }
    if (el.placeholder) return el.placeholder.trim();
    if (el.name) return el.name.replace(/[_-]+/g, ' ');
    return el.ariaLabel || '';
  };
  const isRequired = (el) => !!el.required
    || (el.closest('fieldset, [role="group"]') || {}).getAttribute === 'true';
  const setVal = (el, v) => {
    const proto = el.tagName === 'TEXTAREA' ? window.HTMLTextAreaElement.prototype
      : el.tagName === 'SELECT' ? window.HTMLSelectElement.prototype
      : window.HTMLInputElement.prototype;
    const desc = Object.getOwnPropertyDescriptor(proto, 'value');
    if (desc && desc.set) desc.set.call(el, v); else el.value = v;
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
  };
  const unknown = [];
  const filled = [];

  for (const el of modal.querySelectorAll('input:not([type]), input[type="text"], input[type="number"], input[type="email"], input[type="url"], input[type="tel"], input[type="date"], textarea')) {
    if (isRequired(el) && (el.value || '').trim() === '') {
      const label = labelOf(el);
      const hit = answerFor(label);
      if (hit === null) { unknown.push(label || 'untitled field'); continue; }
      setVal(el, hit.v);
      filled.push({ label: label, key: hit.k, value: hit.v });
    }
  }

  for (const el of modal.querySelectorAll('select')) {
    if (el.required && (el.value || '').trim() === '') {
      const label = labelOf(el);
      const hit = answerFor(label);
      if (hit === null) { unknown.push(label || 'untitled select'); continue; }
      const rn = norm(hit.v);
      const leadNum = (s) => { const m = (s || '').match(/^\s*(\d+)/); return m ? parseInt(m[1], 10) : null; };
      const opt = [...el.options].find((o) => {
        const t = norm(o.text);
        if (t === rn) return true;
        const rnN = leadNum(rn); const tN = leadNum(t);
        if (rnN !== null && tN !== null && rnN === tN) return true;
        return rnN === null && rn.length >= 3 && t.includes(rn);
      });
      if (opt) { el.value = opt.value; el.dispatchEvent(new Event('change', { bubbles: true })); filled.push({ label: label, key: hit.k, value: hit.v }); }
      else unknown.push(label + ': option not found (' + hit.v + ')');
    }
  }

  const groups = {};
  for (const el of modal.querySelectorAll('input[type="radio"]')) {
    const k = el.name || 'g' + (el.id || Math.random());
    if (!groups[k]) groups[k] = { els: [], checked: false };
    groups[k].els.push(el);
    if (el.checked) groups[k].checked = true;
  }
  for (const g of Object.values(groups)) {
    if (g.checked) continue;
    const first = g.els[0];
    const req = first.required || g.els.some((r) => r.required) || isRequired(first);
    if (!req) continue;
    const label = labelOf(first);
    const hit = answerFor(label);
    if (hit === null) { unknown.push(label || 'untitled radio'); continue; }
    const rn = norm(hit.v);
    const opt = g.els.find((r) => { const t = norm(labelOf(r)); return t === rn || (rn.length >= 3 && t.includes(rn)); });
    if (opt) { opt.click(); filled.push({ label: label, key: hit.k, value: hit.v }); }
    else unknown.push(label + ': option not found (' + hit.v + ')');
  }

  for (const el of modal.querySelectorAll('input[type="checkbox"]')) {
    if (el.checked) continue;
    const req = el.required || isRequired(el);
    if (!req) continue;
    const label = labelOf(el);
    // Consent/authorization checkboxes are typically required with no
    // informative label; we can't safely confirm intent, so stay conservative.
    if (!label) { unknown.push('untitled required checkbox'); continue; }
    const hit = answerFor(label);
    if (hit === null) { unknown.push(label || 'untitled required checkbox'); continue; }
    const yes = /^(yes|true|agree|accept|consent)$/i.test(norm(hit.v));
    if (yes) { el.click(); filled.push({ label: label, key: hit.k, value: hit.v }); }
    else unknown.push(label + ': checkbox value not understood (' + hit.v + ')');
  }

  return { ok: true, unknown: unknown, filled: filled };
}
"""

# Find the primary Next/Review/Submit application button inside the modal.
_FORWARD_JS = r"""
() => {
@@MODAL@@
  if (!modal) return null;
  const texts = ['submit application', 'submit', 'review', 'done', 'next'];
  const score = { 'submit application': 5, 'submit': 4, 'review': 3, 'done': 2, 'next': 1 };
  let best = null; let bestScore = -1;
  for (const btn of modal.querySelectorAll('button')) {
    if (btn.disabled || btn.offsetParent === null) continue;
    const t = (btn.innerText || '').trim().toLowerCase();
    if (score[t] !== undefined && score[t] > bestScore) { best = btn; bestScore = score[t]; }
  }
  return best ? { text: best.innerText.trim(), score: bestScore } : null;
}
"""

_SUBMIT_JS = r"""
() => {
@@MODAL@@
  const b = [...modal.querySelectorAll('button')].find((x) => x.offsetParent !== null
    && /submit application/i.test((x.innerText || '').trim()));
  if (b) b.click();
  return !!b;
}
"""

_NEXT_JS = r"""
() => {
@@MODAL@@
  const b = [...modal.querySelectorAll('button')]
    .filter((x) => x.offsetParent !== null && ['next', 'done'].includes((x.innerText || '').trim().toLowerCase()))
    .sort((a, b) => b.innerText.length - a.innerText.length)[0];
  if (b) b.click();
  return !!b;
}
"""


def _click_easy_apply(page) -> bool:
    """Click the Easy Apply / Apply-on-LinkedIn button on the detail page."""
    try:
        clicked = page.evaluate(
            r"""() => {
              const btns = [...document.querySelectorAll('button')].filter((b) => b.offsetParent !== null);
              const target = btns.find((b) => {
                const t = ((b.getAttribute('aria-label') || '') + ' ' + (b.innerText || '')).toLowerCase();
                return t.includes('easy apply');
              });
              if (!target) return false;
              target.click();
              return true;
            }"""
        )
        return bool(clicked)
    except Exception as exc:
        log.warning("click easy-apply failed: %s", exc)
    return False


def _modal_visible(page) -> bool:
    """True when the Easy Apply flow modal is on screen."""
    try:
        return bool(
            page.evaluate(
                r"""() => {
                  const shown = (e) => {
                    if (!e) return false;
                    const s = getComputedStyle(e);
                    if (s.display === 'none' || s.visibility === 'hidden') return false;
                    const r = e.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                  };
                  return [...document.querySelectorAll('[role="dialog"], .jobs-easy-apply-modal, div, aside')]
                    .some((e) => shown(e) && /^apply to/i.test((e.innerText || '').trim())
                      && (e.getAttribute('role') === 'dialog' || /pages/.test((e.innerText || '').slice(0, 140))));
                }"""
            )
        )
    except Exception:
        return False


def _forward_action(page) -> str | None:
    try:
        res = page.evaluate(_apply_js(_FORWARD_JS))
    except Exception as exc:
        log.warning("forward scan failed: %s", exc)
        return None
    if not res:
        return None
    return "submit" if res.get("score", 0) >= 4 else "next"


_FWD_TEXTS = (  # descending score = preferred order
    "submit application",
    "submit",
    "review",
    "done",
    "next",
)


def _forward_button_text(page) -> str | None:
    """Return the label (as written on the page) of the strongest forward button."""
    try:
        res = page.evaluate(_apply_js(_FORWARD_JS))
    except Exception as exc:
        log.warning("forward scan failed: %s", exc)
        return None
    return (res or {}).get("text")


def _click_forward_trusted(page) -> bool:
    """Click the strongest footer forward button with a trusted (Playwright) click.

    LinkedIn's Easy Apply renders React buttons where the JS `.click()` event is
    ignored; Playwright's real InputEvents path is required for Next/Review/Submit.
    """
    text = _forward_button_text(page)
    if not text:
        return False
    try:
        btn = page.locator(
            f"footer button:has-text('{text}'), [role='dialog'] footer button:has-text('{text}')"
        ).last
        btn.click(timeout=5000)
        return True
    except Exception as exc:
        log.warning("trusted forward click failed: %s", exc)
        return False


def _submit_success(page) -> bool:
    """Poll until the modal is gone (or LinkedIn confirms the send)."""
    for _ in range(8):
        page.wait_for_timeout(1000)
        if not _modal_visible(page):
            return True
        try:
            text = page.inner_text("body", timeout=2500) or ""
        except Exception:
            text = ""
        if re.search(
            r"we[ ']ve sent your application|you['’]ve applied|application was sent",
            text,
            re.IGNORECASE,
        ):
            return True
    return False


def _step_signature(page) -> str:
    """Fingerprint of the current modal step (counter + primary button + field count)."""
    try:
        return str(
            page.evaluate(
                r"""() => {
                  const modal = [...document.querySelectorAll('[role="dialog"], .jobs-easy-apply-modal, div, aside')]
                    .find((e) => {
                      const s = getComputedStyle(e); if (s.display === 'none' || s.visibility === 'hidden') return false;
                      const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0
                        && /^apply to/i.test((e.innerText || '').trim()) && /pages/.test((e.innerText || '').slice(0, 140));
                    });
                  if (!modal) return 'gone';
                  const footer = modal.querySelector('footer');
                  const btn = footer ? [...footer.querySelectorAll('button')].find((b) => {
                    const s = getComputedStyle(b); return s.display !== 'none' && s.visibility !== 'hidden';
                  }) : null;
                  const n = modal.querySelectorAll('input:not([type]),input[type="text"],input[type="number"],input[type="email"],input[type="tel"],input[type="date"],select,textarea').length;
                  const head = (modal.innerText || '').slice(0, 120).replace(/\s+/g, ' ').trim();
                  return head + '|' + ((btn && (btn.innerText || btn.getAttribute('aria-label') || '')) || '').trim() + '|' + n;
                }"""
            )
        )
    except Exception:
        return ""


def _abort(page) -> None:
    """Close the Easy Apply modal without submitting."""
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(600)
    except Exception:
        pass
    if _modal_visible(page):
        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(600)
        except Exception:
            pass
    if _modal_visible(page):
        try:
            page.locator("[aria-label='Close'], [aria-label='Dismiss'], .artdeco-modal__dismiss").first.click()
        except Exception:
            pass


def _batch_unknown_labels(report) -> list[str]:
    """Unique unknown field labels across a run, deduped by normalized form."""
    seen: set[str] = set()
    out: list[str] = []
    for _, labels in report.unknown_fields:
        for lab in labels or []:
            key = re.sub(r"[\s:()\[\]]+", " ", lab.lower()).strip()
            if lab and key not in seen:
                seen.add(key)
                out.append(lab)
    return out


def _format_batch_question(labels: list[str]) -> str:
    """Build the cumulative Telegram prompt listing every unknown field."""
    lines = [
        "\U0001f4cb Apply run finished. I need your input for the questions below:",
        "",
        "Reply with the values, one per line, as:",
        "  <number or field>: <value>",
        "",
    ]
    for i, lab in enumerate(labels, 1):
        lines.append(f"  {i}. {lab}")
    lines.append("")
    lines.append("Example:")
    lines.append("  current salary: 120000")
    lines.append("")
    lines.append("Send 'skip' to skip for now.")
    return "\n".join(lines)


def _parse_reply(reply: str) -> dict[str, str]:
    """Turn the Telegram reply into {answer key: value} (keys left verbatim)."""
    answers: dict[str, str] = {}
    for raw in reply.splitlines():
        line = raw.strip()
        if not line or ":" not in line:
            continue
        k, _, v = line.partition(":")
        k = k.strip()
        v = v.strip()
        if k and v:
            answers[k] = v
    return answers


def _match_reply_to_labels(answers: dict[str, str], labels: list[str]) -> dict[str, str]:
    """Map answered keys to canonical field labels (substring or numbered index)."""
    matched: dict[str, str] = {}
    labels_by_norm = [                 # keep in lock-step with `labels`
        re.sub(r"[\s:()\[\]]+", " ", x.lower()).strip() for x in labels
    ]
    for key, value in answers.items():
        kn = re.sub(r"[\s:()\[\]]+", " ", key.lower()).strip()
        if kn.isdigit():
            idx = int(kn) - 1
            if 0 <= idx < len(labels):
                matched[labels[idx]] = value
            continue
        if len(kn) < 3:
            continue
        for lab, ln in zip(labels, labels_by_norm):
            if kn in ln or len(ln) >= 3 and ln in kn:
                matched[lab] = value
                break
    return matched


def resolve_unknowns_via_telegram(settings, report) -> dict[str, str]:
    """Ask the user for every unknown field in one message and memorize answers.

    Before asking, the local LLM gets one shot at the batch labels (covers the
    case where it was down mid-apply). Verified hits are memorized and excluded
    from the question. The user's batch reply is parsed and saved into
    config/answers.yaml so the next run answers them automatically. No reply
    within the timeout -> nothing is memorized.
    """
    labels = _batch_unknown_labels(report)
    if not labels or not settings.telegram.enabled:
        return {}
    if settings.llm.enabled:
        from .llm import semantic_prefill

        hits = semantic_prefill(labels, settings.answers.values, settings.llm)
        if hits:
            for q, h in hits.items():
                settings.answers.values[q] = h["value"]
                report.notes.append(
                    f"auto-answered + memorized [{h['source']}]: "
                    f"'{q[:70]}' → {h['key']} ({h['value']})"
                )
            settings.answers.save()
            labels = [lab for lab in labels if lab not in settings.answers.values]
        if not labels:
            return {}
    from .notify.telegram import ask_and_wait

    reply = ask_and_wait(
        settings.telegram,
        _format_batch_question(labels),
        timeout=settings.telegram.ask_timeout_seconds,
    )
    if not reply or reply.strip().lower() in ("skip", "skip it", "pass"):
        log.info("telegram: no usable answers for %d field(s); leaving memorization to next run", len(labels))
        return {}
    parsed = _parse_reply(reply)
    matched = _match_reply_to_labels(parsed, labels)
    if not matched:
        log.info("telegram: reply could not be matched to any field; nothing memorized")
        return {}
    settings.answers.values.update(matched)
    settings.answers.save()
    log.info("telegram: memorized %d new answer(s) into answers.yaml", len(matched))
    return matched


def _attempt_apply(browser, listing: Listing, settings: Settings, dry_run: bool) -> dict:
    page = browser.page
    try:
        page.goto(listing.url, wait_until="domcontentloaded")
        browser.assert_clear()
        page.wait_for_timeout(2500)

        if not _click_easy_apply(page):
            return {"outcome": "error", "note": "no Easy Apply button on detail page"}

        for _ in range(8):  # wait for the modal
            page.wait_for_timeout(750)
            if _modal_visible(page):
                break
        if not _modal_visible(page):
            return {"outcome": "error", "note": "Easy Apply modal did not open"}

        last_sig: str | None = None
        stale = 0
        job_answers = dict(settings.answers.values)
        llm_hits: dict[str, dict] = {}   # question label -> semantic_prefill entry
        asked_llm: set[str] = set()
        filled_log: list[dict] = []      # {label, value, source} audit trail
        memorized: list[tuple[str, dict]] = []  # (label, hit) to persist
        for fwd in range(1, MAX_STEPS + 1):
            if not _modal_visible(page):
                return {"outcome": "error", "note": "modal closed unexpectedly", "filled": filled_log, "memorized": memorized}
            sig = _step_signature(page)
            if sig and last_sig is not None:
                if sig == last_sig:
                    stale += 1
                    if stale >= 2:
                        _abort(page)
                        return {"outcome": "error", "note": "form would not advance; needs manual review", "filled": filled_log, "memorized": memorized}
                else:
                    stale = 0
            last_sig = sig
            try:
                res = page.evaluate(_apply_js(_RESOLVE_JS), {"answers": job_answers})
            except Exception as exc:
                return {"outcome": "error", "note": f"field scan failed: {exc}", "filled": filled_log, "memorized": memorized}
            if res is None or res.get("ok") is False:
                return {"outcome": "error", "note": "form not resolvable", "filled": filled_log, "memorized": memorized}
            for f in res.get("filled") or []:
                label, value = f.get("label") or "", f.get("value") or ""
                if label and not any(e["label"] == label for e in filled_log):
                    src = llm_hits[label]["source"] if label in llm_hits else "db"
                    filled_log.append({"label": label, "value": value, "source": src})
            if res.get("unknown"):
                fresh = [u for u in res["unknown"] if u not in asked_llm]
                if fresh and settings.llm.enabled:
                    # One LLM round per new question: look up the answer in our
                    # own database, fill it, and keep applying. Still stuck
                    # afterwards -> skip the job; Telegram is the last resort.
                    from .llm import semantic_prefill

                    asked_llm.update(fresh)
                    for q, h in semantic_prefill(
                        fresh, settings.answers.values, settings.llm
                    ).items():
                        job_answers[q] = h["value"]
                        llm_hits[q] = h
                        if not dry_run:
                            # Memorize permanently: each question decided once.
                            settings.answers.values[q] = h["value"]
                            memorized.append((q, h))
                    if any(q in llm_hits for q in fresh):
                        continue  # re-resolve this step with the new answers
                _abort(page)
                return {"outcome": "unknown", "fields": res["unknown"], "filled": filled_log, "memorized": memorized}

            action = _forward_action(page)
            if action is None:
                _abort(page)
                return {"outcome": "error", "note": "no Next/Submit button found", "filled": filled_log, "memorized": memorized}
            if action == "submit":
                if dry_run:
                    _abort(page)
                    return {"outcome": "dry-applied", "filled": filled_log, "memorized": memorized}
                if not _click_forward_trusted(page):
                    # fallback to the JS click
                    try:
                        page.evaluate(_apply_js(_SUBMIT_JS))
                    except Exception:
                        return {"outcome": "error", "note": "submit click failed", "filled": filled_log, "memorized": memorized}
                return {"outcome": "applied" if _submit_success(page) else "error", "note": "submit clicked", "filled": filled_log, "memorized": memorized}
            # 'next' — proceed, some steps reveal more fields
            if fwd >= MAX_STEPS:
                _abort(page)
                return {"outcome": "error", "note": "form had too many steps", "filled": filled_log, "memorized": memorized}
            if not _click_forward_trusted(page):
                try:
                    page.evaluate(_apply_js(_NEXT_JS))
                except Exception as exc:
                    return {"outcome": "error", "note": f"next click failed: {exc}", "filled": filled_log, "memorized": memorized}
            page.wait_for_timeout(2200)

        _abort(page)
        return {"outcome": "error", "note": "form had too many steps", "filled": filled_log, "memorized": memorized}
    except (LoginRequired, CaptchaDetected, AccountBlocked) as exc:
        return {"outcome": "locked", "note": str(exc)}
    except Exception as exc:
        try:
            _abort(page)
        except Exception:
            pass
        return {"outcome": "error", "note": str(exc)}


def run_apply(
    settings: Settings, headless: bool = True, dry_run: bool = False, limit: int | None = None
) -> ApplyReport:
    from .browser import Browser

    source = "linkedin"
    caps = settings.caps.for_source(source)
    cap = settings.caps.global_.daily_apply_budget
    report = ApplyReport(dry_run=dry_run, budget_left=cap or 0)

    with Browser(source, caps, headless=headless) as browser:
        db = get_session()
        remaining = remaining_budget(db, "apply", cap) if not dry_run else (limit or cap)
        if remaining is None:
            remaining = cap
        remaining = max(0, remaining)
        if limit is not None:
            remaining = min(remaining, limit)
        report.budget_left = remaining if not dry_run else (limit or cap)
        if remaining <= 0:
            log.warning("daily apply budget exhausted (%s/day)", cap)
            return report
        known_before = set(settings.answers.values)

        candidates = db.exec(
            select(Listing)
            .where(
                Listing.source == source,
                Listing.status == ListingStatus.QUEUED.value,
                Listing.apply_path == ApplyPath.INLINE.value,
            )
            .order_by(Listing.match_score.desc())
        ).all()

        for listing in candidates[:remaining]:
            result = _attempt_apply(browser, listing, settings, dry_run)
            note = result.get("note") or ""
            fields = result.get("fields") or []
            filled = result.get("filled") or []
            for _q, h in result.get("memorized") or []:
                report.notes.append(
                    f"auto-answered + memorized [{h['source']}]: "
                    f"'{_q[:70]}' → {h['key']} ({h['value']})"
                )

            if result["outcome"] == "applied":
                listing.status = ListingStatus.APPLIED.value
                listing.reason = f"applied {_today_iso()}"
                report.applied += 1
                report.applied_detail.append(
                    {"company": listing.company, "title": listing.title, "filled": filled}
                )
                if not dry_run:
                    register_spend(db, "apply")
                    report.budget_left = max(0, report.budget_left - 1)
            elif result["outcome"] == "unknown":
                listing.reason = (
                    f"apply needs fields: {', '.join(fields)}" if fields else "apply needs manual input"
                )
                if not dry_run:
                    listing.status = ListingStatus.NEEDS_MANUAL.value
                report.unknown += 1
                report.unknown_fields.append((listing.company, fields))
            elif result["outcome"] == "locked":
                listing.reason = f"guardrail during apply: {note}"
                if not dry_run:
                    listing.status = ListingStatus.NEEDS_MANUAL.value
                report.guarded += 1
            elif result["outcome"] == "dry-applied":
                report.applied += 1
                report.notes.append(f"would have applied: {listing.title} @ {listing.company}")
            else:  # error
                listing.reason = f"apply error: {note}"
                if not dry_run:
                    listing.status = ListingStatus.NEEDS_MANUAL.value
                report.errors += 1
                report.notes.append(f"{listing.title} @ {listing.company}: {note}")

            if not dry_run:
                db.add(listing)
                db.commit()
            log.info(
                "[%s] %s @ %s%s",
                listing.title[:30],
                listing.company[:20],
                result["outcome"],
                f" ({', '.join(fields)})" if fields else "",
            )
            if not dry_run and report.budget_left <= 0:
                break
            if dry_run:
                time.sleep(0.2)
            else:
                browser.sleep_detail()

    # Persist any answers the LLM round memorized, then ask the human about
    # whatever is still unknown. Absorb last so coverage sees all answers.
    from .knowledge import absorb_unknown_fields

    if not dry_run:
        if set(settings.answers.values) != known_before:
            settings.answers.save()
        resolve_unknowns_via_telegram(settings, report)
    absorb_unknown_fields(report.unknown_fields, settings.answers.values)
    return report