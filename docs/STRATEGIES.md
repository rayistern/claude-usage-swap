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
   - **Reset-proximity preference** (`reset_proximity_weight`, default 8.0; GH #42): beyond the `burn_window` cutoff above, a small continuous term spanning the full 5-hour horizon that still leans selection toward the earlier-resetting account. Scaled small so it only breaks genuine ties — the headroom score (0–100) and burn-soon bonus dominate. Set to 0 to disable.
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
  reset_proximity_weight: 8.0          # full-horizon lean toward earlier-resetting account (GH #42); 0 = off
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

## Proactive triggers (strategy-independent)

These fire in `decide_swap()` regardless of which `strategy:` you pick, because they're about *when* to swap, not *which account* to pick.

### `burn_before_reset` (GH #42)

The ladder / hard-cap / reactive triggers only fire when the **active** account climbs toward its own cap — they never move you **onto** an account whose 5-hour window is about to reset. But a 5h window is "use it or lose it": when it rolls over, accrued usage resets to zero. So the best place to be just before a reset is **on the about-to-reset account**, draining its remaining capacity, while leaving the far-from-reset account's window pristine for later. This is the trigger half of the user request "we should have always been on the closer-to-[reset] session"; the `smart` strategy's burn-soon bonus + `reset_proximity_weight` are the *selection* half.

Fires only when the ladder *wouldn't* (active still below its step), so the two never compete. Guardrails (all must hold):

- a usable target exists (reuses the active `strategy`'s picker + all its filters — never lands on a blocked / over-7d-cap / about-to-re-trip account);
- the target's 5h window resets within `reset_window_minutes`;
- the target still has unused 5h capacity worth draining (`100 - 5h% >= min_candidate_headroom_pct`);
- the active account's 5h window resets at least `min_reset_gap_minutes` **later** than the target's (or its clock isn't ticking at all).

Always fires at **tier 1** (wait-for-Stop, defer if cache warm) so it never interrupts an in-flight turn — see the 2026-05-19 incident where a botched mid-turn relaunch burned ~4% of a 5h window.

Config (`burn_before_reset:`):

```yaml
burn_before_reset:
  enabled: true                 # walk-back: set false
  reset_window_minutes: 30      # target's 5h must reset within this
  min_reset_gap_minutes: 60     # active's 5h must reset >= this much later than target's
  min_candidate_headroom_pct: 15 # target's unused 5h headroom must exceed this
```

### `defer_swap_near_5h_reset` (GH #51)

The complement of `burn_before_reset`: where that one swaps you **onto** a soon-to-reset account, this one declines to swap **away** from the active account when *its own* 5h window is about to reset anyway. At ~70% climbing toward the ladder step with the 5h reset minutes away, swapping is pointless churn — the reset drops 5h back to ~0 on its own and `maybe_reset_thresholds` resets the ladder. "We could have just easily waited" (user, 2026-05-28).

Applies to the **ladder path only**. Defers (waits, swaps nothing) when all hold:

- the 5h window is the **sole** window at/over the threshold — a reset zeroes 5h but leaves 7d untouched, so if 7d is also over we'd just re-trip next cycle (don't defer);
- the 5h window resets within `wait_window_minutes`;
- 5h is **below** `max_defer_pct` — if it's already near the cap, don't gamble on waiting (usage could spike to a real 429 before the reset).

Hard-7d-cap and reactive-429 bypass this (emergencies; 7d doesn't reset soon). The reactive-429 hook is the backstop if usage spikes past the cap before the reset lands.

Config (`defer_swap_near_5h_reset:`):

```yaml
defer_swap_near_5h_reset:
  enabled: true                 # walk-back: set false
  wait_window_minutes: 15       # defer the ladder swap only if 5h resets within this
  max_defer_pct: 90             # ...unless 5h is already this high (too close to cap to risk waiting)
```

---

## Future strategy directions

These are designed but not implemented. Document, file as GitHub issues, or contribute via PR.

### `llm` (tracked: GH #43)

Hand the swap decision to an LLM instead of a hand-coded scoring function. The operator expresses policy in **natural language** (`llm_strategy.policy`) and the model weighs richer/fuzzier context — per-account usage + reset timestamps, ladder step, recent swap history, live tmux sessions and their activity, poll staleness — into a schema-constrained `{target, tier, reason}`.

**Must degrade gracefully**: API down/slow/over-budget → fall back to `fallback_strategy` (deterministic); the daemon never blocks or crashes on the call. **Guardrails win**: the LLM output is advisory — the same hard filters that protect the deterministic pickers (hard 7d cap, blocked-account exclusion, hysteresis, would-immediately-re-trip) still gate the final swap. Cheap on Haiku + prompt-cached; consider `max_decision_age_seconds` to avoid calling when nothing changed. Full design in GH #43.

**Why deferred**: adds a network dependency + API cost to a currently fully-offline-deterministic daemon, and needs the graceful-degradation + guardrail-validation scaffolding before it's safe to ship.

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
