"""Tests for the 2026-07-06 wrong-account canonical-store clobber guard.

THE INCIDENT (verified live 2026-07-06, recurring over two days): an account's
canonical store — account-<X>/.claude.json (identity) + .credentials.json
(tokens) — was silently overwritten with a DIFFERENT account's identity/token.
account-rayi1/.claude.json's oauthAccount was rayi2's and its credentials polled
identically to rayi2, while account-rayi1/meta.yaml AND the independent-login
families under logins/rayi1/family-* still correctly held rayi1's identity.

The vector: _execute_swap_locked's outgoing-account save-back copied the live
mount's identity into account-<current>/.claude.json with NO identity check (the
GH #3 guard only covered the credentials refresh-token lineage). A drifted mount
carried a wrong oauthAccount, and the save-back persisted it.

The fix: meta.yaml + login families are a TRUSTED ANCHOR (the buggy path never
writes them). A canonical-store write whose identity mismatches the anchor is
REFUSED and quarantined to a sidecar. A poisoned account (canonical .claude.json
disagrees with its anchor) is excluded as a swap target, and SOS names the
CANONICAL store — not the correct families — as the clobbered side.

Covers the four required cases:
  (a) mismatched-identity canonical write is refused + quarantined, not applied;
  (b) a matching write still succeeds;
  (c) a poisoned account is excluded from swap-target selection;
  (d) SOS names the canonical store (not the families) as the clobbered side.

Run standalone:  python3 tests/test_wrong_account_clobber_guard.py
Or under pytest: pytest tests/test_wrong_account_clobber_guard.py
"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cus  # noqa: E402


def _creds(refresh: str, access: str) -> dict:
    return {"claudeAiOauth": {
        "accessToken": access,
        "refreshToken": refresh,
        "expiresAt": 9999999999999,
        "scopes": ["user:inference"],
        "subscriptionType": "max",
    }}


def _cj(uuid: str, email: str) -> dict:
    """A .claude.json payload carrying an oauthAccount identity."""
    return {"userID": f"uid-{email}", "oauthAccount": {"accountUuid": uuid, "emailAddress": email}}


class _Env:
    """Throwaway on-disk account tree with every cus path constant repointed at
    it, so execute_swap / pick_swap_target / diagnose run for real without
    touching the live machine. Mirrors test_creds_saveback_drift's _Env, plus
    meta.yaml writing and independent-login family scaffolding (the trusted
    anchor sources the guard relies on)."""

    def __init__(self, accounts: dict[str, dict], active: str, live_creds: dict | bytes,
                 live_identity: dict | None = None):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        accounts_dir = root / "claude-accounts"
        claude_dir = root / ".claude"
        claude_dir.mkdir(parents=True)
        accounts_dir.mkdir(parents=True)

        self.creds_json = claude_dir / ".credentials.json"
        raw = live_creds if isinstance(live_creds, bytes) else json.dumps(live_creds).encode()
        self.creds_json.write_bytes(raw)
        self.claude_json = root / ".claude.json"
        # Live .claude.json: default to `active`'s identity unless overridden
        # (the drift case supplies a WRONG identity here).
        self.claude_json.write_text(json.dumps(live_identity or _cj(f"uuid-{active}", f"{active}@x")))

        self.accounts_dir = accounts_dir
        for name, creds in accounts.items():
            d = accounts_dir / f"account-{name}"
            d.mkdir()
            (d / ".credentials.json").write_text(json.dumps(creds))
            (d / ".claude.json").write_text(json.dumps(_cj(f"uuid-{name}", f"{name}@x")))

        self.state_json = accounts_dir / "state.json"
        self.state_json.write_text(json.dumps({
            "active": active,
            "accounts": {n: {"next_swap_at_pct": 50} for n in accounts},
            "swap_history": [],
        }))
        self.inbox_md = accounts_dir / "inbox.md"

        self._saved = {k: getattr(cus, k) for k in (
            "ACCOUNTS_DIR", "STATE_JSON", "CREDS_JSON", "CLAUDE_JSON",
            "CONFIG_YAML", "INBOX_MD", "migrate_account_dir",
        )}
        cus.ACCOUNTS_DIR = accounts_dir
        cus.STATE_JSON = self.state_json
        cus.CREDS_JSON = self.creds_json
        cus.CLAUDE_JSON = self.claude_json
        cus.CONFIG_YAML = accounts_dir / "config.yaml"   # absent → pure defaults
        cus.INBOX_MD = self.inbox_md
        # login_store_root() derives from ACCOUNTS_DIR, so families land under the
        # sandbox automatically once ACCOUNTS_DIR is repointed above.
        cus.migrate_account_dir = lambda d: {"action": "already_migrated"}

    # --- anchor-source writers -------------------------------------------
    def write_meta(self, account: str, uuid: str, email: str) -> None:
        d = self.accounts_dir / f"account-{account}"
        cus.write_yaml(d / "meta.yaml", {
            "name": account, "oauth_account_uuid": uuid, "oauth_email": email,
            "priority": 1, "locked_sessions": [],
        })

    def add_family(self, account: str, fam: str, uuid: str, email: str,
                   refresh: str = "rt-fam") -> None:
        """Scaffold a login family with a given identity — a second trusted
        anchor witness (browser-/login-time)."""
        fdir = cus.login_family_dir(account, fam)
        fdir.mkdir(parents=True, exist_ok=True)
        (fdir / ".claude.json").write_text(json.dumps(_cj(uuid, email)))
        (fdir / ".credentials.json").write_text(json.dumps(_creds(refresh, "at-fam")))

    # --- readers ----------------------------------------------------------
    def snapshot_creds(self, name: str) -> dict:
        return json.loads((self.accounts_dir / f"account-{name}" / ".credentials.json").read_text())

    def snapshot_cj(self, name: str) -> dict:
        return json.loads((self.accounts_dir / f"account-{name}" / ".claude.json").read_text())

    def rejected_sidecars(self, name: str, base: str) -> list[Path]:
        d = self.accounts_dir / f"account-{name}"
        return sorted(d.glob(f"{base}.rejected-*"))

    def restore(self):
        for k, v in self._saved.items():
            setattr(cus, k, v)
        self._tmp.cleanup()


# ---------------------------------------------------------------------------
# anchor / guard unit tests
# ---------------------------------------------------------------------------

def test_anchor_from_meta_and_families_agree():
    env = _Env({"rayi1": _creds("rt-1", "at-1")}, active="rayi1", live_creds=_creds("rt-1", "at-1"))
    try:
        env.write_meta("rayi1", "uuid-rayi1", "rayi1@x")
        env.add_family("rayi1", "family-1", "uuid-rayi1", "rayi1@x")
        anchor, source = cus.account_identity_anchor("rayi1")
        assert anchor == {"accountUuid": "uuid-rayi1", "emailAddress": "rayi1@x"}
        assert source == "meta+families"
    finally:
        env.restore()


def test_anchor_ambiguous_when_meta_and_family_disagree():
    """meta says rayi1, a family says rayi2 → don't guess → refuse-worthy."""
    env = _Env({"rayi1": _creds("rt-1", "at-1")}, active="rayi1", live_creds=_creds("rt-1", "at-1"))
    try:
        env.write_meta("rayi1", "uuid-rayi1", "rayi1@x")
        env.add_family("rayi1", "family-1", "uuid-rayi2", "rayi2@x")
        anchor, source = cus.account_identity_anchor("rayi1")
        assert anchor is None
        assert source.startswith("ambiguous")
        ok, _ = cus.guard_canonical_identity_write("rayi1", {"accountUuid": "uuid-rayi1"})
        assert ok is False  # ambiguous anchor → refuse rather than compound
    finally:
        env.restore()


