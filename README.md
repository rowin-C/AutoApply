# AutoApply

AI-assisted job application discovery. Step 1 (this repo state): a
deterministic discovery pipeline for LinkedIn that captures listings, 
fetches full job descriptions, classifies how each job is applied to
(**inline** Easy Apply vs **redirect** to a company ATS), scores them
against your profile, and produces a ranked review queue.

Everything is SQLite-backed, incremental, and safe to re-run daily.

## Quickstart

```bash
uv venv .venv --python 3.12      # or 3.13/3.14
uv pip install --python .venv/bin/python -e . playwright
.venv/bin/python -m playwright install chromium
```

Then edit the YAML configs in `config/` — every value is placeholdered and
documented inline:

- `profile.yaml`   — your titles, must/nice skills, locations, salary floor, exclusions
- `searches.yaml`  — keyword × location combos + filters (posted-days, remote, experience)
- `caps.yaml`      — per-source page limits, delays, and daily budgets (the account-protection levers)

## One-time login

```bash
.venv/bin/aa login linkedin
```

A real browser opens using a persistent profile saved under `data/profiles/`.
Log in manually, press Enter in the terminal. Every later run reuses that
session — runs are headless by default; add `--headed` to watch.

## Daily loop

```bash
.venv/bin/aa search                     # Phase A: capture listings (deduped)
.venv/bin/aa fetch-details              # Phase B: JDs + apply-path classification
.venv/bin/aa qualify                    # rules-based scoring -> queued/skipped
.venv/bin/aa review --min-score 7       # the ranked queue
```

`aa review` flags by apply path and status:

```bash
.venv/bin/aa review --apply-path inline      # easy-apply jobs only
.venv/bin/aa review --status needs_manual    # jobs that hit a captcha/block
```

## What gets recorded per listing

`source · job_id · title · company · location · salary · full_jd · match_score ·
apply_path (inline|redirect) · external_url · ats_type · status`

The `external_url` + `ats_type` capture is the key design move: most company
career sites run on a handful of ATS platforms (Lever, Greenhouse, Workday,
SmartRecruiters, …). Detecting them at discovery time means the later
company-site applier is generic per platform, not per company.

## Statuses

`new → detailed → queued / skipped / needs_manual → applied` (Step 2).

## What's deliberately not here yet

Apply + submit engine, ATS field-mapping, Telegram captcha loop, MCP tool
surface, the supervisor agent, Indeed/Naukri adapters (adapter shape is
ready to clone).

## Recurring failure handling

Guardrails (login wall, CAPTCHA, block page) never crash a run — the listing
is marked `needs_manual` and iteration continues. Delays are randomized from
caps ranges, and daily budgets (detail fetches, and later applies) are
hard-enforced via a spend ledger in SQLite.

## Tests

```bash
.venv/bin/python -m pytest
```