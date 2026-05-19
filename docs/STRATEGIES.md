# Swap strategies

The "strategy" knob controls which account `cus` picks as a swap target when an account crosses a threshold. Set in `~/claude-accounts/config.yaml` under `strategy:`. The picker is invoked by `decide_swap()` after the threshold check decides we need to swap *away* from the current account.

## Current strategies (shipping in v0.1)

### `smart` (recommended default)

Designed for the user-stated goal: **never go over 80% of 7-day usage; route on 5-hour dynamics**.

Pipeline:

1. **Filter** — drop blocked accounts (token expired, rate limited, poll error).
2. **Hard 7d cap filter** — among candidates, keep only those with `current_7d_pct < hard_7d_cap_pct` (default 80%). If ALL candidates are at/above the cap, fall back to using all of them (we have to swap somewhere).
3. **Score** each remaining candidate:
   - **Headroom score**: `(100 - 5h%) × five_hour_headroom_weight + (100 - 7d%) × seven_day_headroom_weight`
   - **Burn-soon bonus**: if the candidate's 5h clock is ticking (`current_5h_pct > 0`) AND its 5h window will reset within `burn_window_hours` (default 2), add a bonus proportional to how soon the reset is. Sooner reset = bigger bonus. **Reasoning**: a 5h window that's about to reset has "free" remaining capacity — if we don't use it, it goes to waste. The user articulated this exactly: "if one resets in 1 hour, that one should be burned before the one that resets in 4 hours."
   - **Cold-account penalty** (off by default): if `current_5h_pct == 0`, deprioritize. The 5h clock starts on the first message — an account at 0% has its clock NOT ticking. Future "straddle" strategies (see below) may want to favor keeping at least one cold account in reserve.
4. **Trigger 2 (hard 7d cap on *active* account)** — separately from the picker, `decide_swap()` checks the active account's 7d. If it crosses `hard_7d_cap_pct`, swap immediately regardless of the progressive ladder. This is what enforces "never go over 80%."

Config (`smart_strategy:`):

```yaml
smart_strategy:
  hard_7d_cap_pct: 80                  # the user's hard line on 7d
  burn_window_hours: 2                 # 5h-reset-within window for burn-soon bonus
  burn_soon_weight: 1.0
  five_hour_headroom_weight: 0.6       # weight for 5h in base headroom score
  seven_day_headroom_weight: 0.4
  cold_account_penalty: 0              # off by default; >0 to enable
```

### `headroom`

Simpler version of `smart` — same hard 7d cap filter, but pure weighted headroom score without burn-soon bonus or cold-account handling. Good for users who want a straightforward "pick the one with most room" but with the 7d cap protection.

Config (`headroom_strategy:`):

```yaml
headroom_strategy:
  hard_7d_cap_pct: 80
  five_hour_weight: 0.7
  seven_day_weight: 0.3
```

### `lowest_usage`

Pattern lifted from cux's "balanced." Sorts non-blocked candidates ascending by 7d util; ties broken by 5h util. **No hard 7d cap**, no burn-before-reset logic. Picks whoever has the lowest 7-day percentage.

Trade-off: predictable but blunt. Doesn't consider reset times or the 5h-vs-7d tension. Use when you want the simplest defensible behavior.

### `drain`

Pattern lifted from cux's "drain." Tries to deplete the current account before switching. Pass 1: pick a candidate with both 5h *and* 7d below its own `next_swap_at_pct`. Pass 2 (fallback): any candidate with 5h headroom. Doesn't pick at all if no candidate has headroom.

Trade-off: maximizes "stay on one account" before rotation. Inverse of `lowest_usage`.

### `strict_priority`

Respects the `priority:` field in each account's `accounts:` config entry. Sorts candidates by priority ascending (1 = highest), picks the first one that has headroom on both windows.

Trade-off: deterministic — you'll always rotate in priority order. Use when accounts have a defined preferred-use order (e.g. "burn the secondary first").

### `round_robin`

Cycles through accounts in name order. Ignores usage data.

Trade-off: blindest possible strategy. Useful for testing.

---

## Future strategy directions

These are designed but not implemented. Document, file as GitHub issues, or contribute via PR.

### `staggered_straddle`

**The user's vision** (verbatim): "we eventually can straddle the accounts so there's always one resetting."

