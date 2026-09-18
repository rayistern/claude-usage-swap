"""Tests for the 2026-07-05 "stale per-model usage shown as authoritative" bug cluster.

Incident: an operator (and an agent) trusted `03 Fable=100%` and moved a live
Fable session off account 03 — but 03 was `token_stale`, so that 100% was a
STALE cached per-model value; 03 actually had Fable headroom. The move
interrupted live work and burned usage. Three defects fed it:

  Bug 1 — per-model weekly values were printed BARE (no `~`, no `?`) in
    `cus status` (the `└ 7d by model:` line) and in `cus sessions`
    (`_session_binding`'s premium-gate verdict), so a stale number looked
    current even while the aggregate 5h/7d were correctly marked stale.
  Bug 2 — target selection (`_max_model_weekly_from_acct`, read by
    `pick_swap_target` and the hard-cap anti-pingpong guard) trusted the cached
    per-model dict, so a STALE per-model reading could refuse an account as a
    swap target. (The swap-AWAY force reads FRESH usage and was already safe.)
  Bug 3 — a `token_stale` account with a VALID refresh token was polled only on
    the slow inactive cadence, so its self-refresh preflight ran rarely and the
    stale cached % sat for hours instead of being reconfirmed promptly.

Run standalone:  python3 tests/test_stale_per_model_reading.py
Or under pytest: pytest tests/test_stale_per_model_reading.py
"""

import json
import sys
import tempfile
import time
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

from click.testing import CliRunner

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cus  # noqa: E402


# ==========================================================================
# Shared fixtures
# ==========================================================================

class _FakeResponse:
    """Context-manager stand-in for urllib.request.urlopen's return value."""

    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self, *_args):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _stub_urlopen(fn):
    original = cus.urllib.request.urlopen
    cus.urllib.request.urlopen = fn
    return lambda: setattr(cus.urllib.request, "urlopen", original)


def _gate_config(**over) -> dict:
    """DEFAULT_CONFIG with the per-model weekly gate ON at cap 97%, mirroring
    the live setup that surfaced the incident."""
    base = cus.deep_merge(cus.DEFAULT_CONFIG,
                          {"per_model_weekly": {"gate_enabled": True, "cap_pct": 97}})
    return cus.deep_merge(base, over) if over else base


class _StatusEnv:
    """Throwaway state.json + config.yaml with cus's path constants repointed,
    and a quiet diagnose()/find_live_sessions() so the status render path is
    exercised in isolation. Mirrors tests/test_token_stale_5h_display.py."""

    def __init__(self, acct: dict, color: bool = False):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        state_path = root / "state.json"
        state_path.write_text(json.dumps(
            {"active": "a", "accounts": {"a": acct}, "swap_history": []}))
        config_path = root / "config.yaml"
        config_path.write_text(f"statusline:\n  color: {'true' if color else 'false'}\n")
        self._saved = {k: getattr(cus, k) for k in
                       ("STATE_JSON", "CONFIG_YAML", "diagnose", "find_live_sessions")}
        cus.STATE_JSON = state_path
        cus.CONFIG_YAML = config_path
        cus.diagnose = lambda state, config: []
        cus.find_live_sessions = lambda *a, **k: []

    def restore(self):
        for k, v in self._saved.items():
            setattr(cus, k, v)
        self._tmp.cleanup()


class _PollEnv:
    """Throwaway ~/claude-accounts + state.json + config.yaml for the poll path.
    Mirrors tests/test_token_self_refresh.py's env."""

    def __init__(self, config_yaml: str, state: dict):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._saved = {k: getattr(cus, k) for k in
                       ("ACCOUNTS_DIR", "STATE_JSON", "CONFIG_YAML", "CREDS_JSON")}
        cus.ACCOUNTS_DIR = self.root / "claude-accounts"
        cus.ACCOUNTS_DIR.mkdir()
        cus.STATE_JSON = self.root / "state.json"
        cus.STATE_JSON.write_text(json.dumps(state))
        cus.CREDS_JSON = self.root / "live-credentials.json"  # never the test account
        config_path = self.root / "config.yaml"
        config_path.write_text(config_yaml)
        cus.CONFIG_YAML = config_path

    def write_creds(self, account: str, oauth: dict) -> Path:
        d = cus.ACCOUNTS_DIR / f"account-{account}"
        d.mkdir(parents=True, exist_ok=True)
        p = d / ".credentials.json"
        p.write_text(json.dumps({"claudeAiOauth": oauth}))
        return p

    def restore(self):
        for k, v in self._saved.items():
            setattr(cus, k, v)
        self._tmp.cleanup()