def test_no_anchor_allows_write():
    """No meta, no families → cannot prove a mismatch → allow (backward compat;
    this is why the 458 existing meta-less tests keep passing)."""
    env = _Env({"rayi1": _creds("rt-1", "at-1")}, active="rayi1", live_creds=_creds("rt-1", "at-1"))
    try:
        ok, _ = cus.guard_canonical_identity_write("rayi1", {"accountUuid": "uuid-rayi2"})
        assert ok is True
    finally:
        env.restore()


def test_guard_refuses_mismatch_with_anchor():
    env = _Env({"rayi1": _creds("rt-1", "at-1")}, active="rayi1", live_creds=_creds("rt-1", "at-1"))
    try:
        env.write_meta("rayi1", "uuid-rayi1", "rayi1@x")
        env.add_family("rayi1", "family-1", "uuid-rayi1", "rayi1@x")
        ok, reason = cus.guard_canonical_identity_write(
            "rayi1", {"accountUuid": "uuid-rayi2", "emailAddress": "rayi2@x"})
        assert ok is False
        assert "rayi2" in reason and "anchor" in reason
    finally:
        env.restore()


# ---------------------------------------------------------------------------
# (a) mismatched-identity canonical write is refused + quarantined
# ---------------------------------------------------------------------------

def test_a_drifted_identity_saveback_refused_and_quarantined():
    """Swap rayi1 -> merkos while the LIVE mount has drifted to rayi2's identity.
    The outgoing (rayi1) canonical .claude.json must NOT be overwritten with
    rayi2's identity; the rejected identity is quarantined to a sidecar."""
    env = _Env(
        {"rayi1": _creds("rt-1", "at-1"), "merkos": _creds("rt-m", "at-m")},
        active="rayi1",
        # live creds carry rayi1 lineage (so classify verdict is 'expected',
        # isolating the IDENTITY vector under test), but the live .claude.json
        # identity is rayi2's — the drift.
        live_creds=_creds("rt-1", "at-1-refreshed"),
        live_identity=_cj("uuid-rayi2", "rayi2@x"))
    try:
        env.write_meta("rayi1", "uuid-rayi1", "rayi1@x")
        env.add_family("rayi1", "family-1", "uuid-rayi1", "rayi1@x")
        cus.execute_swap("merkos")
        # rayi1's canonical identity survived — still rayi1, NOT rayi2.
        assert env.snapshot_cj("rayi1")["oauthAccount"]["accountUuid"] == "uuid-rayi1"
        # the rejected wrong-account identity was quarantined beside it.
        assert env.rejected_sidecars("rayi1", ".claude.json"), "expected a .claude.json.rejected-* sidecar"
        assert "wrong-account-guard" in env.inbox_md.read_text()
        # the swap itself still completed.
        assert json.loads(env.state_json.read_text())["active"] == "merkos"
    finally:
        env.restore()


