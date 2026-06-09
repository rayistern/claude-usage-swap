# Background-swap cost-model test — 1-week live run

**Started:** 2026-06-09
**Review on / pick up:** ~2026-06-16 (one week)
**Tracking issues:** [#56](https://github.com/rayistern/claude-usage-swap/issues/56) (strategy review), [#57](https://github.com/rayistern/claude-usage-swap/issues/57) (competitor research), [#59](https://github.com/rayistern/claude-usage-swap/issues/59) (live rollover flag)
**Shipped in:** PR #58 (merged to `main` 2026-06-09) + PR for `feature/live-rollover-flag-and-test-doc`

> **If you're reading this a week later: jump to [§ Pick-up checklist](#pick-up-checklist).** It says exactly what to evaluate and the commands to run.

---

## Why this test exists

We confirmed empirically that **background-swap mode** (`hot_swap.enabled: false`) moves usage to the right account **without restarting any live session** — the credential file is global and Claude Code re-reads it on its next request. That inverts the cost model the swap rules were built for:

- **Old model:** swapping was disruptive, so we swapped *early/often* (low ladder steps, burn-before-reset) to avoid an account hitting its cap and **rate-limiting / interrupting** a live session.
- **New model:** a swap never interrupts anyone. Its *only* cost is a one-time **prompt-cache rebuild** (~5-min TTL) on the sessions that migrate. Hitting a cap is cheap (new sessions just use the other account). So the right time to swap is when it's **free** (cache cold) or **necessary** (near a cap) — not early.

The trigger was recurring **ping-pong**: e.g. 2026-06-08, 3 swaps in 30 min while both accounts sat ~92% 5h / ~12% 7d (≈88% weekly headroom — neither in real danger). Each swap silently rebuilt prompt cache across ~7 panes for no benefit.

## What changed (now live on `main`)

| Area | Change |
|---|---|
| **Lazy swap** (keystone) | A *deferrable* swap (ladder below saturation, burn-before-reset) is **held while any live session is cache-warm**; it fires once the cache goes cold (free) or the swap turns **urgent**. `lazy_swap` config block, on by default. |
| **Urgent bypass** | Hard 7d cap / 5h saturation (≥95%) / reactive 429 → `deferrable=False`, never held. Set per-trigger, NOT from tier (a hard-cap swap can be tier 2). |
| **Decision log** | Every cycle writes a who/what/where/when/why record to `decisions.jsonl`; view with `cus decisions`. |
| **Statusline fixes** | No phantom "swap pending → X" in background mode; `cus status` shows real (not SessionStart) account. |
| **Live rollover flag** (#59) | When the 5h reset countdown elapses before the next poll, statusline shows `5h:↻reset(was X%)` instead of the stale number. |
| **SOS on defer** | A defer cycle still refreshes SOS.md (so a long defer streak can't hide a condition). |
| **Config (earlier, in `~/claude-accounts/config.yaml`)** | `thresholds.steps: [90, 96]` (was `[70,85,95]`), `swap_hysteresis.min_improvement_pct: 5` (was default 3). |

## Live config at test start

```
strategy: smart
hot_swap.enabled: false          # background swap — the whole premise
thresholds.steps: [90, 96]       # don't swap until 90% (was 70)
thresholds.seven_day: false      # ladder keys on 5h only
swap_hysteresis.min_improvement_pct: 5
swap_hysteresis.min_seconds_between_swaps: 300   # left at 5min (a 30min lockout would strand a climb to 100%)
lazy_swap.enabled: true
lazy_swap.cache_window_seconds: 300
burn_before_reset.enabled: true  # KEPT — it's well-guarded; user defended it
smart_strategy.hard_7d_cap_pct: 80
poll_interval_seconds: 600
```

## Baseline (2026-06-09, for week-over-week comparison)

- **Total swaps in history:** 385. (On 2026-06-08, pre-change, the rate was ping-ponging — 3 swaps in 30 min at one point.)
- **Expectation after a week:** far fewer swaps/day; the remaining swaps should be either (a) cold-cache (free) or (b) at/above ~95% (necessary). Near-zero "both accounts ~92%" bounces.

---

## Pick-up checklist

Run these after the week and judge each line. The decision log is the primary evidence.

```bash
cus decisions --swaps-only -n 100   # every swap + defer with its gate + reason
cus decisions -n 50                 # include holds (full context)
cus status                          # current accounts + live sessions
journalctl --user -u cus.service --since "7 days ago" | grep -iE "swap|defer|DEGRADED|SOS"
# swap rate per day from the log:
jq -r 'select(.action=="swap") | .when[:10]' ~/claude-accounts/decisions.jsonl | sort | uniq -c
# how often we deferred (cache-warm holds):
jq -r 'select(.action=="defer") | .when[:10]' ~/claude-accounts/decisions.jsonl | sort | uniq -c
```

Evaluate:

- [ ] **Ping-pong gone?** Count swaps/day vs the ping-pong baseline. Look for any A→B→A within a short window. Expect ~none.
- [ ] **Lazy defer working?** Are there `defer [lazy_swap]` records, and do they later resolve into a `swap` once the account hit ~95% OR the cache went cold? Confirm a defer→release sequence actually happened on the *running daemon* (we only verified the defer side live + unit-tested the release side at test start).
- [ ] **No bad rate-limits?** Any `reactive_429` swaps? Did "letting an account run to ~100%" actually cause user-visible interruptions, or was it harmless as predicted ("who cares if we run out")?
- [ ] **Cache churn down?** Fewer swaps = fewer prompt-cache rebuilds. Subjective: did sessions feel less disrupted?
- [ ] **Rollover flag correct?** Did `5h:↻reset(was X%)` appear when a window actually reset, and NOT false-fire on idle 0% accounts? Did it make resets visible without manual polling?
- [ ] **Decision log useful?** Could you answer "why did/didn't it swap?" from `cus decisions` alone?
- [ ] **Any SOS / surprises?** Scan for unexpected gates or DEGRADED states.

## Decisions still pending (resolve after the test)

These were deliberately deferred so the test isn't confounded:

1. **Saturated-both freeze (#56):** when *both* accounts are ≥ saturation, a swap can't help — still need explicit "freeze, don't bounce" logic, or did lazy-swap + min_improvement_pct=5 already cover it? Check the log for any saturated bounces.
2. **Threshold re-tuning (#56):** are `[90, 96]` right under the new model? Could go higher. Is `min_improvement_pct: 5` enough, or raise it?
3. **`burn_before_reset` (#56):** kept on. Did it fire usefully, or did lazy-swap defer it into irrelevance? Decide keep / tune / drop.
4. **Reset inference (#59) — NOW IMPLEMENTED, evaluate it:** the daemon now (a) infers a 5h reset from the countdown when a poll didn't refresh past it (`reset_inference.enabled`, writes 5h→0 for decisions/SOS) and (b) adaptively repolls just after a known reset (`reset_inference.adaptive_repoll`). Evaluate over the week: did the countdown fallback ever fire (grep daemon.log for "countdown fallback"), and did it ever infer-reset an account that was actually still busy (over-optimistic)? Did adaptive repoll shorten the gap as intended? Watch for any thrash from the shortened sleep.
5. **7d in ladder scope (#56):** `seven_day: false` today. Re-add as a "no real danger, don't churn" signal?
6. **Competitor research (#57):** are we redundant vs cux / ccusage / claude-code-router?

## Walk-back

- Disable just the lazy behavior: `lazy_swap.enabled: false` in `~/claude-accounts/config.yaml` + `systemctl --user restart cus.service`.
- Full revert: `git revert 36f5a1e` (PR #58 merge) + revert the rollover merge + restart.
- The daemon and statusline run the working-tree `cus.py` directly, so **keep `main` checked out** in this repo for the duration (or the fleet statusline + logging silently revert).
