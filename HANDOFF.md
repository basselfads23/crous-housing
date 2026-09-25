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

STILL OPEN, IN PRIORITY ORDER:
1. TASK 1 below — audit session_keeper.py. Blocks fully trusting the auth fix.
2. TASK 2 below — push to origin. Pure data-loss risk.
3. Watch the scouter fix run a full day+, especially whether the second Webshare account
   ("kurosaki ichigo" / pismgcox) eventually hits its own 402.
4. crous_apply.py audit — nobody has reviewed this file. Do this before ever enabling live
   (non-dry-run) auto-apply.
5. Sniper proxy logic — separately scoped, not yet audited.
6. Night/day request throttling — proposed, never built. Needs real activity-logging data FIRST
   (timestamp + day-of-week per listing seen) before changing cadence — "no listings at night/on
   Sundays" is currently an assumption. THROTTLE ONLY, never a hard stop, until weeks of data say
   otherwise (a missed rare listing costs more than the bandwidth saved). Do NOT touch Saturday —
   French government services can be open Saturdays.
7. Cleanup: verify_proxy_group_fix.py, verify_fetch_listings_e2e.py, proxies.txt.bak-2026-09-25
   are recent test/backup artifacts — move to tests/ or delete once no longer needed.

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
