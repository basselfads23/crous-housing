# CROUS Housing Bot — Engineering Handoff

Read this entire file before touching anything. Do not skip to a task without reading the rest.

## What this is
Python daemon on a VPS (systemd, user ubuntu, venv at ~/crous-housing/venv) that watches CROUS
student housing listings in Marseille and auto-applies to good ones.

**Auto-apply is currently OFF** (`AUTO_APPLY_ENABLED=false` in `.env`; `AUTO_APPLY_DRY_RUN=false`).
Live sniping ran from 2026-09-26 until the user turned it off. When it's on, a qualifying Marseille
listing gets a real booking request submitted to CROUS — not a screenshot-only dry run. Treat
anything touching the auto-apply trigger logic (`is_target_listing`), the apply flow
(`crous_apply.py`), or proxy selection as live-production-risk, not a safe sandbox.

**session-keeper.service is STOPPED and DISABLED (2026-10-02).** While running it does a full
headless login every ~20 min ("aged" renewal, even when the session is still valid), each through a
different residential proxy IP, so MesServices emailed the user a "new sign-in" alert ~70x/day.
`session_keeper.py` now also re-reads `AUTO_APPLY_ENABLED` from `.env` every cycle and idles (no
session check, no login) while it's false. **To turn sniping back on:** set
`AUTO_APPLY_ENABLED=true`, restart crous-watcher.service, and
`sudo systemctl enable --now session-keeper.service` — otherwise the session expires and snipes
(including the manual Telegram "Snipe" button) fail until a `/renew`.

The user has a room now; the bot's job is **data collection only**. `.env` is deliberately left as
is (credentials present, `AUTO_APPLY_DRY_RUN=false`) at the user's call. Telegram `/status` lists
what the bot does and does NOT do (`_format_bot_capabilities_text()`, tested by
`verify_status_capabilities.py`), including the manual actions that still log in to the account:
the 🎯 Snipe button (shown on EVERY alert while auto-apply is off, and LIVE), `/renew`, `/test_apply`.

## Architecture
- crous_watcher.py — main polling loop, Telegram bot (commands + the owner-only "Snipe" button),
  fetch_all_crous_listings(), discover_tool_ids() (cached hourly), check_and_notify(), main_loop().
  Since 2026-09-27, record_nationwide_listing_raw() also appends the FULL raw CROUS record (every
  field, not a curated subset — equipments, media captions, occupationModes, availability...) to
  nationwide_listings_data.jsonl for every genuinely new listing anywhere in France, via
  _detect_and_record_nationwide_new(). Separate from, and in addition to, record_marseille_listing()
  (Marseille-only, curated subset, unchanged). Not gitignored, not currently git-tracked either —
  a decision on whether to commit this (and marseille_listings_data.jsonl) is still open.
- **Alerts fire once per APPEARANCE (since 2026-10-02), not once per listing ID ever.**
  Before, `seen_ids` never forgot an ID, so a listing that went offline and was re-posted was
  silently ignored forever — no alert, no Marseille data line. Measured: since 2026-09-27 Marseille
  listings were visible ~38 separate times but only 4 alerted; #1165 (Luminy, 350€) re-posted
  2026-10-02 10:44 got nothing, and the viewer saw ~6 listings online while the owner got 2 alerts.
  Now `update_listing_visibility()` keeps `active` (online now) + `history` (appearance counts)
  in listings_seen.json: alert when a listing comes online, never again while it stays online,
  and again (as "🔁 OFFRE DE NOUVEAU DISPONIBLE", with its appearance number) after it goes
  offline and comes back. "Offline" = missing from `LISTING_GONE_AFTER_MISSES` (2) COMPLETE
  checks in a row; a check where any tool fetch failed never counts as missing. Why 2, not 1:
  the API has returned incomplete results with no error (France 53 -> 8 for one check,
  2026-09-30 00:00 UTC; Marseille 3 -> 2 -> 3 twice on 2026-10-02) — with 1, each such glitch
  would re-alert everything it hid. marseille_listings_data.jsonl now gets an `"event": "appeared"`
  line per appearance (`appearance_no`, `reappearance`) and an `"event": "gone"` line when one goes
  offline (`online_seconds`, `checks_seen`) — lines from before 2026-10-02 have no `event` field.
  posting_activity.json's Marseille count now counts appearances. The auto-sniper (if re-enabled)
  also fires on re-appearances — intended, since each is a real new chance. NOT changed: the
  nationwide raw data (nationwide_seen_ids.json) still records each ID only the first time.
  Tested by `verify_listing_reappearance_alerts.py`.
