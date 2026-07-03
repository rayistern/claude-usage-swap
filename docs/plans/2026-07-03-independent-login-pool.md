# Plan: per-account independent-login POOL (supersedes the per-(slot,account) store)

**Status:** design → phased build.
**Date:** 2026-07-03.
**Supersedes:** the per-`(slot, account)` keying from GH #109 Phase 1–3b and PR #117
(`feature/independent-login-rescue-swaps-20260703`). Keeps #117's decision-layer
relaxation idea; replaces its per-slot store/command/predicate with a pool.
**Related:** #104/#103/#3 (refresh-token clobber family), #109 (independent logins),
#116 (lane target-starvation visibility, merged).

---

## 1. Goal

Let the operator log in **N times per account once at onboarding**, creating a
*pool* of independent OAuth credential families per account. Thereafter any lane
that needs to swap onto an account already held by another live mount **borrows a
free family from that account's pool** — a distinct refresh-token family, so no
#104 clobber — with **no per-lane provisioning**. The user's words:

> "I don't want to have to provision lane by lane. I want to log in like 3 times
> per account on onboarding, and afterward have those as backups."

This unlocks *trapped* headroom (an account with room that's currently held by
another mount), which is the structural cause of the "no swaps since lanes"
starvation surfaced in #116. It does **not** add quota — families of one account
share that account's 5h/7d pool — so it complements, not replaces, adding accounts.

## 2. Why a pool (vs the per-(slot,account) store we have)

The current store keys logins by `(account, slot)`: to let slot-7 host merkos you
must `cus login-mount slot-7 merkos`. That is the lane-by-lane matrix the user is
rejecting. A pool keys by **account only** and binds a family to a slot
*dynamically* at swap time, released when the slot goes idle — so one round of
onboarding logins serves every lane.

## 3. Design

### 3.1 Store layout (account-keyed)
```
~/claude-accounts/logins/<account>/family-1/  .credentials.json .claude.json provenance.json
                          <account>/family-2/
                          <account>/family-3/
```
The account's canonical snapshot (`account-<name>/.credentials.json`) is the
implicit **primary** ("family-0"): when the account is not held anywhere, a swap
uses it exactly as today. Backups are `family-1..N`. `provenance.json` keeps
`{account, family_id, minted_ts, source_email, refresh_fp}` (unchanged shape,
`slot` → `family_id`).

### 3.2 Lease tracking (which family is in use)
Refresh tokens rotate hourly, so fingerprint-matching a live mount to a store
family is racy. Instead record the lease explicitly on the slot:
`state.slots[<slot>].login_family = "<account>/family-<id>"`.
- **Set** when a swap installs a pooled family into the slot.
- **Cleared** when the slot goes idle/gc'd, or swaps onto a different account.
- **Free family** = a `family-<id>` for the account that no *live* slot leases.
- Claim order: **lowest free index** (deterministic, configurable rule later).

`leased_families(account, state)` derives from live slots' `login_family`; a family
is claimable iff it exists on disk, parses with a refresh token, and is unleased.

### 3.3 Provisioning command
`cus login-mount <account>` (slot arg removed / optional-ignored), repeatable:
- scaffolds `logins/<account>/family-<next>/`, prints the `CLAUDE_CONFIG_DIR=… claude`
  login command (mirrors `cus relogin`);
- `--finish` verifies the login landed on `<account>` (duplicate-identity guard,
  2026-07-01) and records provenance;
- `--from-existing` seeds `family-1` from the account snapshot (no browser) — but
  a family seeded from the snapshot is NOT independent from it, so it's only safe
  as the single primary; SOS flags it if two live mounts end up sharing a family.
- `--list` shows each account's pool depth and per-family expiry/identity.

### 3.4 Swap install (claim a family)
`swap_install_source(target, slot, snapshot, config, state)`:
- target not held by another live mount → **primary snapshot** (today's path);
- target held + gate on → **lowest free family** from the pool, record the lease;
- target held + no free family → return a sentinel that makes the caller HOLD
  (never a clobbering copy). SOS surfaces "pool exhausted for <account>".

On swap-out / save-back, the rotated family is written back to its **store family
dir** (extend the existing `saveback_to_login_store`), never to the account snapshot.

### 3.5 Decision + reactive rescue
Both proactive (`decide_slot_swaps`) and reactive (`check_rate_limit_reactive_per_session`)
relax their cross-mount exclusion: an excluded (held) account X is a legal target
for slot S iff `has_free_login_family(X, state)` (gate on). Replaces #117's
per-slot `has_independent_login(X, S)` predicate. A genuinely-free account is still
preferred (the fan-out re-pick tries the un-held pool first).

### 3.6 SOS / status
- **Pool exhausted:** a lane needs to rescue onto X but every family is leased →
  urgent SOS "provision another login for X (`cus login-mount X`)".
- **Near / past expiry** per family (existing expiry logic, per-family).
- `cus status` shows pool depth per account (e.g. `merkos [pool 2/3 free]`).

### 3.7 Config
```
independent_logins:
  use_independent_logins: false   # Phase 2 gate (unchanged)
  pool_size: 3                    # default backups/account for onboarding guidance + SOS "provision more" target
  refresh_token_ttl_days: 30
  warn_expiry_within_days: 5
```
`pool_size` guides onboarding + the pool-exhaustion SOS; it does NOT hard-cap
families (you can log in more).

## 4. Backward-compat & safety
- Gated on `use_independent_logins` (default **off**) → swap path stays bit-for-bit
  the copy path; entire pool machinery inert until provisioned + enabled.
- The old per-`(account, slot)` store is empty in production (nothing provisioned),
  so there is no live data to migrate. Old path helpers kept as thin shims or
  removed with their now-superseded tests.

## 5. Phasing (test at each)
- **P1 — store + config:** pool path helpers, `list_login_families`,
  `free_login_family`/`has_free_login_family`, lease helpers, `pool_size` config.
- **P2 — provisioning:** rework `login_mount_cmd` to account-keyed repeatable;
  `--list` pool view.
- **P3 — swap install + save-back:** claim lowest-free family; lease set/clear;
  store save-back per family; hold-on-exhaustion.
- **P4 — decision + reactive rescue:** relax exclusion via `has_free_login_family`
  in `decide_slot_swaps` AND `check_rate_limit_reactive_per_session`.
- **P5 — SOS + status + docs:** pool-exhaustion + expiry SOS, status pool depth,
  README/runbook, retire superseded per-slot tests.

Each phase: full `tests/` suite green + `ruff` baseline before the next.
```
```