def _iso(dt: datetime) -> str:
    return dt.isoformat()


# ==========================================================================
# (a) A token_stale account renders per-model with a stale marker
#     — in `cus status` AND in `cus sessions` (_session_binding)
# ==========================================================================

def test_status_marks_per_model_stale_under_token_stale():
    acct = {
        "current_5h_pct": 82,
        "current_7d_pct": 63,
        "next_swap_at_pct": 90,
        "token_stale": True,
        "per_model_weekly_pct": {"Fable": 100.0},
    }
    env = _StatusEnv(acct)
    try:
        out = CliRunner().invoke(cus.status).output
        model_lines = [l for l in out.splitlines() if "by model" in l]
        assert len(model_lines) == 1, out
        # Stale marker present; bare authoritative "Fable=100%" must NOT appear.
        assert "Fable=100%~" in model_lines[0], model_lines[0]
        assert "Fable=100% " not in (model_lines[0] + " ")
    finally:
        env.restore()


def test_session_binding_does_not_hard_block_on_stale_per_model():
    """A token_stale premium lane whose cached Fable=100% would trip the gate
    must NOT read as a hard 'premium gate' block — the value is unconfirmed."""
    # 5h/7d have headroom (so no ladder/hard-wall trip preempts the gate) — the
    # ONLY thing that would block is the stale Fable=100%, which must not.
    # No reset anchor, so the cached reading is genuinely unknown rather than a
    # valid lower bound (see the sibling test for that case).
    acct = {"current_5h_pct": 10.0, "current_7d_pct": 5.0,
            "token_stale": True, "per_model_weekly_pct": {"Fable": 100.0}}
    sev, txt = cus._session_binding(acct, "premium", _gate_config())
    assert sev != "blocked", (sev, txt)
    assert "premium gate" not in txt, txt
    assert "~" in txt or "stale" in txt, txt


def test_session_binding_keeps_the_stale_marker_when_the_gate_is_off():
    """`model_stale` must keep its DISPLAY meaning. Folding the bound into it
    would drop the '[stale — repoll to confirm]' hint whenever gate_enabled is
    False, since the gate block that consumes the bound never runs."""
    now = datetime.now(timezone.utc)
    acct = {"current_5h_pct": 10.0, "current_7d_pct": 5.0, "token_stale": True,
            "per_model_weekly_pct": {"Fable": 100.0},
            "seven_day_last_reset_ts": _iso(now - timedelta(hours=1)),
            "last_observed_ts": _iso(now - timedelta(minutes=30))}
    sev, txt = cus._session_binding(
        acct, "premium", _gate_config(per_model_weekly={"gate_enabled": False}))
    assert sev == "ok", (sev, txt)
    assert "stale" in txt, txt


def test_session_binding_blocks_when_the_cached_100_is_still_a_valid_bound():
    """The operator view must not report headroom on the same reading the daemon
    is evacuating the lane on."""
    now = datetime.now(timezone.utc)
    acct = {"current_5h_pct": 10.0, "current_7d_pct": 5.0, "token_stale": True,
            "per_model_weekly_pct": {"Fable": 100.0},
            "seven_day_last_reset_ts": _iso(now - timedelta(hours=1)),
            "last_observed_ts": _iso(now - timedelta(minutes=30))}
    sev, txt = cus._session_binding(acct, "premium", _gate_config())
    assert sev == "blocked", (sev, txt)
    assert "premium gate" in txt, txt


