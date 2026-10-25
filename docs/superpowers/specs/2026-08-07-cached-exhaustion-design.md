# Honor cached per-model exhaustion until the 7-day window resets

## Problem

An account whose per-model weekly usage is at 100% stays eligible as a swap/launch
target whenever its last poll failed. `_max_model_weekly_from_acct` returns `0.0`
("no cap") for any account where `_pct_is_unknown(acct, "current_7d_pct")` holds —
i.e. `rate_limited`, `token_stale`, `token_expired`, or `poll_error` — so a cached
`Fable=100%` does not exclude anything.

Observed 2026-08-06: two accounts both carried `Fable=100%~` while remaining
admissible on the per-model gate.

The `0.0` is deliberate. It fixes a 2026-07-05 incident where a `token_stale`
account's cached `Fable=100%` was trusted as current and a live Fable session was
moved off an account that actually had headroom. The stated fear is a reading
going stale across a week boundary: a pre-stale 95% that is really ~0 after
rollover.

## Key insight

Usage is monotonically non-decreasing within a window — already encoded in
`_compute_burn_rate` ("a drop means the window reset between polls"). Accounts
store `seven_day_resets_at`.

So a cached reading is a valid **lower bound** until a refresh lands: a cached 100%
is still ≥100%. Once one lands it is genuinely unknown. The 2026-07-05 incident is
exactly the post-refresh case.

The refresh moment is **not** the raw `seven_day_resets_at`. `projected_seven_day_reset`
documents that the real ~72h refresh precedes the API's ~7-day boundary; on the live
fleet 6 of 7 accounts had a projected reset earlier than the API value, by up to 4
days. Anchoring on the API boundary would keep accounts excluded long after their
budget returned.

Nor can the projection be used directly as the cutoff: it rolls forward
(`while nxt <= now`), so it is always in the future and a `now < projected` test
never expires. The invariant is instead:

> the cached reading is valid iff no refresh boundary falls between when it was
> observed (`last_observed_ts`) and now.

This yields the needed asymmetry: a cached reading is safe for **excluding** an
account and unsafe for **admitting** one.

## Scope

In scope — per-model weekly only:

- `_max_model_weekly_from_acct` returns the cached per-model max instead of `0.0`
  when the reading is a valid lower bound **and** that max is ≥100.

Already correct, no change:

- Aggregate 7d. `_account_effective_pct` and `_account_estimated_effective_pct`
  read `current_7d_pct` directly without consulting `_pct_is_unknown`, so a cached
  100.0 already fails the `never_swap_to_pct` hard filter.

Out of scope:

- Swap-away. `decide_swap` returns before Trigger 1 without a fresh
  `AccountUsage`, and Trigger 1 reads `_max_model_weekly_from_usage`, never the
  persisted dict — so this change is picker-side only and the swap-away force
  is untouched.

- The aggregate path's mirror bug: post-rollover it keeps excluding on a cached 100
  that may really be ~0. Returning `0.0` there is not the fix — a possibly-full
  account would look empty and *attract* swaps (the stale-low trap that the
  2026-07-07 launch verify-and-repick addresses). A safe fix needs fresh-poll
  verification on the swap-target path.

## Threshold

Exclusion fires at **≥100 only**. A cached 95 stays eligible. This matches the
existing `never_swap_to_pct` default and its recorded rationale: "the sub-100%
gray zone is intentionally left to the scoring logic — below 100% is a judgment
call". Sub-100 cached readings remain too uncertain to strand an account on.

## Design

New predicate beside `_pct_is_unknown`:

```
_cached_7d_usage_valid(acct, config, now=None) -> bool
    False  if last_observed_ts or seven_day_last_reset_ts is missing/unparseable
    False  if a cadence boundary (anchor + k*period) landed after the reading
    False  if seven_day_resets_at landed after the reading and before now
    else   True
```

