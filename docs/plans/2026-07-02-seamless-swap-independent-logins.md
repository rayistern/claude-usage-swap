# Handoff: seamless account swapping via per-mount independent logins

> **Phase 1 + Phase 0 harness built 2026-07-02** on `feature/seamless-swap-independent-logins-20260702` (branched off `main`; the "mounts" of this handoff are the existing per_session **slots**). Shipped, additive and non-breaking (swap path untouched; gate defaults off):
> - **Per-(slot, account) store** at `~/claude-accounts/logins/<account>/<slot>/` — independent OAuth blob + `provenance.json` (minted_ts, source_email, refresh-token fingerprint) + the identity `/login` wrote. Helpers: `login_store_*`, `has_independent_login`, `read/write_login_provenance`, `login_expiry_state`, `list_provisioned_logins`, `_slots_missing_logins`.
> - **`cus login-mount <slot> <account>`** — lazy, two-step (print/`--exec` the login command, then `--finish`), mirrors `cus relogin`. `--finish` **refuses a wrong-account login** by comparing the store dir's `oauthAccount` against the account's canonical `.claude.json` (the 2026-07-01 duplicate-identity guard), `--force` overrides; `--list` shows all provisioned logins.
> - **Surfacing** — `cus status` annotates each slot (`[indep-login ✓]` / `[copy]` / `[indep-login MISSING]`), stays invisible until the feature is in use. `cus sos` flags expired / near-expiry / wrong-account provisioned logins, and (gate-on only) occupied slots lacking a login.
> - **Config** — `independent_logins.{use_independent_logins:false, refresh_token_ttl_days:30, warn_expiry_within_days:5}`. `use_independent_logins` is the Phase 2 gate. A **Phase 2 seam comment** marks the exact swap-path install site (`_execute_swap_locked`, the `atomic_copy(target_creds, live_creds_path)`).
> - **Tests** — `tests/test_independent_logins.py` (17 tests); full suite 235 passing; `cus.py` ruff at baseline.
> - **Phase 0 harness** — `experiments/109-phase0/` (`phase0_confirm.py` + README): automated `multi` (>2-login independence, §2 item 3), `ledger` (idle-expiry, item 4), `files` (filesystem-severing demo, §2 finding #2), and printed interactive procedures for read-cadence (item 1) + drift-writeback (item 2). **Phase 0 experiments not yet RUN** — they need interactive `/login` flows; results still owed in §2 below.
>
> **Not built (at first commit):** Phase 2 (wire the store into the swap path behind the gate), Phase 3 (lift #104 refusal for independent families + make `classify_live_creds_owner`/save-back per-(mount,account) aware), Phase 4 (expiry management + docs + flip default). Line refs in §3/§8 below are from the pre-`main` revision; current touchpoints: `classify_live_creds_owner` ≈ L2779, the two #104 refusals in `switch` and `_launch_prepare`, swap install site in `_execute_swap_locked`.
>
> **Update — Phase 2 + Phase 3a + bootstrap built 2026-07-02 (same session, gate still off):**
> - **Phase 2 (install source):** `swap_install_source()` chooses, in `_execute_swap_locked`, the (target, slot) independent login when the gate is on and one is provisioned, else the shared snapshot. Gate off ⇒ bit-for-bit today's copy path.
> - **Phase 3a (store-aware save-back):** on swap-out of a slot whose outgoing account runs an independent login for that slot, `saveback_to_login_store()` writes the rotated family back to the **store**, never through `classify_live_creds_owner` — so the shared account snapshot is never clobbered (handoff §5.5). Verified end-to-end by test.
> - **Bootstrap (answers "can't you pull from what's on the machine?"):** `cus login-mount <slot> <account> --from-existing` seeds the store from the account's on-disk snapshot — **no login** — for accounts that back a single mount. Only the **2nd+ simultaneous** mount of one account needs a real `/login` (only the browser flow mints a distinct refresh-token family). `duplicate_login_families()` + a critical SOS condition catch two mounts sharing one family (the precise clobber invariant).
> - Tests: `tests/test_independent_logins.py` now 23; full suite **241 passing**; ruff baseline.
> - **Still not built at that point:** Phase 3b (lift the #104 refusals for provably-independent families), Phase 4 (idle-expiry management, docs/README, flip the default). **Phase 0 experiments still not run** — but only the 2nd+-mount accounts need a login, so the burden is small (see `docs/plans/2026-07-02-phase0-login-runbook.md`).
>
> **Update — lanes (Track A) + Phase 3b built 2026-07-02 (same session; both gated, defaults preserve today's behavior).** After discussion, the design grew a second, simpler model the user prefers, and the two now COMPOSE:
> - **Lanes (Track A, `per_session.lane_sharing`, default off):** a lane is a config dir that hosts ≥1 session, swapped in place like global. `cus launch <account>` JOINS the live lane already holding that account (share its dir/login, swap together) rather than minting a second mount. **One account per live lane** stays the invariant (#104) — lanes satisfy it by sharing, so **no second login is ever needed** for the common "sessions spread across accounts" case. Seeded free from the existing login (`--from-existing`). This is the user's default preference ("I'm not logging in").
> - **Phase 3b (`--lane` + #104 lift, gated by `use_independent_logins`):** the escape hatch for running ONE account on TWO *independently-swappable* lanes. `cus login-mount <free-slot> X` then `cus launch X --lane <free-slot>` lifts the #104 refusal *only* because (free-slot, X) has its own provisioned independent family; the install uses it (Phase 2), so no clobber. Without a provisioned login it still refuses; `--force` still overrides (and still clobbers).
> - Composition: **lanes = default no-login sharing; independent logins = opt-in for independent same-account lanes.** The #104 guard is now a 3-way decision (join / refuse / allow-independent).
> - Not built: **daemon-side** auto-double-booking onto independent families (the daemon stays conservative — never auto-doubles); Phase 4 (expiry mgmt, docs/README, flipping defaults). Tests: `tests/test_independent_logins.py` now 29; full suite **247 passing**; ruff baseline.
> - Pre-existing bug noticed (not fixed here): duplicate `acquire_slot` def on `main` (L1515 dead, shadowed by the locked L1648) — filed as a separate GH issue.

**Status:** design handoff — Phase 1 + Phase 0 harness built; Phases 2–4 pending.
**Date:** 2026-07-02.
**Related issue:** [#109](https://github.com/rayistern/claude-usage-swap/issues/109) (experiment write-up).
**Related bugs:** #104 (closed), #103, #3, #75, #77, #79, #81 — the refresh-token-clobber family.

---

## 1. Goal (one paragraph)

Let the daemon rotate which account a set of live Claude Code sessions uses **without logging any session out**, including the case where the *same* account is used by more than one mount at once. Today this is blocked: cus copies a single credential blob into each place an account is used, and Anthropic rotates the refresh token on every refresh, so the copies diverge and one gets blanked (the #104 / #103 / #3 clobber family). The fix proven out below is to give each mount its **own independent login** for the accounts it hosts, so there is no shared token family to clobber.

## 2. What we VERIFIED experimentally on 2026-07-02

These are measured results, not assumptions. Harness + full transcript are in issue #109; the throwaway test dirs were `~/refresh-test/{A,B}` (two independent logins of the same account, `rayistern@trisso.com`).

1. **Anthropic issues an independent refresh-token family per login.** Two separate `claude /login` flows for the *same* account produced two different refresh tokens that each refreshed successfully across two alternating rounds — neither invalidated the other. → One account CAN safely back multiple independent logins simultaneously. (This is the load-bearing finding.)
   - Hardening: every forced refresh returned `rc=1` because the account was at its usage cap, but the OAuth refresh itself succeeded (new access token minted, `expiresAt` renewed). Auth success and usage-cap are orthogonal; the "alive" signal was real.

2. **Sharing one creds file across two mounts via filesystem links does NOT work.** Claude Code writes credentials by **atomic write-to-temp + rename-over-path**, which swaps the inode at that path:
   - **Symlink** → replaced by a regular file on the first refresh; the shared target never updates.
   - **Hardlink** → severed on the first refresh (path gets a new inode; the other name keeps the old one). Measured: inode `262338` → `262479`; the other name stayed frozen at the old token.
   - Consequence: any "share the file" scheme (symlink / hardlink / file bind-mount) breaks **silently on the first refresh (~hourly)**, which is worse than failing up front.

3. **The only way multiple sessions genuinely share one creds file is to share the whole config dir** (same `CLAUDE_CONFIG_DIR`) — that is exactly what global mode is, and it works. Credentials live *inside* the config dir at `$CLAUDE_CONFIG_DIR/.credentials.json`; there is no separate creds-path override.

### To CONFIRM (not yet verified — do not assume either way)
- Claude Code's credential **read cadence**: does a live session pick up an in-place creds swap immediately (re-reads per request) or only on its next token refresh? This determines whether an in-place group swap takes effect on already-running sessions at once. (The maintainer's operational experience is that global in-place swaps work without restart — treat that as the baseline to reproduce and document precisely, not to re-derive.)
- Whether/when a live session writes its *own* refreshed token back over the live file (the #3 "drift" path), and how that interacts with an independent-login store.
- More than 2 concurrent independent logins per account (only 2 were tested).
- 30-day refresh-token expiry for stored independent logins that sit idle between uses.

## 3. Current architecture (as-is, for the builder)

- **Account dir**: `~/claude-accounts/account-<name>/` — a valid `CLAUDE_CONFIG_DIR`. The account's canonical credential snapshot is `account-<name>/.credentials.json` (a real file). Shared subdirs (`settings.json`, `skills`, `hooks`, `plugins`, `projects`, `commands`, `session-env`, …) are **symlinked to `~/.claude/`**.
- **Slot dir**: `~/claude-accounts/slot-N/` — a per-session `CLAUDE_CONFIG_DIR`. Structurally identical to an account dir (same shared symlinks). **The only private, account-defining file is `.credentials.json`, which is currently a regular-file COPY of some account's blob.** That copy is what makes the slot "be" that account.
- **Mount topology (`cus mode`)**: `global` (one shared `~/.claude` mount that all bare sessions follow; swaps rewrite that one file) vs `per_session` (each session gets a slot dir). The machine was in `global` at handoff time even though slots existed (mixed: some sessions bare on the shared mount, some launched into slots).
- **Swap primitives**: `cus switch <name>` (atomic rewrite of the shared mount creds), `cus auto-swap` (strategy-picker force swap), `cus launch [account]` (create a slot, install an account, run claude), `cus slot {create,gc,list}`, `cus pool`.
- **The #104 refusal** lives in the switch/launch path: it refuses to place an account on a mount when that account is already live on another mount, because two copies of one token family clobber on rotation.
- **Save-back / drift classification**: `cus.py` `_classify_saveback` (~L1548–1622) matches a live creds file to an account snapshot by refresh token, to decide whether to save rotated tokens back. **This assumes one token family per account** and will need to become per-(mount, account) aware (see below), or it will "reconcile" two independent families into one and re-introduce the clobber.

## 4. Proposed design

**Model:** keep sessions pointed at stable config dirs ("mounts" — could be the existing slot dirs, or per-group dirs). Rotate accounts by writing the correct credential blob into a mount's live creds file in place (the daemon already does this shape of operation). The new ingredient: the blob written in is a **per-(mount, account) independent login**, not a copy of one shared per-account blob. Because each mount holds its own login family for a given account, two mounts on the same account no longer clobber each other on refresh.

This composes two things:
- **In-place account swap within a mount** (restart-free; the existing global mechanism).
- **Per-mount independent logins** (the #109 finding) → makes the same account safe across multiple mounts.

## 5. Components to build

1. **Per-(mount, account) credential store.** New on-disk store, e.g. `~/claude-accounts/logins/<account>/<mount-id>/.credentials.json`, holding an independent login blob for each (mount, account) pair, distinct from the single per-account canonical snapshot. Provenance metadata (when minted, which mount) alongside.

2. **Independent-login provisioning flow.** `/login` is interactive (browser + paste-back) and cannot be automated. Provide a command that walks the operator through establishing an independent login for an account into a specific mount, e.g.:
   - `cus login-mount <mount-id> <account>` → prints/execs `CLAUDE_CONFIG_DIR=<mount-dir> claude` for the `/login`, then captures the resulting `.credentials.json` into the per-(mount, account) store and records provenance.
   - Because this is O(mounts × accounts) interactive logins, design for **lazy provisioning**: mint an independent login the first time a mount is asked to host an account, and cache it. Surface clearly (via `cus sos`/status) which (mount, account) pairs still need a login.

3. **In-place group swap using the store.** When rotating a mount to account X, write the (mount, X) independent blob into that mount's live `.credentials.json`. Never write a *shared* per-account blob into more than one live mount (that's the clobber). Keep the write atomic + backed up (per #79).

4. **Lift the #104 duplicate-mount refusal when families are independent.** Detect independence: the two mounts' refresh tokens are from separate logins (different lineage), not copies. Allow same-account-on-multiple-mounts iff each mount uses its own independent login; keep refusing when it would be a copy.

5. **Make save-back / drift per-(mount, account) aware.** `_classify_saveback` must treat each (mount, account) family as its own lineage, so a rotation in one mount is saved back to *that* mount's store entry and never reconciled against another mount's independent family. Otherwise the reconciliation re-introduces the exact clobber this design removes.

6. **Docs.** Update `docs/ARCHITECTURE.md` (mount topology) and `docs/TROUBLESHOOTING.md` (#104 entry) once shipped. Add a `cus` command reference for the new provisioning + status.

## 6. Suggested implementation phases

- **Phase 0 — confirm the "to CONFIRM" list in §2** (read cadence, drift-write behavior, >2 logins, expiry). Cheap experiments in the style of the #109 harness. Write results back into this doc. Do this first; it de-risks everything below.
- **Phase 1 — per-(mount, account) store + lazy provisioning command** (`cus login-mount`, store layout, provenance, status surfacing). No swap-path changes yet.
- **Phase 2 — teach the swap path to write from the store**, gated behind a config flag so it's opt-in and non-breaking; leave the copy path as default until proven.
- **Phase 3 — lift the #104 refusal for independent-family mounts; make `_classify_saveback` per-(mount, account) aware.**
- **Phase 4 — expiry management** for idle stored logins (detect near-expiry, prompt re-login via sos), docs, and flip the config default once stable.

## 7. Non-goals / caveats (state these to avoid scope confusion)

- **This does not add capacity.** Multiple independent logins of one account share that account's 5h/7d limits. The win is *no-logout swaps and safe multi-mount*, not more quota. Capacity still only comes from rotating across *different* accounts.
- **This is not "move a live session to a different config dir."** Sessions stay in their mount; only the account *inside* the mount changes. Moving a session to a different *dir* is out of scope (that would need a relaunch).
- **Ship backward-compatible.** New store + swap path behind a flag; existing copy-based behavior stays the default until Phase 4. No breaking changes to on-disk layout or CLI without explicit sign-off.

## 8. References

- Experiment + verdict: issue #109; harness style inlined there (`verdict.py`).
- Code touchpoints in `cus.py`: `_classify_saveback` (~L1548–1622, refresh-token lineage matching), `poll_account_usage` (L900+, creds/token shape), account-dir-as-`CLAUDE_CONFIG_DIR` convention (L124–125), swap/launch path + #104 refusal, `slot` subcommands.
- Bug family this resolves/relates to: #104 (closed — the refusal this makes solvable), #103, #3, #75, #77, #79, #81.