# ==========================================================================
# (b) A fresh account renders per-model normally (no stale marker, gate binds)
# ==========================================================================

def test_status_renders_fresh_per_model_bare():
    acct = {
        "current_5h_pct": 40,
        "current_7d_pct": 63,
        "next_swap_at_pct": 90,
        "per_model_weekly_pct": {"Fable": 100.0},
    }
    env = _StatusEnv(acct)
    try:
        out = CliRunner().invoke(cus.status).output
        model_lines = [l for l in out.splitlines() if "by model" in l]
        assert len(model_lines) == 1, out
        assert "Fable=100%" in model_lines[0]
        assert "Fable=100%~" not in model_lines[0]  # no stale marker on fresh data
    finally:
        env.restore()


def test_session_binding_still_blocks_fresh_premium_over_cap():
    """Unchanged behavior: a FRESH premium lane over the model cap is blocked."""
    acct = {"current_5h_pct": 10.0, "current_7d_pct": 5.0,
            "per_model_weekly_pct": {"Fable": 98.0}}
    sev, txt = cus._session_binding(acct, "premium", _gate_config())
    assert sev == "blocked", (sev, txt)
    assert "Fable" in txt and "premium gate" in txt, txt


# ==========================================================================
# (c) The decision path does not force / refuse a swap on a stale per-model value
# ==========================================================================

def test_max_model_weekly_from_acct_treats_stale_as_unknown():
    cfg = _gate_config()
    fresh = {"current_7d_pct": 50.0, "per_model_weekly_pct": {"Fable": 100.0}}
    stale = {"token_stale": True, "current_7d_pct": 50.0,
             "per_model_weekly_pct": {"Fable": 100.0}}
    # Fresh account: the cap is real and folded in.
    assert cus._max_model_weekly_from_acct(fresh, cfg) == 100.0
    # Stale account: unknown → 0.0, so it neither excludes a target nor forces a hold.
    assert cus._max_model_weekly_from_acct(stale, cfg) == 0.0
    # Any un-observable flag counts, not just token_stale.
    for flag in ("rate_limited", "token_expired", "poll_error"):
        acct = {flag: True, "current_7d_pct": 50.0, "per_model_weekly_pct": {"Fable": 100.0}}
        assert cus._max_model_weekly_from_acct(acct, cfg) == 0.0, flag


def test_max_model_weekly_honors_cached_100_before_the_window_resets():
    # Usage is monotonic within a window, so before seven_day_resets_at a cached
    # 100% is still >= 100% and must exclude the account.
    cfg = _gate_config()
    now = datetime.now(timezone.utc)
    acct = {"token_stale": True, "current_7d_pct": 100.0,
            "per_model_weekly_pct": {"Fable": 100.0},
            "seven_day_last_reset_ts": _iso(now - timedelta(hours=1)),
            "last_observed_ts": _iso(now - timedelta(minutes=30)),
            "seven_day_resets_at": _iso(now + timedelta(days=2))}
    assert cus._max_model_weekly_from_acct(acct, cfg) == 100.0


def test_swap_away_trigger1_reads_fresh_usage_not_cached_dict():
    # Trigger 1 is the only swap-away force: it reads fresh usage, so the cached
    # lower bound (a persisted-dict rule) never reaches it.
    """Evidence that the swap-AWAY force was already safe: a token_stale
    account's fresh AccountUsage this cycle is empty, so the swap-away signal
    (_max_model_weekly_from_usage) is 0.0 and can never force a lane off it."""
    u = cus.AccountUsage.empty()      # what poll_account_usage returns for token_stale
    u.token_stale = True
    assert cus._max_model_weekly_from_usage(u, _gate_config()) == 0.0