The boundary is computed from the OBSERVED anchor on its fixed cadence, not from
`projected_seven_day_reset`. That helper returns `min(projection, api)`, and the
API value is a ~7-day oldest-tokens boundary unrelated to the 72h cadence — so
`resolved - period` is only the previous refresh when the projection won. When the
API value is nearer, that subtraction yields a bogus moment arbitrarily far in the
past and readings predating a real refresh get accepted. The API boundary is
therefore consulted only as an extra invalidator.

`last_poll_ts` is deliberately not a fallback for `last_observed_ts`: the error
branches restamp it every cycle, so an arbitrarily old reading would look current
forever.

Requiring the anchor narrows the feature to accounts where a reset drop has been
observed. That is the conservative direction, and all 7 live accounts carry it.

`_max_model_weekly_from_acct`, in the branch that currently returns `0.0`:

```
if _cached_7d_usage_valid(acct, config):
    cached_max = max of the numeric per-model values
    if cached_max >= 100:
        return cached_max
return 0.0
```

The floor is consulted only in this exclusion path. It never enters scoring, so an
account with a cached low reading still does not look attractive on a stale
number — that half of the 2026-07-05 fix is unchanged.

Routing through `_max_model_weekly_from_acct` means the existing per-model HARD
gate does the work: `pick_swap_target` already returns `None` rather than degrading
onto a model-capped account, giving refuse-and-HOLD behavior with no new control
flow.

## Configuration

None added. `per_model_weekly.gate_enabled` is the existing kill switch.

## Known assumption: per-model shares the aggregate cadence

`_parse_per_model_weekly` reads a per-entry `resets_at` for each model-scoped
weekly limit, but only the utilization is persisted into `per_model_weekly_pct`.
The predicate therefore dates a per-model reading by the AGGREGATE 72h anchor. If
a model-scoped window ever rolls on a different schedule, a cached per-model 100
could be honored past its own reset.

Bounded by the self-expiry property above: the exclusion lapses at most one period
after the reading regardless, so the worst case is a bounded over-exclusion rather
than an indefinite one. Making it model-accurate means persisting the per-model
`resets_at`, which wants confirmation of real API behavior first — deferred.

## Incidental change

Guarding `per_model_weekly_pct` against malformed shapes also affects the FRESH
path: a non-numeric entry is now dropped rather than raising out of `max()`. This
can lower a fresh account's computed cap instead of failing loudly. Chosen
deliberately — a schema tweak should not take polling down — and consistent with
the surrounding degrade-to-safe style.

## Failure modes

All degrade to current behavior (no exclusion): `seven_day_last_reset_ts` or
`last_observed_ts` missing or unparseable, `per_model_weekly_pct` missing or
malformed, cached max below 100. `seven_day_resets_at` is optional — it is only an
extra invalidator, so an account with an anchor but no API boundary is still
eligible for exclusion.

## Operator view

`_session_binding` skipped the per-model gate on any stale reading. It now honors
the same lower bound the decision layer uses, so a lane the daemon is evacuating
no longer reports "ok, headroom" in `cus sessions`. A stale reading without a
valid bound still reports the unconfirmed number, unchanged.

## Display

`cus status` renders 7d as `?` for these accounts because there was no
reset-inference for the 7-day window. The floor is that inference, so pre-reset it
can render `100%~` instead. Deferred: it widens the change beyond the decision path
and the `~` convention needs its own review.

## Tests

1. Cached per-model 100, pre-reset → excluded from `pick_swap_target`.
2. Same → excluded from `launch auto` picking.
3. Cached per-model 100, post-reset → NOT excluded (2026-07-05 incident case).
4. Cached per-model 94, pre-reset → NOT excluded.
5. `seven_day_resets_at` missing → NOT excluded.
6. Fresh (non-stale) readings → behavior unchanged.
7. All candidates cached-exhausted → `pick_swap_target` returns `None` (HOLD).
8. Swap-away decisions still ignore the cached value.
