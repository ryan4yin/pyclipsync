#!/usr/bin/env python3
"""Integration tests for pyclipsync.

Runs the daemon under a live X11 (XWayland) + Wayland session and verifies
clipboard sync for every supported type in both directions:

  text/plain, text/uri-list, image/png, image/jpeg, text/html (both ways)
  x-special/gnome-copied-files (QQ stickers) -> text/uri-list on Wayland
  rapid double copy must converge to the last copy (no echo ping-pong)

Usage (on a host with a running graphical session):

    python3 -m unittest discover -v          # test the repo's pyclipsync.py
    PYCLIPSYNC=/path/to/binary python3 -m unittest discover -v
    # test a built binary, e.g.:
    PYCLIPSYNC=$(nix build .#default --print-out-paths)/bin/pyclipsync \
        python3 -m unittest discover -v

Requires DISPLAY, WAYLAND_DISPLAY and wl-copy/wl-paste/xclip/clipnotify in
PATH; the whole module skips otherwise. Standard library only.
"""

from __future__ import annotations

import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
DAEMON_CMD = (
    [os.environ["PYCLIPSYNC"]]
    if "PYCLIPSYNC" in os.environ
    else [sys.executable, str(REPO_ROOT / "pyclipsync.py")]
)

SYNC_TIMEOUT = 10.0
POLL_INTERVAL = 0.25


def _missing() -> str:
    if not os.environ.get("DISPLAY"):
        return "DISPLAY not set (need a live X11 session)"
    if not os.environ.get("WAYLAND_DISPLAY"):
        return "WAYLAND_DISPLAY not set (need a live Wayland session)"
    for tool in ("wl-copy", "wl-paste", "xclip", "clipnotify"):
        if shutil.which(tool) is None:
            return f"'{tool}' not found in PATH"
    return ""