def test_pick_swap_target_not_refused_on_stale_per_model():
    """A stale-Fable=100% spare is NOT refused as a target on the stale reading
    (pick returns it cleanly). Isolates Bug 2's target-selection error.

    Annotation 2026-07-05 (supersedes the fresh-case expectation, fix "premium
    lane placed on Fable-capped account via degraded fan-out fallback — should
    HOLD"): pre-fix, a FRESH Fable=100% spare was returned as a DEGRADED
    fallback. The per-model gate is now a HARD filter for a gated config, so when
    the ONLY candidate is a FRESH over-cap account the picker HOLDS (returns
    None) rather than degrading a premium lane onto it. The stale case is the
    load-bearing half of this test (a stale reading must NOT exclude a target)
    and is unchanged; the fresh case now asserts the HARD HOLD."""
    cfg = _gate_config()

    def _state_with_spare(spare_acct: dict) -> dict:
        return {
            "active": "cur",
            "accounts": {
                "cur": {"current_5h_pct": 95.0, "current_7d_pct": 40.0, "next_swap_at_pct": 50},
                "spare": spare_acct,
            },
            "swap_history": [],
        }

    stale_spare = {"current_5h_pct": 5.0, "current_7d_pct": 5.0, "next_swap_at_pct": 90,
                   "token_stale": True, "per_model_weekly_pct": {"Fable": 100.0}}
    tgt = cus.pick_swap_target(_state_with_spare(stale_spare), cfg)
    assert tgt is not None and tgt.name == "spare", tgt
    assert "DEGRADED" not in tgt.reason, tgt.reason  # not refused on the stale %

    # Fresh over-cap sole candidate → HARD HOLD (fix 2026-07-05), not a degraded
    # pick. A premium lane never lands on a Fable-dead account.
    fresh_spare = {"current_5h_pct": 5.0, "current_7d_pct": 5.0, "next_swap_at_pct": 90,
                   "per_model_weekly_pct": {"Fable": 100.0}}
    tgt2 = cus.pick_swap_target(_state_with_spare(fresh_spare), cfg)
    assert tgt2 is None, f"expected HOLD (fresh over-cap sole target), got {tgt2}"


def test_max_model_weekly_ignores_cached_100_once_a_refresh_landed_since_the_reading():
    # The real ~72h refresh precedes the API's ~7d boundary
    # (projected_seven_day_reset). A reading taken BEFORE that refresh says
    # nothing about the new window, even though the API boundary is still future.
    cfg = _gate_config()
    now = datetime.now(timezone.utc)
    acct = {"token_stale": True, "current_7d_pct": 100.0,
            "per_model_weekly_pct": {"Fable": 100.0},
            # refresh cadence anchored 8 days back → a boundary landed 2 days ago
            "seven_day_last_reset_ts": _iso(now - timedelta(hours=192)),
            "last_observed_ts": _iso(now - timedelta(days=3)),
            "seven_day_resets_at": _iso(now + timedelta(days=2))}
    assert cus._max_model_weekly_from_acct(acct, cfg) == 0.0


def test_max_model_weekly_ignores_a_reading_predating_the_refresh_when_api_is_nearer():
    # projected_seven_day_reset returns min(anchor projection, raw API boundary).
    # The API value is a ~7d oldest-tokens boundary unrelated to the 72h cadence,
    # so "next - 72h" is only the previous refresh for the anchor branch.
    cfg = _gate_config()
    now = datetime.now(timezone.utc)
    acct = {"token_stale": True, "current_7d_pct": 100.0,
            "per_model_weekly_pct": {"Fable": 100.0},
            "seven_day_last_reset_ts": _iso(now - timedelta(hours=2)),   # refresh 2h ago
            "last_observed_ts": _iso(now - timedelta(hours=50)),         # reading predates it
            "seven_day_resets_at": _iso(now + timedelta(hours=10))}      # API nearer than projection
    assert cus._max_model_weekly_from_acct(acct, cfg) == 0.0


def test_max_model_weekly_ignores_a_reading_with_no_observation_timestamp():
    # last_poll_ts is a poll ATTEMPT stamp, restamped every cycle by the error
    # branches, so it cannot stand in for when usage was last actually seen.
    cfg = _gate_config()
    now = datetime.now(timezone.utc)
    acct = {"token_stale": True, "current_7d_pct": 100.0,
            "per_model_weekly_pct": {"Fable": 100.0},
            "seven_day_last_reset_ts": _iso(now - timedelta(hours=200)),
            "last_poll_ts": _iso(now - timedelta(seconds=30))}
    assert cus._max_model_weekly_from_acct(acct, cfg) == 0.0


