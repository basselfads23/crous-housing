# CROUS Housing Bot — Engineering Handoff

Read this entire file before touching anything. Do not skip to a task without reading the rest.

## What this is
Python daemon on a VPS (systemd, user ubuntu, venv at ~/crous-housing/venv) that watches CROUS
student housing listings in Marseille and auto-applies to good ones.

**⚠️ LIVE SNIPING IS ACTIVE as of 2026-09-26.** `AUTO_APPLY_ENABLED=true` and
`AUTO_APPLY_DRY_RUN=false` in `.env`. On a qualifying Marseille listing, the bot now actually
submits a real booking request to CROUS — this is no longer a screenshot-only dry run. Treat
anything touching the auto-apply trigger logic (`is_target_listing`), the apply flow
(`crous_apply.py`), or proxy selection as live-production-risk from now on, not a safe sandbox.

## Architecture
- crous_watcher.py — main polling loop, Telegram bot (commands + the owner-only "Snipe" button),
  fetch_all_crous_listings(), discover_tool_ids() (cached hourly), check_and_notify(), main_loop().
- crous_auth.py — Playwright headless login/session renewal (auto_login()), Altcha PoW solver,
  session.json read/write, check_session_status()/is_session_valid(). Audited — correct as-is.
- session_keeper.py — lives at /home/ubuntu/session_keeper.py, OUTSIDE this git repo (untracked).
  Own systemd service (session-keeper.service), independent of crous-watcher.service, same venv.
  Audited (read-only): reuses crous_auth.auto_login()/check_session_status() directly (so it
  automatically benefits from the residential-proxy auth fix below), writes session.json only via
  crous_auth's atomic write, never touches proxy_manager's index files directly. Confirmed safe.
  Bringing it into the repo is still a nice-to-have, not urgent.
- proxy_manager.py — TWO pools, THREE deliberately isolated selection systems:
  - **Datacenter pool** (proxies.txt, gitignored, real credentials): Webshare "Proxy Server" plan,
    200 proxies + 5 Oxylabs, $5.98/mo, 250GB included. Used ONLY by the scouter
    (get_current_proxy()/rotate_proxy(), group-aware exhaustion on HTTP 402, 24h TTL). 9 of the
    original 200 confirmed dead (connection timeouts, not CROUS-side) and commented out
    2026-09-25 — 196 confirmed healthy against the real search API. A live interval test found
    zero errors down to 15s between polls and 1.0-1.5s between paginated requests (see below).
  - **Residential pool** (proxies_residential.txt, gitignored): Webshare "Static Residential"
    plan, 20 proxies, $6.00/mo, 250GB included. Real home-ISP IPs (Comcast, Orange, Rogers,
    Telecom Italia, etc, confirmed via Webshare's asn_name field) — look human, unlike datacenter
    IPs, which matters for login and the multi-page apply flow. Used by:
    - Auth: get_auth_proxy() — permanently excludes Oxylabs (confirmed 403 on
      messervices.etudiant.gouv.fr specifically, not this pool), live-validates each candidate
      against both trouverunlogement.lescrous.fr and messervices.etudiant.gouv.fr. Own rotation
      index (.auth_proxy_index).
    - Sniper: get_sniper_proxy()/rotate_sniper_proxy(), sharing _sniper_candidate_list() (fixed
      2026-09-25 — the two used to index into different orderings while sharing one stored index,
      meaning a post-429 rotation could land on an arbitrary proxy). Own index
      (.sniper_proxy_index).
  - Old free 10-proxy Webshare plan: auto-cancelled by Webshare when the paid plans were
    purchased. Second Webshare account ("kurosaki ichigo" / pftzcdqy) still disabled/commented out
    in proxies.txt — untouched, not currently in use.
- crous_apply.py — automated booking/application logic. Fully audited 2026-09-25. Occupation-mode
  selection (radio/dropdown) used to silently default to whichever option came first in the DOM if
  none matched the requested mode — fixed via `_pick_mode_index()`, which now hard-fails (log +
  screenshot) instead of guessing. Study-level failures now log instead of failing silently.
  Screenshots older than 14 days auto-prune. The "does dry-run leave anything behind on CROUS's
  side" question was resolved by evidence (a real Step-2 screenshot shows an unclicked "Modifier
  mon formulaire" option and CROUS's own copy describing multi-day human review after the final
  click) — not proven via network trace, but accepted as good enough.
- activity_logger.py — Telegram error/failure alert dispatch (owner-only by construction — only
  ever reads TELEGRAM_CHAT_ID, never touches the viewer list) + scouter/sniper attempt logging.
- Telegram messaging is now split by audience (fixed 2026-09-25): the viewer
  (TELEGRAM_VIEWER_CHAT_IDS) gets ONLY the new-listing alert, nothing else. Errors, sniper
  results, test messages, and the startup/restart message are owner-only.
  `broadcast_telegram_message()` was removed entirely once it had no callers left.
