"""Tests for MCP OAuth carry-over (2026-07-10).

The bug: Claude Code stores remote-MCP-server OAuth tokens (atlassian,
singlemcp-*) under the "mcpOAuth" key of the SAME .credentials.json that
carries the account's claudeAiOauth. cus installs that file wholesale at
every swap, so each swap clobbered the mount's MCP tokens with the account
snapshot's stale/empty mcpOAuth — sessions kept working from memory while on
disk every slot degraded to registration stubs, and every NEW session opened
to "needs authentication" (jira2a/slot-11, 2026-07-10).

The fix under test: a canonical store (ACCOUNTS_DIR/mcp-oauth.json) that
harvest_mcp_oauth() fills from any creds blob cus reads, and
inject_mcp_oauth() drains into any creds blob cus writes; plus the periodic
_sync_mcp_oauth() daemon pass that heals all live mounts.

Run standalone:  python3 tests/test_mcp_oauth_carryover.py
Or under pytest: pytest tests/test_mcp_oauth_carryover.py
"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cus  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _mcp_entry(token: str, refresh: str = "rt", expires_at: int | None = 9999999999999,
               server: str = "atlassian") -> dict:
    """A live-shaped mcpOAuth entry (what Claude Code writes after a
    successful browser auth)."""
    e = {
        "serverName": server,
        "serverUrl": f"https://mcp.{server}.example/",
        "clientId": "cid",
        "accessToken": token,
        "refreshToken": refresh,
        "redirectUri": "http://localhost:45454/callback",
        "discoveryState": "ok",
    }
    if expires_at is not None:
        e["expiresAt"] = expires_at
    return e


def _stub_entry(server: str = "atlassian") -> dict:
    """A registration-only stub (what a needs-auth server looks like on disk:
    clientId + discoveryState present, token fields EMPTY)."""
    return {
        "serverName": server,
        "serverUrl": f"https://mcp.{server}.example/",
        "clientId": "cid",
        "clientSecret": "cs",
        "accessToken": "",
        "refreshToken": "",
        "redirectUri": "http://localhost:45454/callback",
        "discoveryState": "ok",
    }


def _account_creds(refresh: str, access: str = "at", expires_at: int = 9999999999999) -> dict:
    return {"claudeAiOauth": {
        "accessToken": access,
        "refreshToken": refresh,
        "expiresAt": expires_at,
        "scopes": ["user:inference"],
        "subscriptionType": "max",
    }}


class _Env:
    """Throwaway on-disk tree with the cus path constants repointed at it
    (same setattr + restore() pattern as test_creds_backup)."""

    def __init__(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.accounts_dir = root / "claude-accounts"
        self.claude_dir = root / ".claude"
        self.claude_dir.mkdir(parents=True)
        self.accounts_dir.mkdir(parents=True)
        self.creds_json = self.claude_dir / ".credentials.json"
        self._saved = {k: getattr(cus, k) for k in (
            "ACCOUNTS_DIR", "CLAUDE_DIR", "CREDS_JSON", "MCP_OAUTH_STORE",
        )}
        cus.ACCOUNTS_DIR = self.accounts_dir
        cus.CLAUDE_DIR = self.claude_dir
        cus.CREDS_JSON = self.creds_json
        cus.MCP_OAUTH_STORE = self.accounts_dir / "mcp-oauth.json"

    def slot(self, name: str, creds: dict) -> Path:
        d = self.accounts_dir / name
        d.mkdir(exist_ok=True)
        p = d / ".credentials.json"
        p.write_text(json.dumps(creds))
        return p

    def account(self, name: str, creds: dict) -> Path:
        d = self.accounts_dir / f"account-{name}"
        d.mkdir(exist_ok=True)
        p = d / ".credentials.json"
        p.write_text(json.dumps(creds))
        return p

    def store(self) -> dict:
        p = self.accounts_dir / "mcp-oauth.json"
        return json.loads(p.read_text()) if p.exists() else {}

    def restore(self):
        for k, v in self._saved.items():
            setattr(cus, k, v)
        self._tmp.cleanup()


# ---------------------------------------------------------------------------
# Merge-rule unit tests
# ---------------------------------------------------------------------------

def test_entry_liveness():
    assert cus._mcp_entry_live(_mcp_entry("tok"))
    # Expired-but-refreshable is still live (Claude Code refreshes silently).
    assert cus._mcp_entry_live(_mcp_entry("", refresh="rt", expires_at=1))
    assert not cus._mcp_entry_live(_stub_entry())
    assert not cus._mcp_entry_live(None)
    assert not cus._mcp_entry_live("garbage")
    print("ok: liveness classification")


def test_merge_never_downgrades():
    fresh = _mcp_entry("fresh", expires_at=2_000)
    stale = _mcp_entry("stale", expires_at=1_000)
    dst = {"atlassian|abc": fresh}
    # A staler live entry must not replace a fresher one...
    assert cus._mcp_merge(dst, {"atlassian|abc": stale}) == 0
    assert dst["atlassian|abc"] is fresh
    # ...and a stub must never replace ANY live entry.
    assert cus._mcp_merge(dst, {"atlassian|abc": _stub_entry()}) == 0
    assert dst["atlassian|abc"] is fresh
    # A fresher live entry DOES replace a staler one.
    dst2 = {"atlassian|abc": stale}
    assert cus._mcp_merge(dst2, {"atlassian|abc": fresh}) == 1
    assert dst2["atlassian|abc"] == fresh
    # Live replaces a stub.
    dst3 = {"atlassian|abc": _stub_entry()}
    assert cus._mcp_merge(dst3, {"atlassian|abc": stale}) == 1
    print("ok: merge never downgrades")


def test_merge_is_idempotent_and_never_deletes():
    live = _mcp_entry("tok")
    dst = {"keepme|xyz": _stub_entry("keepme")}
    assert cus._mcp_merge(dst, {"atlassian|abc": live}) == 1
    assert cus._mcp_merge(dst, {"atlassian|abc": live}) == 0  # idempotent
    assert "keepme|xyz" in dst  # never deletes
    print("ok: merge idempotent + non-destructive")


# ---------------------------------------------------------------------------
# Harvest / inject round-trip
# ---------------------------------------------------------------------------

def test_harvest_then_inject_roundtrip():
    env = _Env()
    try:
        authed = _account_creds("rt-a")
        authed["mcpOAuth"] = {"atlassian|abc": _mcp_entry("tok"),
                              "singlemcp-staging|def": _stub_entry("singlemcp-staging")}
        assert cus.harvest_mcp_oauth(authed) == 1  # only the LIVE entry lands
        store = env.store()
        assert "atlassian|abc" in store["mcpOAuth"]
        assert "singlemcp-staging|def" not in store["mcpOAuth"]

        # A different mount, different account, stub-only mcpOAuth: injection
        # upgrades the stub and leaves claudeAiOauth byte-identical.
        other = _account_creds("rt-b")
        other["mcpOAuth"] = {"atlassian|abc": _stub_entry()}
        before_account = json.dumps(other["claudeAiOauth"])
        assert cus.inject_mcp_oauth(other) == 1
        assert other["mcpOAuth"]["atlassian|abc"]["accessToken"] == "tok"
        assert json.dumps(other["claudeAiOauth"]) == before_account
        # Second injection is a no-op → callers skip the file write.
        assert cus.inject_mcp_oauth(other) == 0
    finally:
        env.restore()
    print("ok: harvest→inject round-trip")


def test_harvest_ignores_blob_without_mcp_and_bad_store():
    env = _Env()
    try:
        assert cus.harvest_mcp_oauth(_account_creds("rt")) == 0
        assert cus.harvest_mcp_oauth(None) == 0
        # Corrupt store degrades to empty, not an exception.
        (env.accounts_dir / "mcp-oauth.json").write_text("{not json")
        assert cus._load_mcp_oauth_store() == {}
        blob = _account_creds("rt")
        blob["mcpOAuth"] = {"atlassian|abc": _mcp_entry("tok")}
        assert cus.harvest_mcp_oauth(blob) == 1  # store self-heals on next write
    finally:
        env.restore()
    print("ok: harvest edge cases")


# ---------------------------------------------------------------------------
# Periodic sync — the clobbered-slot heal
# ---------------------------------------------------------------------------

def test_sync_heals_clobbered_slots():
    env = _Env()
    try:
        # slot-1: a session authenticated atlassian here (live token on disk).
        c1 = _account_creds("rt-1")
        c1["mcpOAuth"] = {"atlassian|abc": _mcp_entry("tok", expires_at=2_000)}
        p1 = env.slot("slot-1", c1)
        # slot-2: post-swap clobber — registration stub only.
        c2 = _account_creds("rt-2")
        c2["mcpOAuth"] = {"atlassian|abc": _stub_entry()}
        p2 = env.slot("slot-2", c2)
        # An account snapshot parked an OLDER but refreshable token (the
        # pre-fix save-backs left these strays) — must be harvested but must
        # NOT beat slot-1's fresher one.
        snap = _account_creds("rt-def")
        snap["mcpOAuth"] = {"atlassian|abc": _mcp_entry("old", expires_at=1_000)}
        env.account("default", snap)
        # Shared mount exists with no mcpOAuth at all.
        env.creds_json.write_text(json.dumps(_account_creds("rt-shared")))

        state = {"slots": {"slot-1": {"account": "a"}, "slot-2": {"account": "b"}}}
        cus._sync_mcp_oauth(state, no_execute=False)

        # slot-2 healed with slot-1's FRESHER token.
        healed = json.loads(p2.read_text())
        assert healed["mcpOAuth"]["atlassian|abc"]["accessToken"] == "tok"
        assert healed["claudeAiOauth"]["refreshToken"] == "rt-2"  # account token untouched
        # shared mount gained the entry too.
        shared = json.loads(env.creds_json.read_text())
        assert shared["mcpOAuth"]["atlassian|abc"]["accessToken"] == "tok"
        # slot-1 unchanged (nothing fresher to inject → no write, no backup churn).
        assert json.loads(p1.read_text())["mcpOAuth"]["atlassian|abc"]["accessToken"] == "tok"
        assert not list((env.accounts_dir / "slot-1").glob(".credentials.json.bak.*"))
        # GH #79: the healed mount was backed up before the rewrite.
        assert list((env.accounts_dir / "slot-2").glob(".credentials.json.bak.*"))
        # Account snapshot is harvest-only: never rewritten by the sync.
        snap_after = json.loads((env.accounts_dir / "account-default" / ".credentials.json").read_text())
        assert snap_after["mcpOAuth"]["atlassian|abc"]["accessToken"] == "old"
        # Idempotent second run: no further writes.
        before = p2.read_bytes()
        cus._sync_mcp_oauth(state, no_execute=False)
        assert p2.read_bytes() == before
    finally:
        env.restore()
    print("ok: periodic sync heals clobbered slots")


def test_sync_no_execute_writes_nothing():
    env = _Env()
    try:
        c1 = _account_creds("rt-1")
        c1["mcpOAuth"] = {"atlassian|abc": _mcp_entry("tok")}
        env.slot("slot-1", c1)
        c2 = _account_creds("rt-2")
        p2 = env.slot("slot-2", c2)
        before = p2.read_bytes()
        cus._sync_mcp_oauth({"slots": {"slot-1": {}, "slot-2": {}}}, no_execute=True)
        assert p2.read_bytes() == before  # dry run: harvest happened, injection didn't
        assert "atlassian|abc" in env.store()["mcpOAuth"]
    finally:
        env.restore()
    print("ok: --no-execute dry run")


# ---------------------------------------------------------------------------
# Install-point behavior (execute_swap's write, exercised via its pieces)
# ---------------------------------------------------------------------------

def test_install_payload_merge_matches_install_point_semantics():
    """The install point harvests the OUTGOING live file then injects into
    the incoming payload — meaning tokens authenticated under account A
    survive a swap to account B. Reproduce that sequence directly."""
    env = _Env()
    try:
        outgoing = _account_creds("rt-out")
        outgoing["mcpOAuth"] = {"atlassian|abc": _mcp_entry("tok")}
        incoming = _account_creds("rt-in")  # snapshot with no MCP tokens at all
        cus.harvest_mcp_oauth(outgoing)
        assert cus.inject_mcp_oauth(incoming) == 1
        assert incoming["mcpOAuth"]["atlassian|abc"]["accessToken"] == "tok"
        assert incoming["claudeAiOauth"]["refreshToken"] == "rt-in"
    finally:
        env.restore()
    print("ok: swap install-point semantics")


if __name__ == "__main__":
    test_entry_liveness()
    test_merge_never_downgrades()
    test_merge_is_idempotent_and_never_deletes()
    test_harvest_then_inject_roundtrip()
    test_harvest_ignores_blob_without_mcp_and_bad_store()
    test_sync_heals_clobbered_slots()
    test_sync_no_execute_writes_nothing()
    test_install_payload_merge_matches_install_point_semantics()
    print("all mcp-oauth carry-over tests passed")
