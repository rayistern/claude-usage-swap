"""GH #237 — track each account's PLAN TIER (5x / 20x) so burn is comparable
across plan sizes and placement can be sized to the payload.

The incident (2026-09-18): after an upgrade, slot-3's burn "dropped" from ~6
to ~1.7 points of the 5h window per minute. The panes were not doing less;
the account was bigger. Percentages are not comparable across plan sizes.

Ground truth for every freshness assertion here is the read-only survey of
2026-09-20 on the fleet box, plus one live profile GET per account:

    account  credential-file rateLimitTier   profile organization.rate_limit_tier
    rayi2    default_claude_max_5x           default_claude_max_20x   (file STALE)
    rayi3    default_claude_max_20x          default_claude_max_5x    (file STALE)
    rayi4    default_claude_max_5x           default_claude_max_5x    (the "upgrade" was free->5x)
    rayi1    default_claude_max_20x          default_claude_max_20x

and rayi3 had family stores rewritten in the same minute that disagreed
(20x vs 5x). So the credential-file field is written at LOGIN and carried
through token refreshes unchanged: it goes stale after a plan change until a
re-login. The profile endpoint is live. These tests encode that requirement —
the file value may be shown, labelled, but may never size anything.

Run standalone:  python3 tests/test_plan_tier_237.py
Run under pytest: pytest tests/test_plan_tier_237.py
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import cus  # noqa: E402
from click.testing import CliRunner  # noqa: E402
from test_login_pool import _Env  # noqa: E402

NOW = datetime(2026, 9, 20, 22, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _cfg(**over) -> dict:
    return cus.deep_merge(cus.DEFAULT_CONFIG, over) if over else dict(cus.DEFAULT_CONFIG)


# --------------------------------------------------------------------------
# Parsing and the metadata reader
# --------------------------------------------------------------------------

def test_tier_string_parses_to_a_multiplier_or_nothing():
    assert cus._plan_tier_multiplier("default_claude_max_20x") == 20
    assert cus._plan_tier_multiplier("default_claude_max_5x") == 5
    assert cus._plan_tier_multiplier("default_claude_ai") is None      # free/canceled org (captured 2026-08-07)
    assert cus._plan_tier_multiplier(None) is None
    assert cus._plan_tier_label("default_claude_max_20x") == "20x"
    assert cus._plan_tier_label("default_claude_ai") == "default_claude_ai"   # shown raw, never a default


def test_metadata_reader_returns_only_the_two_plan_fields_and_the_file_age(tmp_path):
    p = tmp_path / ".credentials.json"
    p.write_text(json.dumps({"claudeAiOauth": {"accessToken": "at-secret", "refreshToken": "rt-secret",
                                                "expiresAt": 2_000_000_000_000,
                                                "subscriptionType": "max", "rateLimitTier": "default_claude_max_5x"}}))
    meta = cus._read_plan_metadata_from_creds(p)
    assert set(meta) == {"raw", "subscription_type", "file_mtime_ts"}
    assert meta["raw"] == "default_claude_max_5x" and meta["subscription_type"] == "max"
    assert "secret" not in json.dumps(meta)                                   # never token material
    p.write_text(json.dumps({"claudeAiOauth": {"accessToken": "x"}}))
    assert cus._read_plan_metadata_from_creds(p) is None                      # no tier field => nothing
    assert cus._read_plan_metadata_from_creds(tmp_path / "missing.json") is None


def test_profile_detail_carries_the_live_tier():
    """Field name verified against a real response on 2026-09-20."""
    profile = {"account": {"has_claude_max": True, "has_claude_pro": False},
               "organization": {"organization_type": "claude_max", "rate_limit_tier": "default_claude_max_20x",
                                "seat_tier": None, "subscription_status": "active"}}
    disabled, detail = cus._profile_says_subscription_disabled(profile)
    assert disabled is False and detail["rate_limit_tier"] == "default_claude_max_20x"


# --------------------------------------------------------------------------
# The resolver: which source wins, when it is stale, when it may size
# --------------------------------------------------------------------------

def test_rayi2_shape_fresh_profile_wins_over_a_stale_credential_file():
    acct = {"plan_tier_profile": {"raw": "default_claude_max_20x", "observed_ts": _iso(NOW - timedelta(minutes=12))},
            "plan_tier_file": {"raw": "default_claude_max_5x", "subscription_type": "max",
                               "file_mtime_ts": _iso(NOW - timedelta(hours=3))}}
    t = cus.resolve_plan_tier(acct, NOW, _cfg())
    assert (t["label"], t["multiplier"], t["source"]) == ("20x", 20, "profile")
    assert t["mismatch"] is True and t["file_label"] == "5x"
    assert t["sizing_ok"] is True and t["stale"] is False
    assert cus.plan_tier_text(t) == "20x (profile 12m; credential file says 5x — stale)"


def test_stale_profile_reading_is_shown_with_its_age_and_never_sizes():
    acct = {"plan_tier_profile": {"raw": "default_claude_max_20x", "observed_ts": _iso(NOW - timedelta(hours=30))}}
    t = cus.resolve_plan_tier(acct, NOW, _cfg())
    assert t["label"] == "20x" and t["stale"] is True and t["sizing_ok"] is False
    assert "STALE" in cus.plan_tier_text(t) and "30h" in cus.plan_tier_text(t)
    assert cus.tokens_per_5h_point(t, _cfg()) is None
    # the freshness bar is configurable, like every other freshness rule here
    assert cus.resolve_plan_tier(acct, NOW, _cfg(plan_tiers={"profile_max_age_hours": 48}))["sizing_ok"] is True


def test_credential_file_alone_is_unverified_and_never_sizes():
    """The rayi2 incident in one fixture: the file says 5x; live it was 20x."""
    acct = {"plan_tier_file": {"raw": "default_claude_max_5x", "subscription_type": "max",
                               "file_mtime_ts": _iso(NOW - timedelta(hours=3))}}
    t = cus.resolve_plan_tier(acct, NOW, _cfg())
    assert t["label"] == "5x?" and t["source"] == "credential-file"
    assert t["sizing_ok"] is False and t["multiplier"] is None
    assert cus.plan_tier_text(t) == "5x? (credential file 3h, unverified)"
    assert cus.projected_pts_per_min(100_000, t, _cfg()) is None


def test_no_reading_and_unparseable_tier_degrade_to_unknown():
    t = cus.resolve_plan_tier({}, NOW, _cfg())
    assert t["label"] is None and t["sizing_ok"] is False and cus.plan_tier_text(t) == "unknown"
    assert cus.resolve_plan_tier(None, NOW, _cfg())["label"] is None                  # older state.json
    t2 = cus.resolve_plan_tier({"plan_tier_profile": {"raw": "default_claude_ai", "observed_ts": _iso(NOW)}}, NOW, _cfg())
    assert t2["label"] == "default_claude_ai" and t2["multiplier"] is None and t2["sizing_ok"] is False


# --------------------------------------------------------------------------
# Burn both ways: the same token burn is a different %/min on 5x and 20x
# --------------------------------------------------------------------------

def _tier(raw):
    return cus.resolve_plan_tier({"plan_tier_profile": {"raw": raw, "observed_ts": _iso(NOW)}}, NOW, _cfg())


def test_same_token_burn_is_four_times_the_points_per_minute_on_5x_as_on_20x():
    """The 2026-09-18 incident: ~6 pts/min on the 5x account read as ~1.7 on
    the 20x one for the same panes. Calibration: 5000 tokens per point per x."""
    cfg = _cfg()
    five, twenty = _tier("default_claude_max_5x"), _tier("default_claude_max_20x")
    assert cus.tokens_per_5h_point(five, cfg) == 25_000 and cus.tokens_per_5h_point(twenty, cfg) == 100_000
    p5, p20 = cus.projected_pts_per_min(100_000, five, cfg), cus.projected_pts_per_min(100_000, twenty, cfg)
    assert p5 == 4.0 and p20 == 1.0
    assert cus.minutes_until_wall(80.0, 100_000, five, cfg) == 20.0
    assert cus.minutes_until_wall(80.0, 100_000, twenty, cfg) == 80.0
    # the calibration is config, not code
    assert cus.tokens_per_5h_point(five, _cfg(plan_tiers={"tokens_per_5h_point_per_x": 8000})) == 40_000
    # unknowns stay unknown: no tier, no headroom, or no burn
    unknown = cus.resolve_plan_tier({}, NOW, cfg)
    assert cus.projected_pts_per_min(100_000, unknown, cfg) is None
    assert cus.minutes_until_wall(None, 100_000, five, cfg) is None
    assert cus.minutes_until_wall(80.0, 0, five, cfg) is None


# --------------------------------------------------------------------------
# The poll persists both records; the profile is read once an hour
# --------------------------------------------------------------------------

class _Resp:
    def __init__(self, payload: dict):
        self._b = json.dumps(payload).encode()

    def read(self, *_a):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _serve(calls, usage_status=200, tier="default_claude_max_20x"):
    def fake(req, timeout=None):
        calls.append(req.full_url)
        if req.full_url == cus.PROFILE_API_URL:
            return _Resp({"account": {"has_claude_max": True, "has_claude_pro": False},
                          "organization": {"organization_type": "claude_max", "rate_limit_tier": tier,
                                           "seat_tier": None, "subscription_status": "active"}})
        if usage_status == 429:
            raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests", {}, None)
        return _Resp({"five_hour": {"utilization": 8.0, "resets_at": None},
                      "seven_day": {"utilization": 20.0, "resets_at": None}})
    return fake


def test_poll_persists_profile_and_file_tier_and_reads_the_profile_once_an_hour(monkeypatch):
    env = _Env(accounts=("alpha", "beta"))
    try:
        env.set_config({})
        creds = env.accounts_dir / "account-alpha" / ".credentials.json"
        creds.write_text(json.dumps({"claudeAiOauth": {"accessToken": "at", "refreshToken": "rt-alpha",
                                                        "expiresAt": 2_000_000_000_000,
                                                        "subscriptionType": "max", "rateLimitTier": "default_claude_max_5x"}}))
        calls: list[str] = []
        monkeypatch.setattr(cus.urllib.request, "urlopen", _serve(calls))
        cus._PLAN_TIER_PROBE_CACHE.clear()
        u = cus.poll_account_usage("alpha")
        assert u.plan_tier_profile["raw"] == "default_claude_max_20x"          # live
        assert u.plan_tier_file["raw"] == "default_claude_max_5x"              # login-time file value
        assert calls.count(cus.PROFILE_API_URL) == 1
        state = cus.load_state()
        cus.update_state_with_usage(state, {"alpha": u})
        a = state["accounts"]["alpha"]
        assert a["plan_tier_profile"]["raw"] == "default_claude_max_20x" and a["plan_tier_file"]["raw"] == "default_claude_max_5x"
        t = cus.resolve_plan_tier(a, datetime.now(timezone.utc), cus.load_config())
        assert t["label"] == "20x" and t["mismatch"] is True and t["sizing_ok"] is True
        # a second poll within the hour: usage yes, profile no
        u2 = cus.poll_account_usage("alpha")
        assert calls.count(cus.PROFILE_API_URL) == 1 and calls.count(cus.USAGE_API_URL) == 2
        assert u2.plan_tier_profile is None and u2.plan_tier_file["raw"] == "default_claude_max_5x"
        cus.update_state_with_usage(state, {"alpha": u2})
        assert state["accounts"]["alpha"]["plan_tier_profile"]["raw"] == "default_claude_max_20x"   # kept
        # disabled => no profile GET at all, and nothing rendered as a default
        env.set_config({"plan_tiers": {"enabled": False}})
        cus._PLAN_TIER_PROBE_CACHE.clear(); calls.clear()
        u3 = cus.poll_account_usage("alpha")
        assert cus.PROFILE_API_URL not in calls and u3.plan_tier_profile is None and u3.plan_tier_file is None
    finally:
        cus._PLAN_TIER_PROBE_CACHE.clear()
        env.restore()


def test_a_429_poll_keeps_the_tier_the_subscription_probe_already_read(monkeypatch):
    env = _Env(accounts=("alpha", "beta"))
    try:
        env.set_config({})
        calls: list[str] = []
        monkeypatch.setattr(cus.urllib.request, "urlopen", _serve(calls, usage_status=429, tier="default_claude_max_5x"))
        cus._PLAN_TIER_PROBE_CACHE.clear(); cus._SUBSCRIPTION_PROBE_CACHE.clear()
        u = cus.poll_account_usage("alpha")
        assert u.raw.get("rate_limited") and u.plan_tier_profile["raw"] == "default_claude_max_5x"
        assert calls.count(cus.PROFILE_API_URL) == 1                          # one GET served both questions
        state = cus.load_state()
        cus.update_state_with_usage(state, {"alpha": u})
        assert state["accounts"]["alpha"]["plan_tier_profile"]["raw"] == "default_claude_max_5x"
    finally:
        cus._PLAN_TIER_PROBE_CACHE.clear(); cus._SUBSCRIPTION_PROBE_CACHE.clear()
        env.restore()


# --------------------------------------------------------------------------
# Surfaces: status, panes header, PTS/MIN~, --me, JSON; unknown never a default
# --------------------------------------------------------------------------

def _acct(pct5=20.0, **extra):
    a = {"current_5h_pct": pct5, "current_7d_pct": 5.0, "last_observed_ts": _iso(NOW - timedelta(minutes=2)),
         "five_hour_resets_at": _iso(NOW + timedelta(hours=3)), "burn_rate_5h_pct_per_min": 0.5}
    a.update(extra)
    return a


def test_panes_header_names_the_tier_with_its_source_and_age():
    fresh = _acct(plan_tier_profile={"raw": "default_claude_max_20x", "observed_ts": _iso(NOW - timedelta(minutes=12))})
    h = cus.account_headroom(fresh, NOW)
    assert h["plan_tier"]["label"] == "20x" and h["burn_5h_pct_per_min_measured"] == 0.5
    assert "plan 20x (profile 12m)" in cus._account_header("a", h, NOW)
    file_only = _acct(plan_tier_file={"raw": "default_claude_max_5x", "file_mtime_ts": _iso(NOW - timedelta(hours=3))})
    assert "plan 5x? (credential file 3h, unverified)" in cus._account_header("b", cus.account_headroom(file_only, NOW), NOW)
    assert "plan unknown" in cus._account_header("c", cus.account_headroom(_acct(), NOW), NOW)
    assert "plan unknown" in cus._account_header("d", cus.account_headroom(None, NOW), NOW)
    json.dumps(h)                                                             # serialisable as shipped


def test_panes_rows_carry_pts_per_min_only_when_the_tier_can_size(monkeypatch, tmp_path):
    from test_panes_view import _fake_env, _write_session, usage_line  # noqa: E402
    claude = _fake_env(monkeypatch, tmp_path)
    monkeypatch.setattr(cus, "read_peer_registry", lambda: {})
    monkeypatch.setattr(cus, "read_panes_from_reader", lambda include_all=True: [
        {"pane": "%20", "session": "x", "state": "working", "pane_pid": 11, "profile": "claude-code"}])
    monkeypatch.setattr(cus, "load_config", lambda: dict(cus.DEFAULT_CONFIG))
    for raw, expect_numeric in (("default_claude_max_20x", True), (None, False)):
        rec = _acct(plan_tier_profile={"raw": raw, "observed_ts": _iso(datetime.now(timezone.utc))}) if raw else _acct()
        monkeypatch.setattr(cus, "load_state", lambda rec=rec: {"slots": {"slot-7": {"account": "rayi5"}}, "accounts": {"rayi5": rec}})
        payload = cus.build_panes_payload(30)
        row = payload["panes"][0]
        assert row["unmeasured"] is True                                     # no transcript in this fixture
        assert row["burn_pts_per_min_est"] is None
        text = cus.render_panes_table(payload)
        assert "PTS/MIN~" in text and ("plan 20x" in text if raw else "plan unknown" in text)
    # a measured row: the estimate is NEW/MIN over the tier's tokens-per-point
    row = {"pane": "%1", "unmeasured": False, "burn_new_tokens_per_min": 50_000.0, "account": "a"}
    tier = cus.resolve_plan_tier({"plan_tier_profile": {"raw": "default_claude_max_5x", "observed_ts": _iso(NOW)}}, NOW, _cfg())
    assert cus.projected_pts_per_min(row["burn_new_tokens_per_min"], tier, _cfg()) == 2.0


def test_me_prints_plan_burn_both_ways_sizing_and_placement(monkeypatch, tmp_path):
    from test_panes_view import _pane_on, _pm_state, FABLE_HEAVY, _payload, NOW as PV_NOW  # noqa: E402
    state = _pm_state(10.0)
    # the panes fixture evaluates at its own fixed NOW; stamp the reading there
    state["accounts"]["rayi5"]["plan_tier_profile"] = {"raw": "default_claude_max_20x", "observed_ts": _iso(PV_NOW)}
    state["accounts"]["rayi5"]["burn_rate_5h_pct_per_min"] = 0.4
    row, head = _pane_on(monkeypatch, tmp_path, "s-tier", FABLE_HEAVY, state)
    other = cus.account_headroom({"current_5h_pct": 40.0, "current_7d_pct": 10.0,
                                  "last_observed_ts": _iso(datetime.now(timezone.utc)),
                                  "plan_tier_profile": {"raw": "default_claude_max_5x", "observed_ts": _iso(datetime.now(timezone.utc))}},
                                 datetime.now(timezone.utc))
    unknown = cus.account_headroom({"current_5h_pct": 1.0, "current_7d_pct": 1.0,
                                    "last_observed_ts": _iso(datetime.now(timezone.utc))}, datetime.now(timezone.utc))
    payload = _payload([row], {"rayi5": head, "small": other, "mystery": unknown})
    text = cus.render_me(payload, row)
    assert "PLAN: 20x (profile 0m)" in text
    assert "BURN:" in text and "new tokens/min measured (comparable across plans)" in text
    assert "5h-pts/min on this 20x account (estimate)" in text and "account measured 0.40 pts/min" in text
    assert "SIZING: at this burn the 5h headroom" in text and "on 20x" in text
    assert "PLACEMENT" in text and "small 5x 60% left ≈" in text and "mystery" in text   # unknown named, not estimated
    sz = cus.sizing_estimate(payload, row, head, _cfg())
    assert sz["pts_per_min_est"] == sz["new_tokens_per_min"] / 100_000
    assert sz["minutes_to_wall_est"] == pytest.approx(head["headroom_5h_pct"] / sz["pts_per_min_est"])
    small = next(t for t in sz["targets"] if t["account"] == "small")
    assert small["minutes_to_wall_est"] == pytest.approx(60.0 / (sz["new_tokens_per_min"] / 25_000))
    assert next(t for t in sz["targets"] if t["account"] == "mystery")["minutes_to_wall_est"] is None
    json.dumps(sz)
    # no tier anywhere: every estimate is None and the text says so
    row2, head2 = _pane_on(monkeypatch, tmp_path / "u", "s-u", FABLE_HEAVY, _pm_state(10.0))
    t2 = cus.render_me(_payload([row2], {"rayi5": head2}), row2)
    assert "PLAN: unknown — not used for sizing" in t2 and "SIZING: unknown" in t2


def test_status_shows_the_tier_with_source_or_unknown():
    env = _Env(accounts=("alpha", "beta"))
    try:
        env.set_config({})
        st = cus.load_state()
        st["accounts"]["alpha"]["plan_tier_profile"] = {"raw": "default_claude_max_20x", "observed_ts": _iso(datetime.now(timezone.utc))}
        st["accounts"]["beta"]["plan_tier_file"] = {"raw": "default_claude_max_5x", "file_mtime_ts": _iso(datetime.now(timezone.utc) - timedelta(hours=2))}
        cus.save_state(st)
        r = CliRunner().invoke(cus.cli, ["status"])
        assert r.exit_code == 0, r.output
        assert "plan=20x (profile 0m)" in r.output
        assert "plan=5x? (credential file 2h, unverified)" in r.output
        st = cus.load_state(); st["accounts"]["beta"].pop("plan_tier_file"); cus.save_state(st)
        r = CliRunner().invoke(cus.cli, ["status"])
        assert "plan=unknown" in r.output
    finally:
        env.restore()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
