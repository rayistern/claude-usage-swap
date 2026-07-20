#!/usr/bin/env python3
"""Render the two compact markdown tables the watchdog tick reports each interval.

Why this exists
---------------
The `/watch` skill's heartbeat used to be a single terse line. The operator asked
(2026-07-20) for every tick to instead show two at-a-glance markdown tables — an
ACCOUNTS table (the fleet's headroom + reset ETAs) and a PANES table (the protected
sessions and what account each is riding). Hand-building those every 15 minutes is
error-prone (two 7d-reset representations, per-model Fable, pane->slot->account
resolution), so this centralizes it into one command the tick just calls.

Data sources (ground truth, not the frozen pane statuslines):
  * `cus sessions --json`  -> live pane -> slot -> account, pool, 5h/7d/Fable, drift.
  * ~/claude-accounts/state.json -> per-account reset timestamps + current %s.
  * ~/claude-accounts/config.yaml -> which accounts are `disabled`.

The 7d reset is shown TWO ways on purpose (operator request):
  * "7d reset (72h)"  = the PROJECTED real refresh. The `seven_day` weekly budget
    actually refreshes every ~72h at a fixed UTC anchor (field finding 2026-07-05,
    gist: monperrus). cus rotates on THIS number, so it's the one that matters.
  * "7d reset (API)"  = the raw `seven_day_resets_at` the API reports. It points
    ~7 days out at the oldest-tokens-age-off boundary and is misleading — shown only
    so the two can be compared.

Usage:
  watch_tables.py                      # default active panes (see ACTIVE_DEFAULT)
  watch_tables.py torah2a %148 ratioef1a   # explicit active panes (session name OR pane id)
  watch_tables.py --active torah2a,trisso4,ratioef1a
Anything not passed as "active" still appears in the accounts table; the panes table
lists the active set first (starred) then any other live premium/work panes.
"""
from __future__ import annotations

import datetime
import json
import os
import subprocess
import sys

STATE = os.path.expanduser("~/claude-accounts/state.json")
CONFIG = os.path.expanduser("~/claude-accounts/config.yaml")

# The sessions the operator is actively using this watch window. Session NAMES here;
# the script maps each to its current pane id via tmux. Override on the CLI.
ACTIVE_DEFAULT = ["torah2a", "trisso4", "ratioef1a"]

# Thresholds mirrored from the watch skill's judging rules.
FABLE_GATE = 97      # premium daemon swaps a lane off at/above this per-model weekly %
FABLE_CLEAN = 90     # below this = a safe Fable landing zone
FIVE_H_HOT = 90      # 5h pressure


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _parse(ts: str | None) -> datetime.datetime | None:
    if not ts:
        return None
    return datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _rel(t: datetime.datetime | None, now: datetime.datetime) -> str:
    """Human relative ETA. '<48h' shown in hours, else days. Past = 'rolled off'."""
    if not t:
        return "—"
    dh = (t - now).total_seconds() / 3600
    if dh < 0:
        return "rolled off"          # window already elapsed with no usage
    return f"+{dh:.0f}h" if dh < 48 else f"+{dh / 24:.1f}d"


def _proj72(last_reset: str | None, now: datetime.datetime) -> datetime.datetime | None:
    """Roll the last OBSERVED 7d reset forward in whole 72h steps until it's future."""
    t = _parse(last_reset)
    if not t:
        return None
    while t <= now:
        t += datetime.timedelta(hours=72)
    return t


def _disabled_accounts() -> set[str]:
    try:
        import yaml  # PyYAML ships with the cus env
        cfg = yaml.safe_load(open(CONFIG))
        out = set()
        for a in cfg.get("accounts", []) or []:
            if isinstance(a, dict) and a.get("disabled"):
                out.add(a.get("name"))
        return out
    except Exception:
        return set()


def _sessions() -> list[dict]:
    try:
        raw = subprocess.run(
            ["cus", "sessions", "--json"], capture_output=True, text=True, timeout=30
        ).stdout
        return json.load(__import__("io").StringIO(raw)).get("sessions", [])
    except Exception:
        return []