- Manual "🎯 Snipe" button (new 2026-09-25): appears only in the owner's copy of an alert, and
  only when the listing WASN'T already auto-sniped. Tapping it runs the identical
  apply_for_accommodation() engine as auto-snipe, respecting AUTO_APPLY_DRY_RUN. Debounced 90s per
  accommodation_id to prevent a double-tap from firing two real attempts back-to-back (this
  matters — see the 429 note below).

## Git state
- Repo: https://github.com/basselfads23/crous-housing.git, local branch `main`, **fully pushed
  and in sync as of 2026-09-26**.
- Push auth is now configured on the VPS: `git config --global credential.helper store`, with a
  fine-grained GitHub PAT (repo-scoped to crous-housing, Contents: read/write) stored in
  `~/.git-credentials` (chmod 600, outside the repo, never committed). `git push origin main`
  works directly from here now — no more manual auth needed per session.
- The MCP `github` plugin (separate from the above) failed to connect this session
  (`Authorization header is badly formatted`) — untouched/unfixed, not needed for git push, but
  flag it if the user wants to use GitHub-API-level tools (PRs, issues) via that plugin.
- Git identity: user.name "gitdabassel", user.email "basselfads23@gmail.com".
- Stale feature branches exist (feat/viewer-broadcast, fix/atomic-session-write, fix/auth-proxy,
  fix/cadence, fix/remove-hourly-check, fix/session-status-states) — already merged into main.
  Don't delete without checking first.

## Current sniper trigger rules (individual only; colocation is NEVER auto-applied)
```
rent < 250€            -> auto-snipe, any surface
250€ <= rent <= 300€    -> auto-snipe only if surface is 12-19 m² (inclusive)
rent > 300€, <= 400€    -> alert only (manual Snipe button available)
colocation, any price   -> alert only, always (manual Snipe button available)
```
A third auto-tier (300-350€, >19m²) existed briefly on 2026-09-25 and was deliberately removed —
"too loose to trust unattended." That band is manual-button-only now. Fails safe (no auto-apply,
no guessing) if surface data is ever missing. Anchor cases: a real 255€/12m² and a real
284.82€/19m² T1 both auto-snipe; a real 350€/14m² room does not.

## Current cadence (tightened 2026-09-25, all changes backed by live testing, not guesses)
- Peak hours (08:00-18:30 Paris): 15s between polls (was 50s) — live-tested down to 15s with
  zero errors (30s/25s/20s/15s all clean, 5 cycles each, real fetch_all_crous_listings() calls).
- Pagination delay (between pages of the same search): 1.0-1.5s (was 2.0-3.0s) — live-tested: 2.5s/
  1.5s/1.0s all clean, 0.5s produced a real failure (~1.1s, not a timeout — possibly an active
  rejection). 1.0-1.5s is the tightest interval proven clean; do not push below it without
  re-testing.
- The "homepage check -> search request" pacing delay (2-3s) is now skipped entirely on the
  common case (tool-ID cache hit, i.e. most cycles) — it only fires on the rare real homepage
  fetch (at most once/hour), via `discover_tool_ids_with_fetch_flag()`.
- Full lifecycle estimate (scout identifies a matching listing -> booking click sent), built from
  real measurements, not guesses: **best case ~27s, worst case ~57s.**
- A single-proxy "hammer test" (repeated requests to the same IP, ramping down to 0.25s spacing)
  found zero rate-limit errors — deliberately did not push further once that was established, to
  avoid unnecessarily burning a proxy.

## Two real Telegram-path bugs found and fixed 2026-09-25/26 (user noticed sniper attempts
triggered via Telegram failed more often than direct CLI verification — this was the trigger for
finding both)
1. `METRICS["telegram_update_offset"]` lived in memory only, resetting to 0 on every restart.
   Telegram redelivers unconfirmed updates for up to 24h — a restart soon after an interaction
   could silently replay a command or button tap, re-triggering a real action. Traced tonight's
   specific 429 via watcher.log and confirmed it was NOT caused by this (68+80 polls happened
   between the tap and the next restart, already long-confirmed) — but it's a real, live risk for
   future interactions, especially given how often this service gets restarted during active
   development. Fixed: offset now persisted to `.telegram_update_offset` (gitignored) immediately
   on receipt, before handling.
2. `/test_apply` picked `items[0]` from a full nationwide fetch — an unpredictable, possibly
   already-unavailable listing, different every call, nothing like the stable listing used for all
   manual CLI verification. Fixed via `pick_test_apply_target()`, which prefers the first listing
   CROUS itself marks available.

