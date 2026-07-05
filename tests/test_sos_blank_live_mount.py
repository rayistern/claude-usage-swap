"""Tests for the SOS "blanked/invalid live shared-mount" detector (GH #141, 2026-07-05).

Incident: the live shared-mount creds file ~/.claude/.credentials.json was found
BLANKED (expiresAt: 0, empty accessToken) while the active account's snapshot was
healthy. A bare session on the shared mount showed "not logged in", but `cus sos`
reported "All clear". SOS had no check for an invalid/blanked LIVE shared mount.

This suite covers:
  - the pure token-validity helper `_live_mount_creds_invalid`
  - the pure predicate `_diagnose_live_mount_creds` (blank/valid x mode; identity
    mismatch as a secondary signal)
  - the end-to-end path through `diagnose()` reading a real (temp) blank file.

Run standalone:  python3 tests/test_sos_blank_live_mount.py
Run under pytest: pytest tests/test_sos_blank_live_mount.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cus  # noqa: E402


def _valid_creds() -> dict:
    return {"claudeAiOauth": {"accessToken": "at-live", "refreshToken": "rt-live",
                              "expiresAt": 2_000_000_000_000}}


def _blank_creds() -> dict:
    """The exact 2026-07-05 incident signature: epoch expiresAt + empty token."""
    return {"claudeAiOauth": {"accessToken": "", "refreshToken": "", "expiresAt": 0}}


# --------------------------------------------------------------------------
# Pure helper: _live_mount_creds_invalid
# --------------------------------------------------------------------------

def test_live_mount_creds_invalid_signatures():
    assert cus._live_mount_creds_invalid(_valid_creds()) is False
    assert cus._live_mount_creds_invalid(_blank_creds()) is True
    # Missing / non-dict / empty payloads are all unusable.
    assert cus._live_mount_creds_invalid(None) is True
    assert cus._live_mount_creds_invalid({}) is True
    assert cus._live_mount_creds_invalid("not-a-dict") is True
    # expiresAt present + positive but no access token → invalid.
    assert cus._live_mount_creds_invalid(
        {"claudeAiOauth": {"accessToken": "", "expiresAt": 2_000_000_000_000}}) is True
    # access token present but expiresAt missing / <= 0 → invalid.
    assert cus._live_mount_creds_invalid({"claudeAiOauth": {"accessToken": "x"}}) is True
    assert cus._live_mount_creds_invalid(
        {"claudeAiOauth": {"accessToken": "x", "expiresAt": 0}}) is True
    # bool expiresAt must not read as a positive epoch (int subclass guard).
    assert cus._live_mount_creds_invalid(
        {"claudeAiOauth": {"accessToken": "x", "expiresAt": True}}) is True
    # Flat top-level OAuth shape is tolerated (still valid).
    assert cus._live_mount_creds_invalid(
        {"accessToken": "x", "expiresAt": 2_000_000_000_000}) is False


# --------------------------------------------------------------------------
# Pure predicate: _diagnose_live_mount_creds
# --------------------------------------------------------------------------

def test_predicate_blank_in_global_fires_with_restore_remedy():
    cond = cus._diagnose_live_mount_creds(_blank_creds(), "global", "merkos")
    assert cond is not None
    assert cond.severity == "urgent"
    assert "no valid token" in cond.summary
    assert "not logged in" in cond.summary
    # Exact remedy names the active account and --live.
    assert "cus restore-creds merkos --live" in cond.action


def test_predicate_blank_in_hybrid_fires():
    cond = cus._diagnose_live_mount_creds(_blank_creds(), "hybrid", "merkos")
    assert cond is not None
    assert cond.severity == "urgent"
    assert "cus restore-creds merkos --live" in cond.action


def test_predicate_valid_creds_no_condition():
    assert cus._diagnose_live_mount_creds(_valid_creds(), "global", "merkos") is None
    assert cus._diagnose_live_mount_creds(_valid_creds(), "hybrid", "merkos") is None


def test_predicate_per_session_blank_is_ignored():
    # Observe-only/authless shared mount by design → never fire, even blank.
    assert cus._diagnose_live_mount_creds(_blank_creds(), "per_session", "merkos") is None


def test_predicate_identity_mismatch_secondary_signal():
    # Token is valid but the live mount belongs to a different account.
    live = {"accountUuid": "uuid-wrong", "emailAddress": "wrong@x"}
    want = {"accountUuid": "uuid-right", "emailAddress": "right@x"}
    cond = cus._diagnose_live_mount_creds(_valid_creds(), "global", "merkos", live, want)
    assert cond is not None
    assert cond.severity == "urgent"
    assert "wrong account" in cond.summary
    assert "cus restore-creds merkos --live" in cond.action


def test_predicate_identity_match_no_condition():
    ident = {"accountUuid": "uuid-right", "emailAddress": "right@x"}
    assert cus._diagnose_live_mount_creds(_valid_creds(), "global", "merkos", ident, ident) is None


def test_predicate_identity_no_shared_evidence_no_condition():
    # _identities_match returns None (no shared keys) → treated as "can't say", no fire.
    live = {"emailAddress": "a@x"}
    want = {"accountUuid": "uuid-right"}
    assert cus._diagnose_live_mount_creds(_valid_creds(), "global", "merkos", live, want) is None


def test_predicate_blank_beats_identity():
    # When the token is blank, the primary (blank) condition wins regardless of identity.
    live = {"emailAddress": "wrong@x"}
    want = {"emailAddress": "right@x"}
    cond = cus._diagnose_live_mount_creds(_blank_creds(), "global", "merkos", live, want)
    assert cond is not None
    assert "no valid token" in cond.summary


# --------------------------------------------------------------------------
# End-to-end through diagnose() with a real (temp) live file
# --------------------------------------------------------------------------

class _Env:
    """Throwaway on-disk tree (same monkeypatch pattern as the other suites)."""

    def __init__(self, mode: str, live_creds: dict | None) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.claude_dir = root / ".claude"
        self.accounts_dir = root / "claude-accounts"
        (self.claude_dir / "projects").mkdir(parents=True)
        if live_creds is not None:
            (self.claude_dir / ".credentials.json").write_text(json.dumps(live_creds))
        # Live ~/.claude.json identity == the active account (no identity mismatch).
        (root / ".claude.json").write_text(
            json.dumps({"oauthAccount": {"accountUuid": "uuid-merkos",
                                         "emailAddress": "merkos@x"}}))
        # One healthy active account snapshot.
        d = self.accounts_dir / "account-merkos"
        d.mkdir(parents=True)
        (d / ".credentials.json").write_text(json.dumps(_valid_creds()))
        (d / ".claude.json").write_text(
            json.dumps({"oauthAccount": {"accountUuid": "uuid-merkos",
                                         "emailAddress": "merkos@x"}}))
        cus.write_json(self.accounts_dir / "state.json", {
            "active": "merkos",
            "accounts": {"merkos": {"next_swap_at_pct": 50,
                                    "current_5h_pct": 0.0, "current_7d_pct": 0.0}},
            "slots": {},
            "swap_history": [],
        })
        cus.write_yaml(self.accounts_dir / "config.yaml", {"mode": mode})

        self._saved = {k: getattr(cus, k) for k in
                       ("HOME", "CLAUDE_DIR", "CREDS_JSON", "CLAUDE_JSON",
                        "ACCOUNTS_DIR", "STATE_JSON", "CONFIG_YAML")}
        cus.HOME = root
        cus.CLAUDE_DIR = self.claude_dir
        cus.CREDS_JSON = self.claude_dir / ".credentials.json"
        cus.CLAUDE_JSON = root / ".claude.json"
        cus.ACCOUNTS_DIR = self.accounts_dir
        cus.STATE_JSON = self.accounts_dir / "state.json"
        cus.CONFIG_YAML = self.accounts_dir / "config.yaml"

    def restore(self) -> None:
        for k, v in self._saved.items():
            setattr(cus, k, v)
        self._tmp.cleanup()


def _live_conditions(conditions):
    return [c for c in conditions if "shared mount ~/.claude/.credentials.json" in c.summary]


def test_diagnose_blank_live_creds_global_fires():
    env = _Env("global", _blank_creds())
    try:
        conds = _live_conditions(cus.diagnose())
        assert len(conds) == 1
        assert conds[0].severity == "urgent"
        assert "cus restore-creds merkos --live" in conds[0].action
    finally:
        env.restore()


def test_diagnose_missing_live_creds_global_fires():
    # No live .credentials.json at all (logged out) also locks bare sessions out.
    env = _Env("global", None)
    try:
        conds = _live_conditions(cus.diagnose())
        assert len(conds) == 1
        assert conds[0].severity == "urgent"
    finally:
        env.restore()


def test_diagnose_valid_live_creds_no_new_condition():
    env = _Env("global", _valid_creds())
    try:
        assert _live_conditions(cus.diagnose()) == []
    finally:
        env.restore()


def test_diagnose_per_session_blank_is_ignored():
    env = _Env("per_session", _blank_creds())
    try:
        assert _live_conditions(cus.diagnose()) == []
    finally:
        env.restore()


if __name__ == "__main__":
    import traceback

    funcs = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in funcs:
        try:
            fn()
            print(f"  ok   {fn.__name__}")
        except Exception:  # noqa: BLE001
            failed += 1
            print(f"  FAIL {fn.__name__}")
            traceback.print_exc()
    print(f"\n{len(funcs) - failed}/{len(funcs)} passed")
    sys.exit(1 if failed else 0)