def test_a_drifted_unknown_lineage_tokens_refused():
    """Fully-drifted mount: live .claude.json is rayi2's AND the live tokens are
    rotation-shaped (match no snapshot → 'unknown'). The unknown-verdict token
    save-back into rayi1's snapshot must be refused when the identity was refused."""
    env = _Env(
        {"rayi1": _creds("rt-1-old", "at-1"), "merkos": _creds("rt-m", "at-m")},
        active="rayi1",
        live_creds=_creds("rt-DRIFTED-rotated", "at-x"),   # matches no snapshot → unknown
        live_identity=_cj("uuid-rayi2", "rayi2@x"))         # foreign identity
    try:
        env.write_meta("rayi1", "uuid-rayi1", "rayi1@x")
        env.add_family("rayi1", "family-1", "uuid-rayi1", "rayi1@x")
        cus.execute_swap("merkos")
        # rayi1's snapshot refresh token was NOT clobbered with the drifted token.
        assert env.snapshot_creds("rayi1")["claudeAiOauth"]["refreshToken"] == "rt-1-old"
        assert env.rejected_sidecars("rayi1", ".credentials.json"), "expected a quarantined token sidecar"
    finally:
        env.restore()


# ---------------------------------------------------------------------------
# (b) a matching write still succeeds
# ---------------------------------------------------------------------------

def test_b_matching_identity_saveback_succeeds():
    """No drift: the live mount's identity matches rayi1's anchor → the normal
    save-back proceeds and updates rayi1's canonical store (no quarantine)."""
    env = _Env(
        {"rayi1": _creds("rt-1", "at-1-old"), "merkos": _creds("rt-m", "at-m")},
        active="rayi1",
        live_creds=_creds("rt-1", "at-1-REFRESHED"),
        live_identity=_cj("uuid-rayi1", "rayi1@x"))
    try:
        env.write_meta("rayi1", "uuid-rayi1", "rayi1@x")
        env.add_family("rayi1", "family-1", "uuid-rayi1", "rayi1@x")
        cus.execute_swap("merkos")
        # save-back landed: rayi1's snapshot got the refreshed access token.
        assert env.snapshot_creds("rayi1")["claudeAiOauth"]["accessToken"] == "at-1-REFRESHED"
        # identity still rayi1, and NO rejection sidecar was written.
        assert env.snapshot_cj("rayi1")["oauthAccount"]["accountUuid"] == "uuid-rayi1"
        assert not env.rejected_sidecars("rayi1", ".claude.json")
        assert not env.rejected_sidecars("rayi1", ".credentials.json")
    finally:
        env.restore()