**Concept**: actively manage 5h windows across N accounts so that at any moment, *at least one account* is within minutes of resetting. The 5h window resets 5 hours after first message — by staggering "first messages" across accounts, you create a continuously-resetting pool.

**Why it matters**: if you have 3+ accounts and stagger them by ~1.5 hours each, you guarantee that whenever your active account is approaching its 5h cap, another account is *about* to reset (or has just reset) and is fresh. This eliminates the "all accounts hit 5h cap simultaneously" failure mode.

**Implementation sketch**:

1. Treat the daemon's state as a *scheduler*. Track when each account's 5h window started (timestamp of first message in current window) and project when it'll reset (start + 5h).
2. Before swapping, compute the "schedule gap" — the time until the *next* reset across all accounts.
3. If the gap is large (e.g. >2 hours away from a reset), prefer swapping to a COLD account (`current_5h_pct == 0`) to start its clock — this seeds a new staggered window.
4. If the gap is small (some account is about to reset), prefer the burn-before-reset behavior of `smart`.
5. Adjust over time: if the schedule drifts (e.g. all 5h windows trended to reset within 30 minutes of each other), force-swap to a cold account to re-stagger.

**Why deferred**: requires modeling each 5h window's start time (not just its reset time, which we have), needs "schedule health" metrics, and depends on having ≥3 accounts. The current 2-account user-base benefits less.

### `cost_aware`

Different Pro/Max plan tiers have different rate-limit-tier costs. If we can read `claudeAiOauth.rateLimitTier` per account from `.credentials.json` and know each tier's effective cap, the strategy could route based on **tokens-per-dollar** rather than usage percentage.

**Implementation sketch**: extend `AccountUsage` to carry the rate-limit tier; weight headroom score by tier-relative-cost; prefer using cheaper-per-token accounts first.

**Why deferred**: requires knowing each plan tier's actual token limit (Anthropic doesn't publish this explicitly per tier — it's inferred).

### `time_of_day_aware`

If the user has a primary use-case window (e.g. working hours 9-5), use the "best" account during those hours and rotate during off-hours when high-quality availability matters less.

**Implementation sketch**: config knobs for `working_hours_start`/`working_hours_end`. During working hours, use `strict_priority`. Off-hours, switch to `drain` to deplete secondaries.

**Why deferred**: highly user-specific; the user hasn't asked for it.

### `quota_predictive`

Forecast tomorrow's usage based on the past 7 days' burn rate. If projected usage on the active account would breach 80% in the next 12 hours, swap proactively.

**Implementation sketch**: rolling average of recent usage (the 7d-window-delta-per-hour) → linear projection → swap if projected to cross threshold before next reset.

**Why deferred**: needs historical usage tracking (not currently kept beyond what's in `state.json`'s `last_poll_ts`).

### `multi-machine coordination`

If `cus` runs on multiple machines and both poll the same accounts, both daemons could independently decide to use the same swap target, double-burning it.

**Implementation sketch**: shared SQLite over NFS, or a small coordinator service. Each daemon reads/writes a lease on the active account choice; daemons defer to whichever holds the lease.

**Why deferred**: assumes infrastructure (NFS, coordinator) we don't require. The user's setup is currently single-machine.

### `subagent-budget`

When a subagent is active, the daemon already defers swaps (via `subagent_skip`). A smarter version: estimate the subagent's *expected* token burn (from past subagent histories in `tool_use.log`), reserve that headroom, and refuse to swap to an account that wouldn't have room.

**Implementation sketch**: per-subagent-type token budget; subtract estimated budget from candidate headroom score.

**Why deferred**: subagent burns vary wildly by task; modeling this accurately needs per-task signals (not currently captured).

---

## Picking a strategy

| If you want... | Use |
|---|---|
| The user's stated goal (never go over 80% 7d, route on 5h) | `smart` |
| Hard 7d cap but simpler scoring | `headroom` |
| Maximum predictability, no time consideration | `lowest_usage` |
| "Burn one account before touching others" | `drain` |
| Strict ordering across accounts | `strict_priority` |
| Cycling for testing | `round_robin` |

Switch with `cus config --edit` (set `strategy:` value), then `systemctl --user restart cus.service`.

Combine with `thresholds.steps` to control when swaps trigger (the progressive ladder), and `hot_swap.enabled` to control whether live sessions get restarted on swap.