def test_cached_exclusion_self_expires_for_a_permanently_unpollable_account():
    """Safety bound: the anchor and the observation are written from the same
    polled_at, so the k=0 boundary never invalidates a reading on its own and the
    exclusion lapses at the FIRST boundary after it. An account that can never be
    polled again is sidelined for at most one period, not forever."""
    cfg = _gate_config()
    now = datetime.now(timezone.utc)

    def _acct(age_hours: float) -> dict:
        stamp = _iso(now - timedelta(hours=age_hours))
        return {"token_stale": True, "current_7d_pct": 100.0,
                "per_model_weekly_pct": {"Fable": 100.0},
                "seven_day_last_reset_ts": stamp, "last_observed_ts": stamp}

    assert cus._max_model_weekly_from_acct(_acct(10), cfg) == 100.0, "within the period → still excluded"
    assert cus._max_model_weekly_from_acct(_acct(80), cfg) == 0.0, "past the first boundary → lapsed"


def test_max_model_weekly_requires_an_observed_reset_anchor():
    # Without seven_day_last_reset_ts the 72h cadence is unknown, and the API
    # boundary cannot bound it. Decline rather than guess.
    cfg = _gate_config()
    now = datetime.now(timezone.utc)
    acct = {"token_stale": True, "current_7d_pct": 100.0,
            "per_model_weekly_pct": {"Fable": 100.0},
            "last_observed_ts": _iso(now - timedelta(minutes=5)),
            "seven_day_resets_at": _iso(now + timedelta(days=2))}
    assert cus._max_model_weekly_from_acct(acct, cfg) == 0.0


def test_max_model_weekly_survives_a_naive_reset_timestamp():
    # A stored timestamp without an offset must degrade to "unknown", not raise
    # out of pick_swap_target/decide_swap.
    cfg = _gate_config()
    # Anchor present so the guard is actually entered and the naive/aware
    # comparison inside it is what degrades.
    acct = {"token_stale": True, "current_7d_pct": 100.0,
            "per_model_weekly_pct": {"Fable": 100.0},
            "seven_day_last_reset_ts": "2026-08-07T00:00:00",
            "last_observed_ts": "2026-08-07T00:00:00",
            "seven_day_resets_at": "2026-08-09T00:00:00"}
    assert cus._max_model_weekly_from_acct(acct, cfg) == 0.0


def test_max_model_weekly_survives_a_malformed_per_model_dict():
    cfg = _gate_config()
    for pm in ([("Fable", 100.0)], {"Fable": "lots"}, {5: 100.0}, "Fable=100"):
        acct = {"token_stale": True, "current_7d_pct": 100.0, "per_model_weekly_pct": pm}
        assert cus._max_model_weekly_from_acct(acct, cfg) == 0.0, pm


def test_max_model_weekly_ignores_cached_100_when_the_api_boundary_passed():
    # The API boundary is the extra invalidator: no cadence boundary has landed
    # since the reading, but seven_day_resets_at fell between it and now.
    cfg = _gate_config()
    now = datetime.now(timezone.utc)
    acct = {"token_stale": True, "current_7d_pct": 100.0,
            "per_model_weekly_pct": {"Fable": 100.0},
            "seven_day_last_reset_ts": _iso(now - timedelta(hours=1)),
            "last_observed_ts": _iso(now - timedelta(minutes=30)),
            "seven_day_resets_at": _iso(now - timedelta(minutes=15))}
    assert cus._max_model_weekly_from_acct(acct, cfg) == 0.0