- **Fixed 2026-10-02: main_loop() wrote a stale startup snapshot of the state on every failed
  cycle**, erasing every ID recorded since startup (confirmed: #2156 alerted 09-25 07:11, erased
  by failure writes at 12:19-12:58, alerted again 09-27), and making "5 consecutive failures"
  count since startup. Failures now go through `_record_cycle_failure()`, which re-reads the file.
- crous_auth.py — Playwright headless login/session renewal (auto_login()), Altcha PoW solver,
  session.json read/write, check_session_status()/is_session_valid(). Audited — correct as-is.
- session_keeper.py — lives at /home/ubuntu/session_keeper.py, OUTSIDE this git repo (untracked).
  Own systemd service (session-keeper.service), independent of crous-watcher.service, same venv.
  Audited (read-only): reuses crous_auth.auto_login()/check_session_status() directly (so it
  automatically benefits from the residential-proxy auth fix below), writes session.json only via
  crous_auth's atomic write, never touches proxy_manager's index files directly. Confirmed safe.
  Gated on AUTO_APPLY_ENABLED (re-read from .env each cycle; idles when false) since 2026-10-02 —
  see the header note. Backup of the pre-gate version: ~/session_keeper.py.bak-2026-10-02.
  Bringing it into the repo is still a nice-to-have, not urgent.
