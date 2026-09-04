"""skills/pane_state.py is a SHIM since 2026-09-04: the classifier and its tests live in
vibeCoding/skills/build-babysitter/ (`tests/test_pane_state.py` there). This file pins only
the shim's contract — it resolves and execs the canonical copy, or says where to get it.

Every test copies the shim into a private layout so the sibling-checkout candidate
(`<repo>/../vibeCoding/...`) points into the temp dir, not at whatever this machine has.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

SHIM = Path(__file__).resolve().parent.parent / "skills" / "pane_state.py"

FAKE_READER = ("import json, sys\n"
               "print(json.dumps({'from': 'fake', 'argv': sys.argv[1:]}))\n"
               "sys.exit(7)\n")


def _layout(tmp_path: Path) -> tuple[Path, dict]:
    """A private copy of the shim at <tmp>/cus/skills/pane_state.py with HOME=<tmp>/home."""
    shim = tmp_path / "cus" / "skills" / "pane_state.py"
    shim.parent.mkdir(parents=True)
    shutil.copy(SHIM, shim)
    home = tmp_path / "home"
    home.mkdir()
    env = {k: v for k, v in os.environ.items() if k != "PANE_STATE_PY"}
    env["HOME"] = str(home)
    return shim, env


def _run(shim: Path, env: dict, *argv: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(shim), *argv], env=env, capture_output=True, text=True)


def test_env_override_is_execd_with_the_same_argv_and_exit_code(tmp_path):
    shim, env = _layout(tmp_path)
    fake = tmp_path / "reader.py"
    fake.write_text(FAKE_READER)
    env["PANE_STATE_PY"] = str(fake)
    r = _run(shim, env, "2zajac2a", "--table")
    assert r.returncode == 7, r.stderr
    assert json.loads(r.stdout) == {"from": "fake", "argv": ["2zajac2a", "--table"]}


def test_home_checkout_is_found_without_an_override(tmp_path):
    shim, env = _layout(tmp_path)
    canon = Path(env["HOME"]) / "repos" / "vibeCoding" / "skills" / "build-babysitter" / "pane_state.py"
    canon.parent.mkdir(parents=True)
    canon.write_text(FAKE_READER)
    r = _run(shim, env, "--all")
    assert r.returncode == 7 and json.loads(r.stdout)["argv"] == ["--all"]


def test_sibling_checkout_is_found_last(tmp_path):
    shim, env = _layout(tmp_path)
    canon = tmp_path / "vibeCoding" / "skills" / "build-babysitter" / "pane_state.py"
    canon.parent.mkdir(parents=True)
    canon.write_text(FAKE_READER)
    r = _run(shim, env)
    assert r.returncode == 7 and json.loads(r.stdout)["from"] == "fake"


def test_missing_everywhere_is_one_error_line_and_exit_3(tmp_path):
    shim, env = _layout(tmp_path)
    r = _run(shim, env, "2zajac2a")
    assert r.returncode == 3
    lines = r.stdout.strip().splitlines()
    assert len(lines) == 1
    doc = json.loads(lines[0])
    assert "vibeCoding/skills/build-babysitter" in doc["error"]
    assert "~/.claude/skills/build-babysitter" in doc["error"]
    assert len(doc["looked_in"]) == 3  # home repo + user skill link + sibling; no override set
    assert "state" not in doc  # never a fabricated pane row


def test_user_skill_link_is_a_candidate_between_home_and_sibling(tmp_path):
    shim, env = _layout(tmp_path)
    canon = Path(env["HOME"]) / ".claude" / "skills" / "build-babysitter" / "pane_state.py"
    canon.parent.mkdir(parents=True)
    canon.write_text(FAKE_READER)
    r = _run(shim, env)
    assert r.returncode == 7 and json.loads(r.stdout)["from"] == "fake"


def test_resolution_order_is_env_then_home_then_skill_link_then_sibling(tmp_path):
    shim, env = _layout(tmp_path)
    home = Path(env["HOME"])
    spots = {
        "env": tmp_path / "override.py",
        "home": home / "repos" / "vibeCoding" / "skills" / "build-babysitter" / "pane_state.py",
        "link": home / ".claude" / "skills" / "build-babysitter" / "pane_state.py",
        "sibling": tmp_path / "vibeCoding" / "skills" / "build-babysitter" / "pane_state.py",
    }
    for name, path in spots.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"print('{name}')\n")
    order = []
    for name in ("env", "home", "link", "sibling"):
        e = dict(env)
        if name == "env":
            e["PANE_STATE_PY"] = str(spots["env"])
        order.append(_run(shim, e).stdout.strip())
        spots[name].unlink()                     # remove the winner, the next one must win
    assert order == ["env", "home", "link", "sibling"]


def test_shim_never_execs_itself(tmp_path):
    """The old, still-documented reader path IS the shim; pointing the override (or a
    candidate link) at it must be a clean exit 3, not an endless os.execv loop."""
    shim, env = _layout(tmp_path)
    env["PANE_STATE_PY"] = str(shim)
    r = subprocess.run([sys.executable, str(shim), "x"], env=env, capture_output=True,
                       text=True, timeout=10)
    assert r.returncode == 3
    doc = json.loads(r.stdout)
    assert "resolved to this shim itself" in doc["error"]
    # the same through a candidate that is a symlink back to the shim
    env.pop("PANE_STATE_PY")
    link = Path(env["HOME"]) / "repos" / "vibeCoding" / "skills" / "build-babysitter" / "pane_state.py"
    link.parent.mkdir(parents=True)
    link.symlink_to(shim)
    r = subprocess.run([sys.executable, str(shim), "x"], env=env, capture_output=True,
                       text=True, timeout=10)
    assert r.returncode == 3 and "resolved to this shim itself" in json.loads(r.stdout)["error"]


def test_override_pointing_at_a_missing_file_does_not_fall_back(tmp_path):
    shim, env = _layout(tmp_path)
    canon = Path(env["HOME"]) / "repos" / "vibeCoding" / "skills" / "build-babysitter" / "pane_state.py"
    canon.parent.mkdir(parents=True)
    canon.write_text(FAKE_READER)  # a valid copy exists — the wrong override must still fail
    env["PANE_STATE_PY"] = str(tmp_path / "nope.py")
    r = _run(shim, env)
    assert r.returncode == 3
    doc = json.loads(r.stdout)
    assert "PANE_STATE_PY is set but is not a file" in doc["error"]
    assert doc["looked_in"] == [str(tmp_path / "nope.py")]   # only the override was considered


def test_override_expands_tilde(tmp_path):
    shim, env = _layout(tmp_path)
    fake = Path(env["HOME"]) / "reader.py"
    fake.write_text(FAKE_READER)
    env["PANE_STATE_PY"] = "~/reader.py"
    assert _run(shim, env).returncode == 7


def test_shim_against_the_real_canonical_copy_if_present():
    """On a box with the real reader installed, prove the shim IS the reader: --help is
    the reader's, and a name that matches no pane comes back as the reader's own
    not_found row (or the reader's exit-2 error line when tmux itself is unusable) —
    never the shim's exit 3. The classifier's contract is pinned upstream by
    vibeCoding's `skill-tests` CI job (skills/build-babysitter/tests/test_pane_state.py);
    this is the cross-repo smoke, and it skips where the copy is absent (cus CI)."""
    canon = Path.home() / "repos" / "vibeCoding" / "skills" / "build-babysitter" / "pane_state.py"
    if not canon.is_file():
        import pytest
        pytest.skip("canonical pane_state.py not on this machine")
    env = {k: v for k, v in os.environ.items() if k != "PANE_STATE_PY"}
    r = subprocess.run([sys.executable, str(SHIM), "--help"], env=env, capture_output=True, text=True)
    assert r.returncode == 0 and "tmux session names or pane ids" in r.stdout
    r = subprocess.run([sys.executable, str(SHIM), "no-such-pane-name-xyz"], env=env,
                       capture_output=True, text=True, timeout=30)
    doc = json.loads(r.stdout.strip().splitlines()[0])
    assert r.returncode in (0, 2) and (doc.get("state") == "not_found" or "error" in doc)
    assert r.returncode != 3