def test_max_model_weekly_ignores_a_cached_reading_below_100():
    # Only the ceiling is unambiguous; a cached 94 might have headroom left.
    cfg = _gate_config()
    now = datetime.now(timezone.utc)
    acct = {"token_stale": True, "current_7d_pct": 94.0,
            "per_model_weekly_pct": {"Fable": 94.0},
            "seven_day_last_reset_ts": _iso(now - timedelta(hours=1)),
            "last_observed_ts": _iso(now - timedelta(minutes=30)),
            "seven_day_resets_at": _iso(now + timedelta(days=2))}
    assert cus._cached_7d_usage_valid(acct, cfg), \
        "precondition: the reading IS valid, so only the threshold can reject it"
    assert cus._max_model_weekly_from_acct(acct, cfg) == 0.0


def test_max_model_weekly_ignores_cached_100_without_a_reset_timestamp():
    # No reset timestamp = no way to know whether the window rolled.
    cfg = _gate_config()
    acct = {"token_stale": True, "current_7d_pct": 100.0,
            "per_model_weekly_pct": {"Fable": 100.0}}
    assert cus._max_model_weekly_from_acct(acct, cfg) == 0.0


def test_pick_swap_target_holds_on_cached_exhaustion_before_reset():
    """The user-facing requirement: an account known to be 7d-exhausted stays out
    of rotation while rate-limited/stale, instead of being swapped onto."""
    cfg = _gate_config()

    def _state_with_spare(spare_acct: dict) -> dict:
        return {
            "active": "cur",
            "accounts": {
                "cur": {"current_5h_pct": 95.0, "current_7d_pct": 40.0, "next_swap_at_pct": 50},
                "spare": spare_acct,
            },
            "swap_history": [],
        }

    # Aggregate 7d stays LOW so the never_swap_to_pct filter can't be the reason
    # for a HOLD, and token_stale (not rate_limited) so the rate-limited filter
    # can't be either. The only disqualifier available is the cached per-model %.
    now = datetime.now(timezone.utc)
    base = {"current_5h_pct": 5.0, "current_7d_pct": 5.0, "next_swap_at_pct": 90,
            "token_stale": True, "per_model_weekly_pct": {"Fable": 100.0},
            "seven_day_resets_at": _iso(now + timedelta(days=2))}

    # Reading taken AFTER the most recent refresh → still a valid lower bound.
    pre_reset = dict(base,
                     seven_day_last_reset_ts=_iso(now - timedelta(hours=1)),
                     last_observed_ts=_iso(now - timedelta(minutes=30)))
    assert cus.pick_swap_target(_state_with_spare(pre_reset), cfg) is None, \
        "cached-exhausted spare must not be a target while the reading still holds"

    # A refresh landed after the reading → says nothing about the new window.
    post_reset = dict(base,
                      seven_day_last_reset_ts=_iso(now - timedelta(hours=192)),
                      last_observed_ts=_iso(now - timedelta(days=3)))
    tgt = cus.pick_swap_target(_state_with_spare(post_reset), cfg)
    assert tgt is not None and tgt.name == "spare", \
        f"post-refresh the reading is unknown, not disqualifying, got {tgt}"


# ==========================================================================
# (d) Auto-refresh clears token_stale during a NORMAL (not force) poll:
#     the account is poll-due on the fast cadence, and polling it refreshes
#     the token and re-fetches real usage, clearing token_stale.
# ==========================================================================

_POLL_CFG = ("polling:\n"
             "  active_interval_seconds: 300\n"
             "  inactive_interval_seconds: 600\n"
             "poll_interval_seconds: 300\n")


def test_token_stale_account_is_poll_due_on_fast_cadence():
    """A token_stale INACTIVE account whose last_poll_ts is 400s old would be
    NOT due on the slow 600s inactive cadence, but the token_stale fast-track
    puts it on the 300s active cadence → due. A non-stale twin stays not-due."""
    config = {"polling": {"active_interval_seconds": 300, "inactive_interval_seconds": 600},
              "poll_interval_seconds": 300, "mode": "global"}
    last = _iso(datetime.now(timezone.utc) - timedelta(seconds=400))
    state = {"active": "other", "accounts": {
        "other": {"last_poll_ts": last},
        "spare": {"last_poll_ts": last, "token_stale": True},
    }}
    due, why = cus._account_poll_due(state, config, "spare")
    assert due is True, why
    assert "token_stale-fast" in why, why

    # Control: same age, NOT stale → slow inactive cadence → not due.
    state["accounts"]["spare"].pop("token_stale")
    due2, why2 = cus._account_poll_due(state, config, "spare")
    assert due2 is False, why2