@unittest.skipIf(_missing(), f"skipped: {_missing() or 'no live session'}")
class SyncTest(unittest.TestCase):
    """Sequential sync tests against one daemon instance.

    Tests share the two clipboards, so order matters: method names are
    numbered (unittest runs them in sorted name order).
    """

    _failed = False

    @classmethod
    def setUpClass(cls) -> None:
        cls.workdir_path = Path(tempfile.mkdtemp(prefix="pyclipsync-test."))
        cls.log_path = cls.workdir_path / "daemon.log"
        cls.log_file = open(cls.log_path, "wb")
        cls.proc = subprocess.Popen(
            DAEMON_CMD,
            stdout=cls.log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        cls._wait_started(timeout=15.0)
        time.sleep(2.0)  # let the startup sync settle

    @classmethod
    def _wait_started(cls, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if cls.proc.poll() is not None:
                raise RuntimeError(
                    f"daemon exited early (rc={cls.proc.returncode}), "
                    f"log: {cls.log_path}"
                )
            if "pyclipsync started" in cls.log_path.read_text(errors="replace"):
                return
            time.sleep(0.1)
        raise RuntimeError(f"daemon did not start in time, log: {cls.log_path}")

    @classmethod
    def tearDownClass(cls) -> None:
        if cls.proc.poll() is None:
            cls.proc.terminate()
            try:
                cls.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                cls.proc.kill()
                cls.proc.wait(timeout=5)
        cls.log_file.close()
        if cls._failed:
            print(f"\npyclipsync test workdir kept: {cls.workdir_path}")
            print("--- daemon log (tail) ---")
            lines = cls.log_path.read_text(errors="replace").splitlines()
            print("\n".join(lines[-40:]))
        else:
            shutil.rmtree(cls.workdir_path, ignore_errors=True)

    def run(self, result):
        super().run(result)
        if result.failures or result.errors:
            SyncTest._failed = True

    # -- clipboard plumbing --------------------------------------------------

    def write_w(self, mime: str, data: bytes) -> None:
        subprocess.run(
            ["wl-copy", "--type", mime],
            input=data,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
            timeout=15,
        )

    def set_x(self, target: str, data: bytes) -> None:
        """Make the X CLIPBOARD owner, detached like a GUI app would be."""
        p = subprocess.Popen(
            ["xclip", "-selection", "clipboard", "-t", target],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        assert p.stdin is not None
        p.stdin.write(data)
        p.stdin.close()
        p.wait(timeout=5)  # foreground exits; the forked owner child persists

    def read_x(self, target: str) -> bytes:
        return subprocess.run(
            ["xclip", "-o", "-selection", "clipboard", "-t", target],
            capture_output=True,
            timeout=10,
        ).stdout

    def read_w(self, mime: str) -> bytes:
        return subprocess.run(
            ["wl-paste", "--type", mime],
            capture_output=True,
            timeout=10,
        ).stdout

    @staticmethod
    def wl_text(data: bytes) -> bytes:
        """What `wl-copy` actually stores on the Wayland side for text/*.

        wl-copy appends exactly one trailing newline to piped text input
        (binary mimes pass through byte-exact), so any text payload placed
        via wl-copy -- or pushed by the daemon via wl-copy -- ends up with
        a trailing newline. The daemon records the *measured* destination
        state, so sync stays consistent despite this.
        """
        return data + b"\n"

    def wait_value(self, read, expected: bytes) -> bytes:
        """Poll `read()` until it returns `expected`; fail on timeout."""
        deadline = time.monotonic() + SYNC_TIMEOUT
        actual = b""
        while True:
            try:
                actual = read()
            except (subprocess.SubprocessError, OSError):
                actual = b""
            if actual == expected:
                return actual
            if time.monotonic() >= deadline:
                break
            time.sleep(POLL_INTERVAL)
        self.fail(
            f"not synced within {SYNC_TIMEOUT}s: "
            f"expected {len(expected)} bytes, got {len(actual)} bytes: "
            f"{actual[:64]!r}"
        )

    # -- W -> X ---------------------------------------------------------------

    def test_01_w2x_text(self):
        data = f"tc-w2x-text-{secrets.token_hex(4)}".encode()
        self.write_w("text/plain", data)
        # wl-copy stored wl_text(data) on W; the daemon must mirror W byte-exact
        self.wait_value(lambda: self.read_x("UTF8_STRING"), self.wl_text(data))

    def test_02_w2x_uri(self):
        data = f"file:///tmp/img-{secrets.token_hex(4)}.png\n".encode()
        self.write_w("text/uri-list", data)
        self.wait_value(lambda: self.read_x("text/uri-list"), data)

    def test_03_w2x_png(self):
        data = secrets.token_bytes(512)
        self.write_w("image/png", data)
        self.wait_value(lambda: self.read_x("image/png"), data)

    def test_04_w2x_jpeg(self):
        data = secrets.token_bytes(640)
        self.write_w("image/jpeg", data)
        self.wait_value(lambda: self.read_x("image/jpeg"), data)

    def test_05_w2x_html(self):
        data = f"<b>tc-w2x-html-{secrets.token_hex(4)}</b>".encode()
        self.write_w("text/html", data)
        self.wait_value(lambda: self.read_x("text/html"), self.wl_text(data))

    # -- X -> W ----------------------------------------------------------------

    def test_06_x2w_text(self):
        data = f"tc-x2w-text-{secrets.token_hex(4)}".encode()
        self.set_x("UTF8_STRING", data)
        # X has `data` (no trailing \n); wl-copy lands it on W with one \n
        self.wait_value(lambda: self.read_w("text/plain"), self.wl_text(data))

    def test_07_x2w_qq_sticker(self):
        """QQ sticker: X gnome-copied-files (with 'copy' header) becomes a
        clean text/uri-list on the Wayland side."""
        name = f"sticker-{secrets.token_hex(4)}.png"
        self.set_x("x-special/gnome-copied-files", f"copy\nfile:///tmp/{name}\n".encode())
        self.wait_value(lambda: self.read_w("text/uri-list"), f"file:///tmp/{name}\n".encode())

    def test_08_x2w_png(self):
        data = secrets.token_bytes(777)
        self.set_x("image/png", data)
        self.wait_value(lambda: self.read_w("image/png"), data)

    def test_09_x2w_jpeg(self):
        data = secrets.token_bytes(891)
        self.set_x("image/jpeg", data)
        self.wait_value(lambda: self.read_w("image/jpeg"), data)

    def test_10_x2w_html(self):
        data = f"<i>tc-x2w-html-{secrets.token_hex(4)}</i>".encode()
        self.set_x("text/html", data)
        self.wait_value(lambda: self.read_w("text/html"), self.wl_text(data))

    # -- race -------------------------------------------------------------------

    def test_11_w2x_rapid_double(self):
        """Rapid double copy: the last copy must win (no echo ping-pong)."""
        first = f"tc-rapid-first-{secrets.token_hex(4)}".encode()
        last = f"tc-rapid-last-{secrets.token_hex(4)}".encode()
        self.write_w("text/plain", first)
        time.sleep(0.2)
        self.write_w("text/plain", last)
        self.wait_value(lambda: self.read_x("UTF8_STRING"), self.wl_text(last))

    # -- log hygiene --------------------------------------------------------------

    def test_99_log_no_errors(self):
        log = self.log_path.read_text(errors="replace")
        bad = [l for l in log.splitlines() if " ERROR " in l or "Traceback" in l]
        self.assertFalse(bad, "daemon log contains errors:\n" + "\n".join(bad))
        warns = [l for l in log.splitlines() if " WARNING " in l]
        if warns:
            print("\npyclipsync warnings (non-fatal):\n" + "\n".join(warns))


class UnreadableOfferLogTest(unittest.TestCase):
    """Unit tests for the unreadable-offer diagnostic (no live session needed)."""

    def setUp(self) -> None:
        if str(REPO_ROOT) not in sys.path:
            sys.path.insert(0, str(REPO_ROOT))
        import pyclipsync

        self.pc = pyclipsync
        pyclipsync._miss_log.clear()

    def test_supported_unreadable_offer_is_logged(self):
        with self.assertLogs("pyclipsync", level="WARNING") as cm:
            self.pc._log_unreadable(
                "Wayland clipboard",
                {"image/png", "text/plain"},
                self.pc.W_SUPPORTED,
            )
        self.assertIn("image/png", cm.output[0])

    def test_unknown_offers_are_ignored(self):
        with self.assertNoLogs("pyclipsync", level="WARNING"):
            self.pc._log_unreadable(
                "Wayland clipboard", {"application/x-foo"}, self.pc.W_SUPPORTED
            )

    def test_repeated_identical_offer_is_throttled(self):
        with self.assertLogs("pyclipsync", level="WARNING"):
            self.pc._log_unreadable(
                "Wayland clipboard", {"image/png"}, self.pc.W_SUPPORTED
            )
        with self.assertNoLogs("pyclipsync", level="WARNING"):
            self.pc._log_unreadable(
                "Wayland clipboard", {"image/png"}, self.pc.W_SUPPORTED
            )


class WatchRecycleTest(unittest.TestCase):
    """Unit tests for the watcher recycle helper (no live session needed)."""

    def setUp(self) -> None:
        if str(REPO_ROOT) not in sys.path:
            sys.path.insert(0, str(REPO_ROOT))
        import pyclipsync

        self.pc = pyclipsync

    def test_recycles_a_hung_command(self):
        events = []
        start = time.monotonic()
        self.pc._watch_once(
            ["sh", "-c", "echo tick; exec sleep 30"], lambda: events.append(1), 0.3
        )
        self.assertEqual(events, [1])
        self.assertLess(time.monotonic() - start, 5.0)

    def test_returns_when_command_exits(self):
        events = []
        start = time.monotonic()
        self.pc._watch_once(["sh", "-c", "echo once"], lambda: events.append(1), 30)
        self.assertEqual(events, [1])
        self.assertLess(time.monotonic() - start, 5.0)


class WatchLoopTest(unittest.TestCase):
    """Unit tests for the resilient watcher loop (no live session needed)."""

    class _Stop(BaseException):
        """Raised by fake watchers to break out of the otherwise infinite loop."""

    def setUp(self) -> None:
        if str(REPO_ROOT) not in sys.path:
            sys.path.insert(0, str(REPO_ROOT))
        import pyclipsync

        self.pc = pyclipsync

    def test_survives_failures(self):
        calls = []

        def once():
            calls.append(1)
            if len(calls) >= 3:
                raise self._Stop
            raise RuntimeError("boom")

        with patch.object(self.pc.time, "sleep", lambda _d: None), patch.object(
            self.pc.log, "exception"
        ):
            with self.assertRaises(self._Stop):
                self.pc._watch_loop(once, "test")
        self.assertEqual(len(calls), 3)

    def test_backoff_doubles_until_a_long_run_resets_it(self):
        sleeps = []
        calls = []

        def once():
            calls.append(1)
            if len(calls) >= 4:
                raise self._Stop
            raise RuntimeError("boom")

        with patch.object(self.pc.time, "sleep", sleeps.append), patch.object(
            self.pc.log, "exception"
        ):
            with self.assertRaises(self._Stop):
                self.pc._watch_loop(once, "test")
        self.assertEqual(sleeps, [0.2, 0.4, 0.8])


class WStateTextTest(unittest.TestCase):
    """w_state() must read charset-qualified text, not just bare text/plain."""

    def setUp(self) -> None:
        if str(REPO_ROOT) not in sys.path:
            sys.path.insert(0, str(REPO_ROOT))
        import pyclipsync

        self.pc = pyclipsync

    def test_reads_each_text_variant(self):
        for mime in self.pc.W_TEXT_TYPES:
            with self.subTest(mime=mime):
                with patch.object(
                    self.pc, "wl_types", return_value={mime}
                ), patch.object(self.pc, "wl_read", return_value=b"hi"):
                    state = self.pc.w_state()
                self.assertIsNotNone(state)
                self.assertEqual(state[0], "text")


class EnvSecondsTest(unittest.TestCase):
    """Unit tests for the positive-float env parser."""

    def setUp(self) -> None:
        if str(REPO_ROOT) not in sys.path:
            sys.path.insert(0, str(REPO_ROOT))
        import pyclipsync

        self.pc = pyclipsync
        self.name = "PYCLIPSYNC_TEST_SECONDS"

    def test_unset_uses_default(self):
        with patch.dict(self.pc.os.environ, {}, clear=False):
            self.pc.os.environ.pop(self.name, None)
            self.assertEqual(self.pc._env_seconds(self.name, 5.0), 5.0)

    def test_value_is_parsed(self):
        with patch.dict(self.pc.os.environ, {self.name: "2.5"}):
            self.assertEqual(self.pc._env_seconds(self.name, 5.0), 2.5)

    def test_bad_value_falls_back(self):
        with patch.dict(self.pc.os.environ, {self.name: "abc"}), patch.object(
            self.pc.log, "warning"
        ):
            self.assertEqual(self.pc._env_seconds(self.name, 5.0), 5.0)

    def test_nonpositive_falls_back(self):
        with patch.dict(self.pc.os.environ, {self.name: "0"}), patch.object(
            self.pc.log, "warning"
        ):
            self.assertEqual(self.pc._env_seconds(self.name, 5.0), 5.0)


class OwnerLifecycleTest(unittest.TestCase):
    """Unit tests for spawning, tracking and cleaning up clipboard owners."""

    def setUp(self) -> None:
        if str(REPO_ROOT) not in sys.path:
            sys.path.insert(0, str(REPO_ROOT))
        import pyclipsync

        self.pc = pyclipsync
        with pyclipsync._owner_lock:
            pyclipsync._owner_pgids.clear()

    def tearDown(self) -> None:
        with self.pc._owner_lock:
            self.pc._owner_pgids.clear()

    def test_spawn_owner_records_process_group(self):
        self.assertTrue(self.pc._spawn_owner(["sh", "-c", "exit 0"], b"data"))
        with self.pc._owner_lock:
            self.assertEqual(len(self.pc._owner_pgids), 1)

    def test_spawn_owner_timeout_is_not_recorded(self):
        with patch.object(self.pc, "CLIPBOARD_TIMEOUT", 0.2), patch.object(
            self.pc.log, "warning"
        ):
            self.assertFalse(
                self.pc._spawn_owner(["sh", "-c", "exec sleep 30"], b"data")
            )
        with self.pc._owner_lock:
            self.assertEqual(self.pc._owner_pgids, set())

    def test_group_alive(self):
        with patch.object(self.pc.os, "killpg", side_effect=ProcessLookupError):
            self.assertFalse(self.pc._group_alive(1))
        with patch.object(self.pc.os, "killpg", side_effect=PermissionError):
            self.assertTrue(self.pc._group_alive(1))
        with patch.object(self.pc.os, "killpg"):
            self.assertTrue(self.pc._group_alive(1))

    def test_prune_owners_drops_dead_groups(self):
        with self.pc._owner_lock:
            self.pc._owner_pgids.update({111, 222})
        with patch.object(
            self.pc, "_group_alive", side_effect=lambda pgid: pgid == 222
        ):
            self.pc._prune_owners()
        with self.pc._owner_lock:
            self.assertEqual(self.pc._owner_pgids, {222})

    def test_cleanup_kills_live_groups(self):
        with self.pc._owner_lock:
            self.pc._owner_pgids.update({111111, 222222})
        killed = []
        with patch.object(self.pc, "_group_alive", return_value=True), patch.object(
            self.pc, "_kill_group", side_effect=lambda pgid, _sig: killed.append(pgid)
        ):
            self.pc._cleanup_owners()
        self.assertEqual(sorted(killed), [111111, 222222])
        with self.pc._owner_lock:
            self.assertEqual(self.pc._owner_pgids, set())

    def test_cleanup_skips_dead_groups(self):
        with self.pc._owner_lock:
            self.pc._owner_pgids.update({111111, 222222})
        killed = []
        with patch.object(
            self.pc, "_group_alive", side_effect=lambda pgid: pgid == 222222
        ), patch.object(
            self.pc, "_kill_group", side_effect=lambda pgid, _sig: killed.append(pgid)
        ):
            self.pc._cleanup_owners()
        self.assertEqual(killed, [222222])


class WatcherLifecycleTest(unittest.TestCase):
    """Unit tests for tracking and terminating watcher children."""

    def setUp(self) -> None:
        if str(REPO_ROOT) not in sys.path:
            sys.path.insert(0, str(REPO_ROOT))
        import pyclipsync

        self.pc = pyclipsync
        with pyclipsync._watcher_lock:
            pyclipsync._watcher_procs.clear()

    def tearDown(self) -> None:
        with self.pc._watcher_lock:
            self.pc._watcher_procs.clear()

    def test_cleanup_terminates_and_clears(self):
        class FakeProc:
            def __init__(self):
                self.terminated = False

            def terminate(self):
                self.terminated = True

        a, b = FakeProc(), FakeProc()
        with self.pc._watcher_lock:
            self.pc._watcher_procs.update({a, b})
        self.pc._cleanup_watchers()
        self.assertTrue(a.terminated)
        self.assertTrue(b.terminated)
        with self.pc._watcher_lock:
            self.assertEqual(self.pc._watcher_procs, set())


if __name__ == "__main__":
    unittest.main(verbosity=2)