def _pane_for(name_or_id: str) -> str | None:
    """Resolve a tmux session NAME (or pass-through a %pane id) to a live pane id."""
    if name_or_id.startswith("%"):
        return name_or_id
    try:
        out = subprocess.run(
            ["tmux", "list-panes", "-t", name_or_id, "-F", "#{pane_id}"],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip().splitlines()
        return out[0] if out else None
    except Exception:
        return None


def _fable(sess: dict) -> float | None:
    pmw = sess.get("per_model_weekly") or {}
    return pmw.get("Fable")


def _proc_account(pane: str | None, state: dict) -> tuple[str | None, str | None]:
    """Fallback when `cus sessions` can't resolve a pane (e.g. an orphan slot):
    read the pane's claude child CLAUDE_CONFIG_DIR -> slot dir -> state.json slots map.
    Returns (slot, account)."""
    if not pane:
        return None, None
    try:
        ppid = subprocess.run(
            ["tmux", "list-panes", "-t", pane, "-F", "#{pane_pid}"],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip().splitlines()
        if not ppid:
            return None, None
        kids = subprocess.run(
            ["pgrep", "-P", ppid[0]], capture_output=True, text=True, timeout=10
        ).stdout.split()
        for c in kids:
            env = open(f"/proc/{c}/environ", "rb").read().decode("utf-8", "replace")
            for kv in env.split("\0"):
                if kv.startswith("CLAUDE_CONFIG_DIR="):
                    slot = os.path.basename(kv.split("=", 1)[1].rstrip("/"))
                    sv = (state.get("slots", {}) or {}).get(slot)
                    acct = sv.get("account") if isinstance(sv, dict) else sv
                    return slot, acct
    except Exception:
        pass
    return None, None


def _acct_metrics(acct: str | None, state: dict) -> tuple:
    """Pull (5h, 7d, Fable) for an account straight from state.json."""
    if not acct:
        return None, None, None
    v = (state.get("accounts", {}) or {}).get(acct, {})
    fab = (v.get("per_model_weekly_pct") or {}).get("Fable")
    return v.get("current_5h_pct"), v.get("current_7d_pct"), fab


def _fmt_pct(v) -> str:
    return f"{v:.0f}%" if isinstance(v, (int, float)) else str(v)


def _status_word(fable, five_h, disabled: bool) -> str:
    if disabled:
        return "⛔ DISABLED"
    if isinstance(fable, (int, float)):
        if fable >= FABLE_GATE:
            return "⛔ Fable at gate"
        if fable >= FABLE_CLEAN:
            return "⚠ Fable high"
    if isinstance(five_h, (int, float)) and five_h >= FIVE_H_HOT:
        return "⚠ 5h hot"
    if isinstance(fable, (int, float)) and fable < 10:
        return "✓ CLEAN"
    return "✓ headroom"


def build() -> str:
    now = _now()
    state = json.load(open(STATE))
    accts = state.get("accounts", {})
    disabled = _disabled_accounts()
    sessions = _sessions()

    # account -> which active-pane sessions ride it (filled after we resolve panes)
    riders: dict[str, list[str]] = {}

    # ---- resolve the active panes ----
    active_names = ACTIVE_DEFAULT
    args = [a for a in sys.argv[1:] if a]
    if args:
        if args[0] == "--active" and len(args) > 1:
            active_names = [x for x in args[1].split(",") if x]
        else:
            active_names = args

    by_pane = {s.get("pane"): s for s in sessions}
    pane_rows = []
    for name in active_names:
        pane = _pane_for(name)
        s = by_pane.get(pane)
        if not s:
            # cus couldn't resolve this pane (orphan slot etc). Fall back to /proc.
            slot, acct = _proc_account(pane, state)
            if not acct:
                pane_rows.append((name, pane or "GONE", "—", "—", "—", None, None, None,
                                  "⛔ unresolved" if pane else "⛔ GONE (crashed?)"))
                continue
            five, seven, fab = _acct_metrics(acct, state)
            riders.setdefault(acct, []).append(name)
            pane_rows.append((name, pane, (slot or "—") + "*", "?", acct, five, seven, fab,
                              _status_word(fab, five, False) + " (proc-resolved)"))
            continue
        acct = s.get("account")
        riders.setdefault(acct, []).append(name)
        pane_rows.append((
            name, pane, s.get("slot", "—"), s.get("pool", "—"), acct,
            s.get("five_h_pct"), s.get("seven_d_pct"), _fable(s),
            _status_word(_fable(s), s.get("five_h_pct"), False),
        ))

    # ---- ACCOUNTS table (sorted cleanest-Fable first) ----
    def fable_of(a):
        f = (accts[a].get("per_model_weekly_pct") or {}).get("Fable")
        return f if isinstance(f, (int, float)) else 999
    order = sorted(accts, key=fable_of)

    L = []
    L.append(f"Repolled {now:%H:%M UTC}. Two 7d-reset columns: **72h** = the projected real "
             f"refresh cus rotates on; **API** = raw `seven_day_resets_at` (misleading, ~7d out).")
    L.append("")
    L.append("### Accounts — fleet state")
    L.append("")
    L.append("| Account | 5h | 7d | Fable | 5h reset | 7d reset (72h) | 7d reset (API) | Status |")
    L.append("|---------|----|----|-------|----------|----------------|----------------|--------|")
    for a in order:
        v = accts[a]
        fab = fable_of(a)
        fab = None if fab == 999 else fab
        five = v.get("current_5h_pct")
        seven = v.get("current_7d_pct")
        r5 = _rel(_parse(v.get("five_hour_resets_at")), now)
        r72 = _rel(_proj72(v.get("seven_day_last_reset_ts"), now), now)
        rapi = _rel(_parse(v.get("seven_day_resets_at")), now)
        who = ", ".join(riders.get(a, []))
        star = f" ← **{who}**" if who else ""
        status = _status_word(fab, five, a in disabled) + star
        L.append(f"| {'**'+a+'**' if who else a} | {_fmt_pct(five)} | {_fmt_pct(seven)} | "
                 f"`{_fmt_pct(fab)}` | {r5} | **{r72}** | {rapi} | {status} |")

    # ---- PANES table ----
    L.append("")
    L.append("### Active panes")
    L.append("")
    L.append("| Pane | id / slot | Pool | Account | 5h | 7d | Fable | Status |")
    L.append("|------|-----------|------|---------|----|----|-------|--------|")
    for (name, pane, slot, pool, acct, five, seven, fab, status) in pane_rows:
        L.append(f"| **{name}** | {pane} / {slot} | {pool} | {acct} | {_fmt_pct(five)} | "
                 f"{_fmt_pct(seven)} | `{_fmt_pct(fab)}` | {status} |")

    return "\n".join(L)


if __name__ == "__main__":
    print(build())