# ---------------------------------------------------------------------------
# (c) a poisoned account is excluded from swap-target selection
# ---------------------------------------------------------------------------

def test_c_poisoned_account_excluded_as_swap_target():
    """rayi1's canonical .claude.json holds rayi2's identity (poisoned) while its
    meta + family are correct. pick_swap_target must never return rayi1."""
    env = _Env(
        {"rayi1": _creds("rt-1", "at-1"), "merkos": _creds("rt-m", "at-m")},
        active="merkos", live_creds=_creds("rt-m", "at-m"))
    try:
        # Poison rayi1's canonical identity; keep meta + family correct.
        (env.accounts_dir / "account-rayi1" / ".claude.json").write_text(
            json.dumps(_cj("uuid-rayi2", "rayi2@x")))
        env.write_meta("rayi1", "uuid-rayi1", "rayi1@x")
        env.add_family("rayi1", "family-1", "uuid-rayi1", "rayi1@x")

        assert cus.account_canonical_store_poisoned("rayi1")[0] is True

        state = json.loads(env.state_json.read_text())
        # Give both accounts usage numbers so the picker has something to rank.
        state["accounts"]["rayi1"].update({"current_5h_pct": 5, "current_7d_pct": 5})
        state["accounts"]["merkos"].update({"current_5h_pct": 90, "current_7d_pct": 90})
        config = cus.load_config()
        tgt = cus.pick_swap_target(state, config)
        # rayi1 is the only non-active candidate, but it is poisoned → None.
        assert tgt is None, f"poisoned rayi1 must not be picked, got {tgt}"
    finally:
        env.restore()


def test_c_clean_account_still_pickable():
    """Control for (c): a clean rayi1 (canonical matches anchor) IS pickable."""
    env = _Env(
        {"rayi1": _creds("rt-1", "at-1"), "merkos": _creds("rt-m", "at-m")},
        active="merkos", live_creds=_creds("rt-m", "at-m"))
    try:
        env.write_meta("rayi1", "uuid-rayi1", "rayi1@x")
        env.add_family("rayi1", "family-1", "uuid-rayi1", "rayi1@x")
        assert cus.account_canonical_store_poisoned("rayi1")[0] is False
        state = json.loads(env.state_json.read_text())
        state["accounts"]["rayi1"].update({"current_5h_pct": 5, "current_7d_pct": 5})
        state["accounts"]["merkos"].update({"current_5h_pct": 90, "current_7d_pct": 90})
        tgt = cus.pick_swap_target(state, cus.load_config())
        assert tgt is not None and tgt.name == "rayi1"
    finally:
        env.restore()


# ---------------------------------------------------------------------------
# (d) SOS names the canonical store (not the families) as the clobbered side
# ---------------------------------------------------------------------------

def test_d_sos_names_canonical_store_as_clobbered():
    env = _Env(
        {"rayi1": _creds("rt-1", "at-1"), "merkos": _creds("rt-m", "at-m")},
        active="merkos", live_creds=_creds("rt-m", "at-m"))
    try:
        (env.accounts_dir / "account-rayi1" / ".claude.json").write_text(
            json.dumps(_cj("uuid-rayi2", "rayi2@x")))
        env.write_meta("rayi1", "uuid-rayi1", "rayi1@x")
        env.add_family("rayi1", "family-1", "uuid-rayi1", "rayi1@x")

        conds = cus.diagnose()
        poisoned = [c for c in conds if c.affected == "rayi1"
                    and "CANONICAL store is clobbered" in c.summary]
        assert poisoned, f"expected a canonical-clobber SOS for rayi1; got {[c.summary for c in conds]}"
        c = poisoned[0]
        # names the CANONICAL store as the bad side and points at relogin...
        assert "meta.yaml + login families are" in c.action and "correct" in c.action
        assert "cus relogin rayi1" in c.action
        # ...and does NOT tell the operator to re-provision the (correct) login.
        assert "login-mount" not in c.action
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
