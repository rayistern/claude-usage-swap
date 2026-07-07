# Diagnostics — reading a live `cus` deployment

A field playbook for the "which pane is on which account, and what's actually
blocking it?" questions. Written after a session where three panes showed
"maxed out" but `cus status` named the wrong accounts. Start with `cus sessions`
— it collapses the six manual probes below into one read-only view — then drop
to the individual checks when you need to confirm a detail.

```bash
cus sessions          # per-pane -> slot -> account -> binding-limit, in one shot
cus sessions --json   # same, machine-readable
```

---

## Mount topology: shared mount vs lanes vs hybrid

A live Claude pane authenticates as whatever account its **`CLAUDE_CONFIG_DIR`**
points at. There are three shapes:

- **Shared mount** (`mode: global`). Every pane runs bare — no
  `CLAUDE_CONFIG_DIR`, so they all read `~/.claude/.credentials.json`. `cus
  switch` / auto-swap rewrite that one file, so **all** bare panes follow the
  swap on their next request. One account at a time, machine-wide.
- **Lanes** (`mode: per_session`). Each pane is launched with
  `CLAUDE_CONFIG_DIR=~/claude-accounts/slot-N/`. A lane's account is independent
  of the shared mount and of every other lane. The daemon moves a lane **in
  place** (rewrites `slot-N/.credentials.json`) — it does **not** touch the
  shared mount.
- **Hybrid** (`mode: hybrid`). Both at once: bare panes follow the shared mount;
  lane panes each follow their own slot. This is the common production shape and
  the one where the wrong-account confusion below bites.

**How to tell which a given pane is on** — read the live process's env:

```bash
# pane %63's claude pid, then its CLAUDE_CONFIG_DIR:
pid=$(tmux list-panes -t %63 -F '#{pane_pid}')
cat /proc/$pid/environ | tr '\0' '\n' | grep CLAUDE_CONFIG_DIR
# empty  -> bare (shared mount, ~/.claude)
# .../slot-N -> lane
# .../account-X -> relogin/login-mount launch
```

`cus sessions` does this walk for every pane automatically (via `/proc`), so you
rarely need the manual version.

**Key consequence:** `cus switch <acct>` and background auto-swap **only move the
shared mount**. They do nothing to lanes. If your work is in a lane and you
`cus switch`, the lane keeps its own account — that's a common "I switched but
nothing changed" surprise.

---

## The per-pane → slot → account → binding-limit checks

`cus sessions` resolves, for each live pane, in this order:

1. **pane → mount** — walk the pane's process tree to the claude pid, read its
   `CLAUDE_CONFIG_DIR` (`/proc` ground truth, not the launch log).
2. **mount → account** — read the slot dir's `.claude.json` `oauthAccount` and
   reverse-map it to an account name. This is the **TRUE** account. It beats
   both the launch-time label in `sessions.log` and the daemon's recorded
   `state.slots[slot].account` (either can lag an in-place move).
3. **account → usage** — 5h %, 7d %, and per-model weekly (e.g. `Fable=98%`)
   from `state.json`.
4. **binding limit** — see the two-dimensional exhaustion section below.

Manually, that's `tmux list-panes` + `/proc` greps + `cus slot list` + `cus
status` + reading `state.json` + `cus force-poll`. `cus sessions` is the one
command that does all of it.

### DRIFT (GH #137)

If `sessions.log`'s launch-time account label disagrees with the resolved live
mount, `cus sessions` prints a red **DRIFT** tag. This is the exact defect
behind #137: `cus status` was showing the launch-time label (or, in
background/hybrid mode, `machine_active`) for panes that had since been moved to
a different slot's account. Trust the resolved account; the log label is only
where the session *started*.

### ORPHAN slots

A slot with live `/proc` pids that **no** live tmux pane resolves to. Two
shapes of the same leak: a pane was closed while a subprocess (hook shell,
subagent bash) kept the mount's `CLAUDE_CONFIG_DIR` alive, or a stray process
carries a slot's config dir with no pane at all. Idle-gc won't reap an orphan
(it still reads as "in use") and a fresh session won't mount it cleanly.
`cus sessions` lists them; investigate the pids before reusing the slot.

---

## Two-dimensional exhaustion

An account can be blocked on **either** of two independent axes — being under
one cap does not mean it's usable:

1. **5h / 7d rolling windows.** The aggregate utilization the ladder swaps on.
2. **Per-model weekly budget.** A separate weekly cap per model (e.g. Fable).
   An account can read `5h 0%` and still be blocked because `Fable=98%` is over
   the weekly gate.

So `5h 0%, 7d 58%, Fable 98%` is a **blocked** lane, not a healthy one — the
binding constraint is the Fable weekly cap. Conversely, an account with plenty
of usage headroom can still be unplaceable if there's **no free login family**
to mount it on (see the #104 divergence section): mount capacity and usage
headroom are different resources.

---

## premium vs standard pool and the Fable weekly gate

Each lane belongs to a **pool** (`state.slots[slot].pool`, default `premium`):

- **premium** honors the per-model weekly gate. When a tracked model reaches
  `per_model_weekly.cap_pct` (e.g. 97%), a premium lane on that account is
  force-swapped off, and the account is excluded as a swap **target** for
  premium lanes.
- **standard** ignores the model gate. A model-exhausted account still has
  aggregate headroom that serves standard-model work, so a standard lane keeps
  using it. `cus sessions` surfaces the model number but marks it *ignored*.

**Why `cus launch <acct>` onto a Fable-capped account gets swapped off a cycle
later:** a fresh lane defaults to the **premium** pool. If that account's Fable
weekly is over the gate, the premium force-swap evicts it on the next daemon
cycle. To pin a lane to an otherwise-capped account, launch it as standard:

```bash
cus launch <acct> --pool standard   # stay on this account; ignore the model gate
```

---

## The #104 divergence signature (live on N mounts without independent logins)

**Signature:** an account is live on more than one mount at once, but those
mounts share a single login (no independent login families). Each mount's use
refreshes the account's single-use OAuth token, rotating it out from under the
others — the families **DIVERGE** and the losing sessions get logged out.

**Remedy:**
- Exit the extra mounts so only one holds the account, or
- Relaunch the evicted sessions so they rejoin a valid login, and/or
- Provision independent logins so each mount has its own family:

```bash
cus login-mount <acct> <slot>   # give this (account, slot) its own login family
cus status                      # "Login pools" section shows family depth + free count
```

**Note:** an already-rotated token cannot be un-rotated. Once a session's
refresh token has been superseded, that session must **relogin** — there is no
way to restore the old token.

---

## The stale-poll gotcha

An **idle** account's cached `current_5h_pct` can read stale — e.g. it shows
`100%` (or a trailing `~`, meaning "last-known, not reconfirmed") when the 5h
window has actually reset to ~0. Polling backs off for idle accounts, so the
cached number can lag reality. Get the truth on demand:

```bash
cus force-poll <acct>    # poll this account now, ignoring the idle backoff
```

The real reset time lives in `state.json` under the account's
`five_hour_resets_at` (and `seven_day_resets_at` for the weekly window). If the
reset timestamp is in the past, a stale-looking `100%` is almost certainly a
window that has already rolled over — force-poll to confirm before treating the
account as exhausted.