def test_normal_poll_of_stale_account_refreshes_and_clears():
    """End-to-end for Bug 3: a token_stale account with a VALID refresh token,
    when polled the way the daemon's regular cycle polls it, mints a fresh
    token and completes a real usage poll — clearing token_stale in one cycle."""
    stale_oauth = {
        "accessToken": "old-access-token",
        "refreshToken": "valid-refresh-token",
        "expiresAt": int(time.time() * 1000) - 40 * 60 * 1000,  # expired 40 min ago
        "scopes": ["user:inference", "user:profile"],
    }
    last = _iso(datetime.now(timezone.utc) - timedelta(seconds=400))
    state = {"active": "other", "accounts": {
        "spare": {"last_poll_ts": last, "token_stale": True},
    }, "slots": {}, "swap_history": []}
    env = _PollEnv(_POLL_CFG, state)
    try:
        env.write_creds("spare", stale_oauth)
        config = cus.load_config()

        # 1. The daemon's poll gate WOULD poll this stale account this cycle.
        due, why = cus._account_poll_due(state, config, "spare")
        assert due is True, why

        # 2. Polling it refreshes the token and observes real usage → not stale.
        def fake_urlopen(req, timeout=None):
            if req.full_url == cus.OAUTH_TOKEN_URL:
                return _FakeResponse(json.dumps({
                    "access_token": "fresh-access-token",
                    "refresh_token": "fresh-refresh-token",
                    "expires_in": 3600,
                }).encode())
            assert req.full_url == cus.USAGE_API_URL
            assert req.headers.get("Authorization") == "Bearer fresh-access-token"
            return _FakeResponse(json.dumps({
                "five_hour": {"utilization": 8.0, "resets_at": None},
                "seven_day": {"utilization": 20.0, "resets_at": None},
            }).encode())

        unstub = _stub_urlopen(fake_urlopen)
        try:
            usage = cus.poll_account_usage("spare")
        finally:
            unstub()

        assert usage.token_stale is False
        assert usage.five_hour.utilization == 8.0

        # And update_state_with_usage clears the persisted flag (Branch 4).
        cus.update_state_with_usage(state, {"spare": usage})
        assert state["accounts"]["spare"].get("token_stale") in (None, False)
        assert state["accounts"]["spare"]["current_5h_pct"] == 8.0
    finally:
        env.restore()


def test_dead_refresh_token_still_degrades_gracefully():
    """Graceful-degrade guard: a token_stale account whose refresh token is
    dead stays token_stale (no crash), and the usage endpoint is never hit."""
    dead_oauth = {
        "accessToken": "old-access-token",
        "refreshToken": "dead-refresh-token",
        "expiresAt": int(time.time() * 1000) - 40 * 60 * 1000,
        "scopes": ["user:inference"],
    }
    state = {"active": "other", "accounts": {"spare": {}}, "slots": {}, "swap_history": []}
    env = _PollEnv(_POLL_CFG, state)
    calls = []
    try:
        env.write_creds("spare", dead_oauth)

        def raise_http_error(req, timeout=None):
            calls.append(req.full_url)
            raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", {}, None)

        unstub = _stub_urlopen(raise_http_error)
        try:
            usage = cus.poll_account_usage("spare")
        finally:
            unstub()

        assert usage.token_stale is True
        assert calls == [cus.OAUTH_TOKEN_URL]  # usage endpoint never reached
    finally:
        env.restore()


def _run_all() -> int:
    import types
    tests = [v for k, v in globals().items()
             if k.startswith("test_") and isinstance(v, types.FunctionType)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL  {t.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_all())