## A real 429 happened during testing 2026-09-25 — read this before assuming the sniper is broken
Listing #6 (a real, currently-available non-Marseille listing used all session as the safe test
target) got HTTP 429'd by CROUS after ~11 total test attempts (8 in one evening). No clean
pattern (different proxies both succeeded and failed; a failure came 29 minutes after a success on
a brand-new proxy). Best read: this specific listing was hit far more than any real listing ever
would be from normal operation (one auto-snipe attempt, or one manual button tap, ever) — this is
an artifact of heavy repeated testing against one target, not a demonstrated flaw in the live
path. The error-handling worked correctly throughout (detected the 429, screenshotted it, reported
it accurately). Recommendation: don't reuse listing #6 (or any single real listing) for repeated
live testing going forward — it may still be flagged from this.

**As of this handoff, the bot has NOT yet fired on a genuine new Marseille listing end-to-end** —
everything tested has been manual CLI/button triggers against non-Marseille test listings. The
first real trigger, whenever it happens, is the true first live test.

## STILL OPEN, IN PRIORITY ORDER
1. Watch for the first real Marseille listing and how the live (non-dry-run) sniper actually
   performs on it — nothing so far has exercised the true automatic end-to-end path.
2. Night/day request throttling (permanent version) — proposed, never built. Now has a real data
   source (`posting_activity.json`, recording first/last-seen listing times daily, nationwide and
   Marseille separately) instead of the untested "quiet at night/on Sundays" assumption — revisit
   once enough days have accumulated. Throttle only, never a hard stop, per original reasoning —
   though bandwidth is no longer the constraint it once was, so re-examine the cost/benefit once
   there's real posting-hours data.
3. Cleanup (low priority, no correctness risk): `seen_ids` in listings_seen.json and
   `nationwide_seen_ids.json` both grow forever, never trimmed. Also move the various
   `verify_*.py` test files and `proxies.txt.bak-2026-09-25` into `tests/` or delete once no longer
   needed.
4. The MCP `github` plugin connection failure (see Git state) — not needed for anything currently
   in use, but flag if GitHub-API-level tools become relevant.

## Standing rules for all future work in this repo
- Redact all secrets (passwords, tokens, API keys) in any output or log you produce.
- Never introduce a silent fallback to an unproxied/direct connection anywhere auth or apply is
  concerned. If all proxies are exhausted/excluded, fail loudly and explicitly.
- Keep the three proxy-selection systems (scouter / auth / sniper) strictly isolated unless a
  task explicitly asks you to touch more than one. State this explicitly in every task summary.
- Commit in small, single-purpose commits as you go, not one large commit at the end.
- Push to origin after every commit, or at minimum every session (auth is now configured — no
  excuse to skip this going forward).
- For ANY claim that something "works," "passed," or "is verified": show the actual raw command
  output (test output, curl output, grep output, diff output) — not a prose summary. This has
  caught real bugs already in this project's history and is non-negotiable.
- NEVER restart crous-watcher.service or session-keeper.service without the user explicitly
  confirming after being shown (a) the exact diff and (b) raw test output. Do not restart as a
  routine "finishing" step.
- Verification effort scales with blast radius:
  - HIGH (full diff review + written tests + raw output required before going live): anything
    touching crous_auth.py, crous_apply.py, session.json, session_keeper.py, the auto-apply
    trigger logic in is_target_listing(), or proxy rotation/exhaustion/retry/alert-threshold
    logic; anything unattended. **Live sniping is now on — this bar applies to more of the
    codebase than before, not less.**
  - LOW (move faster): pure logging/comment changes, read-only diagnostics, docs, non-functional
    cleanup.
- Be genuinely careful with live testing against CROUS's real infrastructure: prefer mocked tests
  for routine verification; reserve real requests for when they're truly needed, and don't reuse
  the same single real listing for repeated testing (see the 429 note above).
- If you discover anything not mentioned in this handoff (another undocumented process, script
  outside the repo, branch, or anything that changes the risk picture) — STOP, report it clearly,
  and wait for confirmation before proceeding.
- Do not make changes beyond what a task explicitly asks for.
- Update this HANDOFF.md's status as things get fixed or new issues are found, so it stays
  accurate for whoever reads it next. This file was substantially rewritten 2026-09-26 to
  condense a long session's worth of detailed fix-by-fix history into current-state facts — if it
  starts accumulating the same kind of long narrative again, condense it again rather than let it
  grow unbounded.

## A note on how this got built

Every fix in this file's history followed the same shape: the project owner (not a trained
engineer, but someone who reasons carefully about edge cases, failure modes, and tradeoffs) asked
a specific, pointed question — often about something that "felt off" (a timing number, a
suspiciously convenient assumption, a bug that "shouldn't" be happening) — and the answer was
never allowed to just sound right. It had to be checked: real logs, real timestamps, real API
calls, real test output. Several of the fixes above exist only because a confident-sounding
first explanation didn't survive that check (the Telegram-offset bug's real blast radius, the
"is dry-run actually safe" question, the city-match false-positive class). Whoever or whatever
picks this project up next — human or AI — the standing rules above aren't bureaucracy, they're
the reason this bot's failure modes are actually understood instead of assumed. Keep asking the
question that doesn't take the first plausible answer.
