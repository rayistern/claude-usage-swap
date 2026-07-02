# Phase 0 login runbook (GH #109) — operator handoff + handback

**For:** whoever runs the confirmation experiments (the parts a script can't drive
alone, because they need an interactive `claude /login` browser flow).
**Goal:** turn the four "To CONFIRM" items in
`docs/plans/2026-07-02-seamless-swap-independent-logins.md` §2 into measured facts,
so Phase 2 (wiring the independent-login store into the swap path) rests on data,
not guesses.
**Safety:** everything below runs in throwaway scratch dirs and **never touches
`~/.claude/` or the live `cus` daemon/state.** You can stop anytime; nothing here
changes production. Harness: `experiments/109-phase0/phase0_confirm.py`.

You do NOT need this branch installed. The harness is stdlib-only; just run it
from the repo. The `cus login-mount` command is not required for these
experiments (it targets the real store; these use scratch dirs) — though you may
use it instead if you prefer to exercise the real path.

---

## Setup (2 min)

```bash
cd ~/repos/claude-usage-swap/.claude/worktrees/poll-throttle-and-per-model-20260702
H=experiments/109-phase0/phase0_confirm.py
mkdir -p ~/p0
# Instant sanity check — no login needed, proves the filesystem mechanics
# behind the whole design (share-the-file schemes sever on first refresh):
python3 $H files      # expect all four booleans true, rc 0
```

Pick accounts you can log into. **Experiment A needs one account you can log in
to ≥3 times; Experiments B/C need two different accounts (A and B).** Any pool
accounts are fine — using one at its usage cap is OK (auth still refreshes; see
the note at the bottom).

---

## Experiment A — >2 independent logins coexist (§2 item 3) — DO THIS FIRST

This is the load-bearing generalisation of the #109 two-login finding: can ONE
account back **three or more** live mounts at once without any login family
invalidating another?

```bash
# Log the SAME account into three scratch dirs (interactive: browser + paste,
# then /exit each). Log in as the SAME account X all three times:
CLAUDE_CONFIG_DIR=~/p0/a1 claude      # /login as X, then /exit
CLAUDE_CONFIG_DIR=~/p0/a2 claude      # /login as X, then /exit
CLAUDE_CONFIG_DIR=~/p0/a3 claude      # /login as X, then /exit

# Round-robin liveness for 3 rounds. PASS = every dir stays alive AND keeps a
# DISTINCT refresh-token fingerprint (none blanks another):
python3 $H multi ~/p0/a1 ~/p0/a2 ~/p0/a3 --rounds 3
```

**Record:** did all three survive 3 rounds? Were the three final `refresh_fp`
values distinct? Copy the final fingerprints block.

---

## Experiment B — read cadence (§2 item 1)

Does an in-place credential swap take effect on an **already-running** session at
once, or only at its next hourly token refresh? (Maintainer's baseline: global
in-place swaps work without restart — reproduce and pin the exact behavior.)

```bash
mkdir -p ~/p0/mount ~/p0/loginB
CLAUDE_CONFIG_DIR=~/p0/loginB claude   # /login as account B, then /exit
CLAUDE_CONFIG_DIR=~/p0/mount  claude   # /login as account A — LEAVE THIS RUNNING
```

In the **running A session**, ask: `which account/email am I? (or run /usage)`.
Then, from another terminal, swap B's creds into the live mount in place:

```bash
cp ~/p0/loginB/.credentials.json ~/p0/mount/.credentials.json
```

Back in the **same still-running A session** (do not restart), ask again.

**Record:** did it answer as **B on the very next turn** (re-reads per request) or
stay **A until a refresh** (picks up only on refresh)? Note any delay.

---

## Experiment C — drift write-back (§2 item 2)

Does a live session write its **own** refreshed token back over the live file
(the #3 drift path)? This decides whether save-back must be per-(mount,account).

```bash
python3 $H alive ~/p0/mount        # note inode + mtime + refresh_fp (BEFORE)
# leave the A session from Experiment B open + idle for ~70 minutes
python3 $H alive ~/p0/mount        # AFTER — compare
```

**Record:** did `refresh_fp` change and `inode` change after ~70 min? That means
the live session refreshed and wrote a new token family to disk. Note the
observed refresh interval.

---

## Experiment D — idle-login expiry (§2 item 4) — spans days

How long does a stored, idle independent login stay refreshable? (Phase 1 assumes
30 days for `refresh_token_ttl_days` — this measures the real number.)

```bash
python3 $H ledger stamp acctX-a1 ~/p0/a1     # stamp today (reuse an Exp-A dir)
# ...come back after several days / a week and DON'T use ~/p0/a1 meanwhile...
python3 $H ledger check                       # re-probes; prints age + alive
```

Re-run `ledger check` periodically. **Record:** the largest age at which it was
still `alive=True`, and the first age where it failed.

---

## Handback path

Paste the filled template below into **one** of these, and hand it back:

1. **Best:** annotate `docs/plans/2026-07-02-seamless-swap-independent-logins.md`
   §2 "To CONFIRM" with a dated block (`> **Phase 0 result 2026-07-XX:** ...`) —
   **annotate, don't rewrite** the existing text (preserve-the-log). Commit on
   this branch (`feature/seamless-swap-independent-logins-20260702`).
2. **Or:** drop it in a comment on PR #110 or issue #109.
3. **Or:** just paste it back to me in chat and say "wire Phase 2 to this" — I'll
   fold the numbers into the config defaults + the Phase 2 swap-path change and
   flip anything the results justify (e.g. set `refresh_token_ttl_days` to the
   measured expiry, decide whether save-back needs the per-(mount,account) split).

```
### Phase 0 results — 2026-07-__

A. >2 logins (multi): survived N rounds? [Y/N]   fingerprints distinct? [Y/N]
   final fps: <paste>
B. read cadence: [per-request re-read | only-on-refresh]   delay: <...>
C. drift write-back: refresh_fp changed after ~70m? [Y/N]  inode changed? [Y/N]
   observed refresh interval: <...>
D. idle expiry: still alive at max age = <N days>;  first failure at = <N days | not yet>
Notes / surprises: <...>
```

**What each result changes in Phase 2:**
- A fails → the whole approach is wrong; stop and re-plan (do not enable the gate).
- B "only-on-refresh" → an in-place group swap is not instant; document the lag and
  gate deferrable swaps on it.
- C "yes, drifts" → `classify_live_creds_owner` + save-back MUST become
  per-(mount,account) (Phase 3) before enabling the gate, or families reconcile.
- D → set `refresh_token_ttl_days` to the measured number; drives the sos nudge
  and any re-login prompting.

---

### Note on cap-driven failures
A probe can exit non-zero because the account is **at its usage cap** even though
the **OAuth refresh succeeded** (measured in #109). The harness reports both the
process rc and the credential facts, and treats "the creds file still holds a
refresh token afterwards" as the liveness signal — so a cap failure is not misread
as an auth failure. If `alive` returns rc 1 but `refresh_fp_after` is present, the
login is fine.
