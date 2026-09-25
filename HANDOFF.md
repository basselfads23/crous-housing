# CROUS Housing Bot — Engineering Handoff

Read this entire file before touching anything. Do not skip to a task without reading the rest.

## What this is
Python daemon on a VPS (systemd, user ubuntu, venv at ~/crous-housing/venv) that watches CROUS
student housing listings in Marseille and can auto-apply to good ones.

## Architecture
- crous_watcher.py — main polling loop, Telegram bot commands (/status /renew /check etc),
  fetch_all_crous_listings() (scouting via CROUS's public search API), discover_tool_ids(),
  check_and_notify(), main_loop().
- crous_auth.py — Playwright headless login/session renewal (auto_login()), Altcha PoW solver,
  session.json read/write, check_session_status()/is_session_valid().
- proxy_manager.py — proxy rotation. THREE separate, deliberately isolated selection systems:
  - Scouter (crous_watcher.py): get_current_proxy()/rotate_proxy(), group-aware — proxies grouped
    by provider account (parsed from username in the proxy URL). A whole group can be marked
    "exhausted" (persisted in .exhausted_proxy_groups.json, 24h TTL) on HTTP 402 (Webshare pooled-
    bandwidth exhaustion). Fixed and verified 2026-09-25 (commit aea186e).
  - Auth (crous_auth.py): get_auth_proxy() — PERMANENTLY excludes any Oxylabs proxy (confirmed via
    direct probing: messervices.etudiant.gouv.fr, the OAuth host CROUS login redirects to, returns
    a hard 403 for ALL Oxylabs IPs, every time — provider-level targeting, not bandwidth-related,
    so do NOT reuse the group-exhaustion/TTL mechanism for this). Live-validates each non-Oxylabs
    candidate against BOTH trouverunlogement.lescrous.fr and messervices.etudiant.gouv.fr before
    selecting it. Own rotation index (.auth_proxy_index), separate from scouter and sniper.
  - Sniper: get_sniper_proxy(), .sniper_proxy_index — NOT YET AUDITED on its own terms. Do not
    assume it shares any fix applied to the other two.
- crous_apply.py — automated booking/application logic. NOT YET AUDITED by anyone. Do not assume
  anything about its internals. Currently running in DRY-RUN mode.
- activity_logger.py — Telegram alert dispatch with cooldown, scouter attempt logging.
- proxies.txt — host:port:username:password, one per line. Currently: 5 Oxylabs datacenter
  (permanently excluded from auth, fine for scouting) + 10 Webshare datacenter proxies (confirmed
  via Webshare's own API `asn_name` field to be ordinary hosting-provider datacenter IPs —
  Leaseweb, Hostpapa, Heart Internet, etc — NOT residential). 7 of 10 Webshare proxies work fine
  against messervices.etudiant.gouv.fr; 3 (64.137.96.74, 191.96.254.138, 142.111.67.146) are just
  generically unreliable against any target, unrelated to auth specifically.
- session_keeper.py — ⚠️ DISCOVERED, NOT YET AUDITED. Lives at /home/ubuntu/session_keeper.py,
  OUTSIDE this git repo, untracked, contents unknown as of this handoff. Runs as its own systemd
  service (session-keeper.service), independently of crous-watcher.service, same venv. Systemd
  description: "keeps session.json authenticated." crous_watcher.py STILL contains
  is_session_valid()/auto_login() calls in multiple places despite a commit claiming the hourly
  check was "removed, superseded by session_keeper.py" — unclear if those call sites are dead,
  manual-command-only, or still running in parallel with session_keeper.py's own loop.
  MUST BE RESOLVED BEFORE TRUSTING TODAY'S AUTH FIX IN PRODUCTION — see Task 1.

## Git state
- Repo: https://github.com/basselfads23/crous-housing.git, local branch `main`.
- Stale feature branches exist (feat/viewer-broadcast, fix/atomic-session-write, fix/auth-proxy,
  fix/cadence, fix/remove-hourly-check, fix/session-status-states) — already merged into main's
  history. Don't delete without checking first.
- Git identity in this repo is already set: user.name "gitdabassel", user.email
  "basselfads23@gmail.com".
- As of this handoff, local main was 8 commits ahead of origin/main with ZERO backup pushed.

## Status: fixed and verified vs. still open

FIXED AND VERIFIED (line-by-line audit + tests run + raw output reviewed, live in production as
of 2026-09-25):
- Scouter 402 group-exhaustion handling (commit aea186e).
- Auth silent-fallback removal + per-attempt proxy logging (commit f900af8).
- get_auth_proxy() itself (built in an earlier session, commit 8d138d8) — reviewed today,
  confirmed correct as-is.
- session_keeper.py audited (read-only, TASK 1) — reuses crous_auth.auto_login()/
  check_session_status() directly (so it already gets the Oxylabs-exclusion + dual-host proxy
  validation for free), writes session.json only via crous_auth's existing atomic write, never
  touches proxy_manager's state files directly. crous_watcher.py's remaining
  is_session_valid()/auto_login() call sites are manual-Telegram-command-only or a one-time
  startup read; the automatic hourly path is confirmed gone. Still outside the git repo
  (untracked) — bringing it in is a nice-to-have, not urgent, since it's already correct as-is.
- Scouter city-match fix (commit eae726b, live as of 2026-09-25 17:37 UTC restart): matching now
  uses residence.location (lat/lon, present on every live search-API item) with a 20km radius
  from Marseille's center, instead of regex/postal-code matching on the free-text address. Fixes
  a real false-positive class (address text containing "marseille" as part of an unrelated street
  name, e.g. "Route de Marseille" in another town) and a real coverage gap (CROUS uses CEDEX-style
  postal codes like 13288/13388/13331 that aren't in the old hardcoded 13001-13016 list). Verified
  against the 4 real listings alerted this week (coordinates pulled live from each listing's own
  page) plus synthetic false-positive/negative controls — see verify_city_match_fix.py, 7/7 pass.
  Price-in-cents parsing (the `>1000 -> divide by 100` heuristic) was also reviewed against this
  week's real listings (284.82€, 328.00€, etc. all came out exactly right) — looks correct in
  practice, not currently a live bug, downgraded from the original suspicion.
- Tool-ID discovery hardening (commit a756348, live as of 2026-09-25 17:41 UTC restart):
  discover_tool_ids() used to hit the CROUS homepage every single cycle with a single unretried
  attempt, silently falling back to the hardcoded tool ID "47" on any failure with no alert. Now
  cached for up to an hour, retries 3x with proxy rotation between attempts (matching
  fetch_all_crous_listings' pattern), and on total failure reuses the last known-good tool ID list
  instead of the hardcoded default — only falling back to "47" (loudly, via Telegram) if there's no
  known-good list yet at all. Verified with verify_tool_id_discovery_fix.py, 5/5 pass.

Scouter is now considered solid enough to move on from, per the plan to fully harden it before
starting sniper work. Remaining scouter items (night/day throttling, seen_ids cleanup) are
low-priority polish, not correctness risks — see below.

- crous_apply.py audited (read-only, TASK 2 [renumbered from earlier draft]) and fixed (commit
  1ac0bee, live as of 2026-09-25 18:39 UTC restart): occupation-mode selection (radio button and
  dropdown) used to silently default to whichever option came first in the DOM if none matched the
  requested mode — for a "colocation" target with no "coloc"-labeled option, that's very likely
  "Individuel", meaning the sniper could have submitted a request for the wrong room type on a
  listing chosen specifically for its colocation price. Extracted the matching into a standalone,
  unit-tested `_pick_mode_index()` that returns "no match" instead of guessing; callers now hard-fail
  (log + screenshot + error result) instead of proceeding on an unverified mode. This ran in both
  DRY-RUN and LIVE paths (mode selection happens before the dry-run/live branch), so it was live in
  production DRY-RUN runs too. Also fixed: study-level selection failures now log a warning instead
  of failing silently; screenshots older than 14 days are now pruned automatically. Verified with
  verify_apply_mode_selection_fix.py, 10/10 pass.
  OPEN QUESTION FROM THIS AUDIT, NOT YET RESOLVED: dry-run mode still performs the real
  "Ajouter à ma sélection" (add to cart) and Step-1 "Vérifier ma demande" actions against the live
  CROUS site before stopping at the final confirmation click — it's unconfirmed whether that leaves
  any real state behind (a held/reserved unit, a pending request record, a quota hit) on the CROUS
  account. Needs a deliberate, watched live test (checking the real CROUS account afterward), not
  something to assume either way. Do this before trusting DRY-RUN's safety further, and definitely
  before ever flipping AUTO_APPLY_DRY_RUN off.
  Its return shape was checked to confirm proxy credentials never reach sniper.log (they don't —
  verified) — full audit of its own selection behavior happened next, see below.

- Sniper proxy logic audited and fixed (commit 9736b3f, live as of 2026-09-25 19:58 UTC restart):
  get_sniper_proxy() and rotate_sniper_proxy() indexed into two DIFFERENT proxy orderings
  (Webshare-first candidate order vs. raw load_proxies() file order) while sharing the same stored
  index file — after a 429 triggered rotate_sniper_proxy(), the next pick could land on an
  essentially arbitrary proxy, including possibly the one that just got rate-limited. Fixed via a
  shared _sniper_candidate_list() ordering used by both functions. Verified with
  verify_sniper_proxy_index_fix.py, 2/2 pass, fully mocked — zero live proxy calls/bandwidth spent.
  Oxylabs proxies remain deprioritized-not-excluded here, confirmed consistent with the handoff
  (the known Oxylabs 403 is against the login/OAuth host, not this one).

- Sniper precision tuning (commit 7277995, live as of 2026-09-25 19:58 UTC restart): replaced the
  old flat dual-tier logic (rent < 300€ any mode, 300–350€ single only) with price/surface tiers,
  individual-only (colocation is now NEVER auto-applied, always alert-only), anchored on real
  listings from 2026-09-25:
    rent < 250€            -> snipe, any surface
    250€ <= rent <= 300€   -> snipe only if surface is 12–19 m² (inclusive)
    300€ <  rent <= 350€   -> snipe only if surface > 19 m²
  Fails safe (no auto-apply) if surface data is missing. New env vars: AUTO_APPLY_CHEAP_MAX_PRICE
  (250), AUTO_APPLY_MID_MAX_PRICE (300), AUTO_APPLY_MID_MIN/MAX_SURFACE_M2 (12/19).
  AUTO_APPLY_ANY_MAX_PRICE retired (no longer meaningful). .env updated to match. Verified with
  verify_sniper_tier_logic.py, 21/21 pass, including the real anchor listings.
  Also investigated (per explicit question): does the code correctly handle listings with two
  different prices depending on occupation mode? Yes, confirmed against live API data —
  occupationModes is parsed per-mode already, not flattened. Found a third occupation type CROUS
  uses, "couple" (alongside "alone"/"house_sharing") — correctly excluded from the auto-apply
  decision, though a listing that ONLY offered "couple" pricing could show a mislabeled price in
  the manual Telegram alert (zero real occurrences found in a full nationwide scan; noted,
  deliberately not fixed).
  Also added: record_marseille_listing() — every new Marseille listing (individual AND colocation)
  now gets appended to marseille_listings_data.jsonl (price(s), surface, room type, mode,
  coordinates, link, timestamp) for future tier tuning against real data. Verified with
  verify_listings_data_recording.py, 18/18 pass.

- Weekend bandwidth-conservation pause: built (commit 7277995 + 55463af), then REMOVED entirely
  (commit f083b1d, live as of 2026-09-25 21:xx UTC restart) once its only justification (the
  free-tier 1GB cap) was resolved by purchasing paid Webshare plans same day — no reason to keep
  dead time-boxed logic around. verify_weekend_pause_window.py deleted with it. Bot runs normally
  24/7 again, no special weekend behavior.
- Proxy pools split (commit 4f01930, live as of 2026-09-25 20:55 UTC restart): purchased two paid
  Webshare plans via their API (proxy.webshare.io/api/v2) — 200 datacenter proxies ("Proxy Server",
  $5.98/mo) and 20 static residential proxies ("Static Residential", $6.00/mo), both with 250GB
  bandwidth included (actual usage ~0.6GB/month at the time, so bandwidth is a non-issue at either
  tier). Old free 10-proxy plan auto-cancelled by Webshare on purchase. Datacenter IPs are
  trivially identifiable via ASN lookup as hosting infrastructure — fine for the scouter's plain
  polling, but exactly the signal that can hurt a login flow or multi-page apply session. Residential
  IPs (confirmed real ISPs: Comcast, Orange, Rogers, Telecom Italia, etc via the API's asn_name
  field) look like a real human. Scouter stays on datacenter (proxies.txt); get_auth_proxy() and
  the sniper (_sniper_candidate_list(), shared by get_sniper_proxy()/rotate_sniper_proxy()) now
  source from the new residential pool (proxies_residential.txt, gitignored — real credentials).
  Verified both with fully-mocked tests AND live: a live get_auth_proxy() call picked a real
  residential IP that passed both required CROUS checks first try; a live end-to-end
  crous_apply.py --dry-run succeeded through the new residential proxy, reaching Step 2 in 22.96s
  (faster than the equivalent datacenter-proxy run minutes earlier, 31.22s).
- Posting-activity tracker (commit f083b1d, live): record_posting_activity() now records, once per
  check_and_notify() cycle, the first/last-seen timestamp and count of newly-detected listings each
  Paris-local day, nationwide and Marseille separately, to posting_activity.json (gitignored, like
  other runtime data). Confirmed via live API query that CROUS has no posting-timestamp field at
  all, so "when we first observed it" is the only available signal. Goal: eventually replace the
  untested "quiet at night/on Sundays" assumption behind smart-cadence with real evidence. Verified
  with verify_posting_activity_tracking.py, 7/7 pass, plus a live check_and_notify() run.
- crous_apply.py dry-run open question: considered RESOLVED (explicit call, 2026-09-25) — the
  screenshot evidence (CROUS's own copy: "vous recevrez une réponse dans un délai de quelques
  jours", plus a "Modifier mon formulaire" go-back option on the review screen) is good enough to
  trust. Not proven via network trace, but not being pursued further.
- The "couple" occupation-type mislabeling edge case: considered IRRELEVANT (explicit call,
  2026-09-25) — colocation/couple pricing doesn't matter since only individual sniping is in scope.
  Only follow up if the mere PRESENCE of a third radio option (when "couple" mode exists on a page)
  is ever observed to shift button positions/layout in a way that breaks Playwright's mode
  selection — would need real listing data to diagnose if so.
- AUTO_APPLY_ENABLED flipped to true (commit pending push, .env not tracked by git) — explicit call,
  2026-09-25: "turn it to true, this is it, there is nothing to lose". Sniper will now actually
  attempt DRY-RUN applies on qualifying Marseille listings. AUTO_APPLY_DRY_RUN is still true —
  nothing will be live-submitted.

STILL OPEN, IN PRIORITY ORDER:
1. Push to origin — deliberately deferred until after the upcoming Telegram messaging work, then
   push everything together. ~24 commits local, unpushed.
2. Watch the scouter run for real, now that AUTO_APPLY_ENABLED is true and the weekend pause is
   gone — first real end-to-end live sniper attempt on an actual new Marseille listing hasn't
   happened yet (everything so far has been manual dry-run tests against non-Marseille listings).
   Also watch whether the second Webshare account ("kurosaki ichigo" / pismgcox, still disabled in
   proxies.txt) or the new proxy pools hit any unexpected issues.
3. Night/day request throttling (the PERMANENT version) — proposed, never built. Now has a real
   data source to eventually use (posting_activity.json) instead of the untested assumption —
   revisit once a meaningful number of days have accumulated. THROTTLE ONLY, never a hard stop, per
   original reasoning (a missed rare listing costs more than the bandwidth saved) — though note
   bandwidth is no longer the constraint it was, so the cost/benefit of throttling at all may need
   re-examining once there's real posting-hours data to look at.
4. Cleanup: seen_ids in listings_seen.json and nationwide_seen_ids.json both grow forever, never
   trimmed — low priority. Also move verify_proxy_group_fix.py, verify_fetch_listings_e2e.py,
   verify_city_match_fix.py, verify_tool_id_discovery_fix.py, verify_apply_mode_selection_fix.py,
   verify_sniper_tier_logic.py, verify_sniper_proxy_index_fix.py, verify_auth_proxy_residential_pool.py,
   verify_listings_data_recording.py, verify_posting_activity_tracking.py, proxies.txt.bak-2026-09-25
   to tests/ or delete once no longer needed.

## TASK 1 (DO FIRST): Audit session_keeper.py — READ-ONLY

Do not modify session_keeper.py, crous_auth.py, or crous_watcher.py in this task. Do not restart
session-keeper.service or crous-watcher.service.

1. Read the full contents of /home/ubuntu/session_keeper.py. Report:
   - Does it call crous_auth's auto_login(), or reimplement its own login logic separately?
   - Does it use get_auth_proxy(), or something older/different (raw get_current_proxy(),
     get_sniper_proxy(), hardcoded)? If it predates get_auth_proxy(), it may still lack the
     Oxylabs exclusion and dual-host validation — meaning it could still fail the same way the
     original incident described, on its own independent schedule.
   - What triggers its renewal check, and how often?
   - Does it write session.json atomically (matching commit 43e89f2's fix)? Two uncoordinated
     writers to session.json is a real corruption risk.
   - Does it touch any proxy_manager.py state files (.proxy_index, .sniper_proxy_index,
     .auth_proxy_index, .exhausted_proxy_groups.json)?
2. Trace whether crous_watcher.py's remaining is_session_valid()/auto_login() call sites are
   actually reachable from the automatic hourly path, or only from manual Telegram commands.
   Don't infer from function names — trace actual control flow.
3. Recommend: should session_keeper.py be brought into this git repo and updated to use
   get_auth_proxy() if it isn't already? Should crous_watcher.py's remaining call sites be removed
   if genuinely redundant, or kept as manual-only?

Report findings. Do not implement any fix. Wait for explicit confirmation before changing
anything here — this is HIGH blast-radius (see Standing Rules).

## TASK 2 (DO EARLY, EVERY SESSION): Push to origin

    git push origin main

If this fails (diverged history, auth issue, etc.), stop and report the exact error. Do not
force-push or work around it without asking.

## Standing rules for all future work in this repo

- Redact all secrets (passwords, tokens, API keys) in any output or log you produce.
- Never introduce a silent fallback to an unproxied/direct connection anywhere auth or apply is
  concerned. If all proxies are exhausted/excluded, fail loudly and explicitly.
- Keep the three proxy-selection systems (scouter / auth / sniper) strictly isolated unless a
  task explicitly asks you to touch more than one. State this explicitly in every task summary.
- Commit in small, single-purpose commits as you go, not one large commit at the end.
- Push to origin after every commit, or at minimum every session.
- For ANY claim that something "works," "passed," or "is verified": show the actual raw command
  output (test output, curl output, grep output, diff output) — not a prose summary. This has
  caught real bugs already in this project's history and is non-negotiable.
- NEVER restart crous-watcher.service or session-keeper.service without the user explicitly
  confirming after being shown (a) the exact diff and (b) raw test output. Do not restart as a
  routine "finishing" step.
- Verification effort scales with blast radius:
  - HIGH (full diff review + written tests + raw output required before going live): anything
    touching crous_auth.py, crous_apply.py, session.json, session_keeper.py, or proxy
    rotation/exhaustion/retry/alert-threshold logic; anything unattended.
  - LOW (move faster): pure logging/comment changes, read-only diagnostics, docs, non-functional
    cleanup.
- If you discover anything not mentioned in this handoff (another undocumented process, script
  outside the repo, branch, or anything that changes the risk picture) — STOP, report it clearly,
  and wait for confirmation before proceeding. This is exactly how session_keeper.py should have
  been surfaced earlier, and wasn't.
- Do not make changes beyond what a task explicitly asks for.
- Update this HANDOFF.md's "Status" section as things get fixed or new issues are found, so it
  stays accurate for whoever reads it next.
