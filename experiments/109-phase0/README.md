# Phase 0 confirmation harness (GH #109)

Turns the four **"To CONFIRM"** items in
`docs/plans/2026-07-02-seamless-swap-independent-logins.md` §2 from assumptions
into measured facts, **before** the swap-path change (Phase 2) is built on them.
Do this first — the plan says so: "it de-risks everything below."

Self-contained, stdlib-only, and it never touches `~/.claude/` or live `cus`
state — everything runs against throwaway scratch config dirs, same posture as
issue #109's inlined `verdict.py`.

## The four items and how each is covered

| # (§2) | Question | Coverage |
|---|---|---|
| 1 | Does a live session pick up an **in-place creds swap** at once, or only on next refresh? | Human procedure (`--procedure`) — needs a long-lived interactive session |
| 2 | Does a live session **write its own refreshed token back** over the live file (the #3 drift path)? | Human procedure (`--procedure`) — watch a session across one refresh (~70 min) |
| 3 | Do **>2 independent logins** of one account coexist without invalidating each other? | Automated: `multi` |
| 4 | How long does a **stored, idle** independent login stay refreshable (the 30-day guess)? | Automated ledger: `ledger stamp` / `ledger check` (spans days) |

## Commands

```bash
# Item 3 — generalise the #109 two-login finding to N logins of ONE account.
# First, log into N scratch dirs as the SAME account (interactive, once each):
#   CLAUDE_CONFIG_DIR=~/p0/a1 claude   # /login as account X, /exit
#   CLAUDE_CONFIG_DIR=~/p0/a2 claude   # /login as account X, /exit
#   CLAUDE_CONFIG_DIR=~/p0/a3 claude   # /login as account X, /exit
python3 phase0_confirm.py multi ~/p0/a1 ~/p0/a2 ~/p0/a3 --rounds 3
# PASS = all dirs stay alive across rounds and keep DISTINCT refresh lineages.

# Item 4 — idle-expiry ledger. Stamp now, re-check days later.
python3 phase0_confirm.py ledger stamp acctX-a1 ~/p0/a1
# ...wait days/weeks...
python3 phase0_confirm.py ledger check

# Building block: one liveness probe of any config dir.
python3 phase0_confirm.py alive ~/p0/a1

# Filesystem-mechanics demo for §2 finding #2 (no OAuth, instant):
python3 phase0_confirm.py files

# Items 1 and 2 — the interactive procedures:
python3 phase0_confirm.py --procedure
```

## Recording results

Write measured numbers back into the plan doc §2, **annotating** under
"To CONFIRM" (dated note; do not rewrite prior text — preserve-the-log per the
repo convention). Once an item is confirmed, note it so Phase 2 can rely on it.

## Notes / gotchas

- A probe can exit non-zero because the account is **at its usage cap** even
  though the **OAuth refresh succeeded** (measured in #109). `alive` reports both
  the process rc and the pre/post credential facts so a cap failure isn't misread
  as an auth failure — liveness is "the creds file still holds a refresh token
  afterwards," not "rc == 0."
- `multi` needs the initial `/login` flows done by a human (browser + paste-back);
  only the liveness rounds are automated (`claude --print` is drivable).
- The ledger persists to `idle_expiry_ledger.json` beside the script.