- proxy_manager.py — TWO pools, THREE deliberately isolated selection systems:
  - **Datacenter pool** (proxies.txt, gitignored, real credentials): Webshare "Proxy Server" plan,
    200 proxies + 5 Oxylabs, $5.98/mo, 250GB included. Used by the scouter
    (get_current_proxy()/rotate_proxy(), group-aware exhaustion on HTTP 402, 24h TTL) and — found
    2026-09-26, previously undocumented — by crous_auth.check_session_status() (see STILL OPEN #7).
    9 of the original 200 confirmed dead (connection timeouts, not CROUS-side) and commented out
    2026-09-25 — 196 confirmed healthy against the real search API. A 10th, 82.22.214.251:6103, was
    commented out 2026-09-26: it answered 502 Bad Gateway to CONNECT tunnels to
    trouverunlogement.lescrous.fr (3/3 tries, instantly) while reaching example.com fine and other
    proxies reached CROUS fine → **195 active**. Backup: proxies.txt.bak-2026-09-26. A live
    interval test found zero errors down to 15s between polls and 1.0-1.5s between paginated
    requests (see below). The scouter takes ONE proxy per cycle (main_loop rotates each cycle), so
    one full lap of the pool ≈ 196 cycles ≈ 78 min at peak cadence — that's why a bad proxy's
    errors recur at a fixed ~79 min rhythm.
    - **Per-proxy quarantine (added 2026-09-26, scouter/datacenter pool ONLY).** A proxy earns a
      strike when it fails a request AND a different proxy then succeeds for that same request
      (`proxy_manager.record_proxy_strike`, wired in `crous_watcher._settle_proxy_health`). All
      attempts failing = nobody blamed (could be an outage). 402s are never strikes (they're
      per-account, `mark_group_exhausted`). 3 consecutive strikes (any success resets) → benched
      24h in `.proxy_quarantine.json` (gitignored via `*.json`; host:port only, no credentials);
      after 24h one probation chance (1 more failure re-benches, 1 success clears). Hard cap: never
      more than 25% of the pool benched (owner gets an alert instead), and a corrupt/missing state
      file fails OPEN (nothing benched). `load_proxies()`, the sniper pool and the auth pool are
      untouched. Manual reset: delete `.proxy_quarantine.json`. Tests: `verify_proxy_quarantine.py`
      (20 tests; mutation-checked — 12 deliberate breakages, all caught).
      **CONFIRMED working in real production (2026-09-29/30):** fired for the first time on real
      traffic, unprompted — 3 proxies benched on genuine `407 Proxy Authentication Required`
      errors, exactly one Telegram alert each, no repeat noise, all 3 correctly excluded from
      rotation afterward. Not just passing synthetic tests anymore.
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
  scouter.log/sniper.log are capped at the last 2000 lines (~4-5h of scouter history at peak
  cadence), and watcher.log's "Network issue on proxy" warnings don't name the proxy — so
  proxy-level forensics has to be done the same day.
  Since 2026-09-26 the scouter's error alert fires only when a request fails ALL its attempts
  (error_message rides on the final attempt). A failure that a retry on the next proxy recovers is
  still logged (scouter.log + watcher.log) and counted as a proxy strike, but no longer pages the
  owner — this deliberately reversed an earlier "alert on every attempt" rule ("Fix A"; origin
  not found in git). A proxy being benched sends one owner-only alert.
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
250€ <= rent <= 300€    -> auto-snipe if surface >= 12 m² (no upper bound)
rent > 300€, <= 400€    -> alert only (manual Snipe button available)
colocation, any price   -> alert only, always (manual Snipe button available)
```
A third auto-tier (300-350€, >19m²) existed briefly on 2026-09-25 and was deliberately removed —
"too loose to trust unattended." That band is manual-button-only now. Fails safe (no auto-apply,
no guessing) if surface data is ever missing. Anchor cases: a real 255€/12m² and a real
284.82€/19m² T1 both auto-snipe; a real 350€/14m² room does not.

The 250-300€ tier's upper surface bound (was 19m², inclusive) was removed 2026-09-28 at the
user's explicit request — "we don't want to restrict the bot... what if it was a 20m2 room for
270? We don't want the bot to miss this one." In this price band a bigger room is strictly a
better find, never a riskier one, so only the floor (12m²) still guards against "cheap because
it's tiny." `AUTO_APPLY_MID_MAX_SURFACE_M2` was removed from `.env` and the codebase entirely
(not left dead/unused) — do not reintroduce it without being asked. This is NOT a reinstatement
of the removed third tier above (different, higher price band, deliberately stays alert-only).
Verified: `verify_sniper_tier_logic.py` (24 tests, includes the hypothetical 20m²/270€ case),
mutation-checked (4/4 deliberate breakages caught: cap reintroduced, floor dropped, missing-
surface no longer fails safe, colocation exclusion bypassed).

**Reminder (not yet built, flagged 2026-09-27):** a successful snipe is not the end of the
process — CROUS gives a 2-3 day window to submit supporting documents before the demande is
auto-removed. Nothing in this codebase tracks that deadline or reminds the owner. Still open.

**The kitchen/bathroom "gotcha" (found 2026-09-27, still NOT wired into any trigger logic):**
none of the rules above check whether a room's kitchen/toilet are private vs shared down the
hall — they only check rent, surface, and individual-vs-colocation. A cheap individual room with
shared facilities (like the 2026-09-27 Gaston Berger snipe) can and will still auto-snipe today.

**CONFIRMED (2026-09-30, 104 real individual listings sampled nationwide via
`nationwide_listings_data.jsonl`):** CROUS's own `equipments` field on each raw item is a
reliable signal, not a guess — absence of the `Evier + plaque` tag (private kitchenette) on a
plain "CHAMBRE"-labeled listing correctly predicted shared facilities in 91% of cases (20/22
real examples), matching the Gaston Berger ground truth exactly (its own listing page: "Les WC
et les cuisines sont collectifs"). The free-text `label` field alone is NOT reliable ("CHAMBRE
SIMPLE" vs "CHAMBRE 9M²" vs "Chambre simple de T1" mean different things) — the fix, if built,
must key off `equipments`, not `label`. Still not wired into `is_target_listing()` — this was
investigation only, not yet implemented, and no one has asked for it to be.

**Colocation ≠ sharing a bedroom, in most cases (CONFIRMED 2026-09-30, 24 real colocation
listings sampled).** The owner's original assumption was right for the large majority: 22/24
(92%) of colocation (`house_sharing`) listings are real flatshares — a shared apartment where
each person gets their own private room (`bedCount` < `roomCount`, e.g. 4 people / 5 rooms, the
5th being a shared living room). **But the risky kind — two people in the literal same room —
is real and does occur**: 2/24 (8%) had `bedCount >= roomCount` in a single-room unit, both
labeled "T1 Bis" ("T1 bis 24 à 31m2", "T1Bis"). n=2 is too small to trust "T1 Bis" itself as the
predictor, but the underlying check (`bedCount` vs `roomCount` on the raw item) is now backed by
3 real examples total (1 found 2026-09-27 + 2 more here) and reliably tells the two apart. Not
wired into anything — colocation stays alert-only, always, regardless.

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
1. **DONE 2026-09-27** ~~Watch for the first real Marseille listing...~~ — happened: Gaston
   Berger, 255€/12m², id 2156, real LIVE (non-dry-run) auto-snipe, ~51s detection-to-confirmed-
   submission (within the estimated 27-57s window). Full trace in git history around that date.
   Sniping was then briefly paused, restarted in DRY-RUN for a day to keep collecting data
   without risk, then turned back LIVE 2026-09-28 at the user's explicit call.
2. **New, real-world, not yet built:** a successful snipe is NOT the end of the process — CROUS
   gives a 2-3 day window after booking to submit supporting documents, or the demande is
   auto-removed. Nothing in this codebase tracks that deadline or reminds the owner. Proposed (not
   built): a Telegram reminder tied to each successful snipe, pure alerting/logging, doesn't touch
   apply/auth code — low risk.
3. **New, real-world, not yet built:** the kitchen/bathroom "gotcha" — none of the auto-apply rules
   check whether a room's kitchen/toilet are private vs shared down the hall, only rent/surface/
   individual-vs-colocation. See the sniper-rules section above — the `equipments`-based signal
   is now CONFIRMED reliable (91% on 22 real examples) and the colocation shared-room check is
   CONFIRMED too (3 real examples), but NEITHER is wired into `is_target_listing()` yet.
4. Night/day request throttling (permanent version) — proposed, never built. Still open, but the
   underlying assumption is no longer untested:
   - **CONFIRMED (2026-09-30):** CROUS does NOT go quiet on Sundays — 22 new listings posted
     nationwide on Sunday 2026-09-27 alone, including the one that got sniped. The old "quiet at
     night/on Sundays" assumption behind smart cadence was wrong on the Sunday half.
   - **CONFIRMED for the current cadence windows specifically (2026-09-30, 120 real listings,
     Mon-Wed only so far):** matched against the exact configured windows in `get_smart_cadence()`
     — peak 08:00-18:30 (15s) captured 91.7% of real activity, evening 18:30-23:30 (75s) captured
     0.8% (1 listing), night 23:30-08:00 (240s) captured 7.5% (9 listings, mostly clustered
     04:00-07:00). This validates the peak window strongly.
   - **NOT yet settled, needs more exposure before touching any config:** the evening window looks
     probably safe to slow down further (almost nothing lands there), and the night window is
     probably worth tightening from 240s (real listings do land there, each sitting undetected up
     to 4 minutes instead of 15s) — but this is 3 weekdays only, no weekend hourly breakdown yet,
     and no auto-snipe-eligible listing has yet been confirmed to land specifically in the night
     window, so there's no evidence yet that the current 240s has actually cost a real snipe.
     Revisit once weekend hourly data exists and more days have accumulated. Throttle only, never
     a hard stop, per original reasoning.
5. Cleanup (low priority, no correctness risk): `seen_ids` in listings_seen.json and
   `nationwide_seen_ids.json` both grow forever, never trimmed. Also move the various
   `verify_*.py` test files and `proxies.txt.bak-2026-09-25` into `tests/` or delete once no longer
   needed. Also decide whether `marseille_listings_data.jsonl` / `nationwide_listings_data.jsonl`
   should be git-tracked (neither is currently, and neither is gitignored either).
6. The MCP `github` plugin connection failure (see Git state) — not needed for anything currently
   in use, but flag if GitHub-API-level tools become relevant.
7. `crous_auth.check_session_status()` (the `/api/health` session check, used by session-keeper and
   the watcher) calls `proxy_manager.load_proxies()` — the scouter's datacenter list — and always
   uses its first 3 entries (`proxies_to_try[retry % len]`), never rotating. Found 2026-09-26,
   NOT changed. It contradicts "datacenter pool is scouter-only" and bypasses the quarantine.
   Decide: leave as is, or point it at the residential pool / a rotating source.
8. `verify_proxy_group_fix.py` is still untracked in git (pre-existing; it passes) — commit or
   delete it with the rest of the `verify_*.py` cleanup in #5.
9. `is_target_listing()`'s price fallback (`raw_price = item.get("price") or 0`, used only when
   an item has NO usable `occupationModes` rent) defaults a listing with genuinely missing price
   data to €0 — i.e. auto-snipes it immediately, the opposite of the surface check's fail-safe
   two rows below it, which correctly refuses to guess. Found 2026-09-28, NOT fixed.
   **CONFIRMED still purely theoretical as of 2026-09-30:** checked across all 120 real listings
   collected in `nationwide_listings_data.jsonl` so far — zero have ever hit this fallback path.
   Worth fixing on principle (same pattern as the surface fix), not because it's caused a
   real incident.
10. **Test hygiene:** importing crous_watcher attaches a FileHandler to the LIVE watcher.log
   (logging.basicConfig at import). Tests that don't first add a NullHandler to the root logger
   (as verify_proxy_quarantine.py, verify_status_capabilities.py and
   verify_listing_reappearance_alerts.py do) write fake lines — mocked alerts, MANUAL SNIPE
   triggers — into the production log. 60 such lines from 2026-10-02 test runs were removed;
   older runs may have left more. Use the journal (`journalctl -u crous-watcher`) as the source
   of truth for analysis, or run other tests via a wrapper that adds the NullHandler first.

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
