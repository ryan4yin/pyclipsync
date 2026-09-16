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
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
import pyclipsync  # noqa: E402 - needs REPO_ROOT on sys.path first

DAEMON_CMD = (
    [os.environ["PYCLIPSYNC"]]
    if "PYCLIPSYNC" in os.environ
    else [sys.executable, str(REPO_ROOT / "pyclipsync.py")]
)

SYNC_TIMEOUT = 10.0
POLL_INTERVAL = 0.25


class PyclipsyncTest(unittest.TestCase):
    """Base for tests that drive the module directly (no live session)."""

    pc = pyclipsync


def _missing() -> str:
    if not os.environ.get("DISPLAY"):
        return "DISPLAY not set (need a live X11 session)"
    if not os.environ.get("WAYLAND_DISPLAY"):
        return "WAYLAND_DISPLAY not set (need a live Wayland session)"
    for tool in ("wl-copy", "wl-paste", "xclip", "clipnotify"):
        if shutil.which(tool) is None:
            return f"'{tool}' not found in PATH"
    return ""


def _find_daemon_child(pid: int, needle: str) -> int | None:
    """Return the pid of a direct child of `pid` whose cmdline contains needle.

    Threads share the process, but /proc lists children per task, so scan every
    task's `children` file. Used to reach into the daemon (e.g. to wedge its
    wl-paste watcher) without adding a control interface to the daemon.
    """
    for task in Path(f"/proc/{pid}/task").iterdir():
        try:
            children = (task / "children").read_text().split()
        except OSError:
            continue
        for child in children:
            try:
                cmdline = Path(f"/proc/{child}/cmdline").read_bytes()
            except OSError:
                continue
            if needle.encode() in cmdline:
                return int(child)
    return None


@unittest.skipIf(_missing(), f"skipped: {_missing() or 'no live session'}")
class LiveSessionTest(unittest.TestCase):
    """Shared harness: a daemon under test plus clipboard plumbing.

    The live-session tests share the two clipboards, so they run one at a time.
    SyncTest starts a single daemon for the class; SyncFallbackTest starts one
    per test with its own environment.
    """

    _failed = False
    proc = None

    @classmethod
    def _start_daemon(cls, env=None, settle: float = 2.0) -> None:
        cls.workdir_path = Path(tempfile.mkdtemp(prefix="pyclipsync-test."))
        cls.log_path = cls.workdir_path / "daemon.log"
        cls.log_file = open(cls.log_path, "wb")
        cls.proc = subprocess.Popen(
            DAEMON_CMD,
            stdout=cls.log_file,
            stderr=subprocess.STDOUT,
            env=dict(os.environ, **(env or {})),
            start_new_session=True,
        )
        cls._wait_started(timeout=15.0)
        time.sleep(settle)

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
    def _stop_daemon(cls) -> None:
        if cls.proc is None:
            return
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
        cls.proc = None

    def run(self, result):
        super().run(result)
        if result.failures or result.errors:
            type(self)._failed = True

    def daemon_log(self) -> str:
        return self.log_path.read_text(errors="replace")

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


class SyncTest(LiveSessionTest):
    """Sequential sync tests against one daemon instance.

    Tests share the two clipboards, so order matters: method names are
    numbered (unittest runs them in sorted name order).
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls._start_daemon()

    @classmethod
    def tearDownClass(cls) -> None:
        cls._stop_daemon()

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


class SyncFallbackTest(LiveSessionTest):
    """Paths that do not go through a watcher.

    SyncTest always changes the clipboard while the daemon is already running,
    so it only exercises the watchers. These tests cover the two remaining
    paths: the initial read at startup, and the backstop poll that recovers an
    event a watcher missed. Each test owns its daemon and its environment.
    """

    def tearDown(self) -> None:
        type(self)._stop_daemon()

    def test_startup_syncs_an_existing_clipboard(self):
        # No daemon yet: the clipboard already holds a value, so only the
        # startup read can carry it to the other side.
        data = f"tc-startup-{secrets.token_hex(4)}".encode()
        self.write_w("text/plain", data)
        type(self)._start_daemon(settle=0.5)
        self.wait_value(lambda: self.read_x("UTF8_STRING"), self.wl_text(data))

    def test_backstop_recovers_a_missed_w_change(self):
        type(self)._start_daemon(env={"IDLE_POLL_SECONDS": "2"}, settle=0.5)
        watcher = _find_daemon_child(self.proc.pid, "--watch")
        self.assertIsNotNone(watcher, "wl-paste watcher child not found")
        os.kill(watcher, signal.SIGSTOP)  # wedge the watcher: no more events
        try:
            data = f"tc-backstop-{secrets.token_hex(4)}".encode()
            self.write_w("text/plain", data)
            # The wedged watcher cannot deliver this change; only the backstop
            # poll can, so it must still land within its (shortened) interval.
            self.wait_value(lambda: self.read_x("UTF8_STRING"), self.wl_text(data))
        finally:
            try:
                os.kill(watcher, signal.SIGCONT)
            except ProcessLookupError:
                pass

    def test_idle_does_not_read_the_clipboard(self):
        # DEBUG logs one line per state read. With the default 60s backstop an
        # idle daemon must not read at all in a window well under that, which a
        # regression to a fast poll would immediately break.
        type(self)._start_daemon(env={"DEBUG": "1"}, settle=0.5)
        data = f"tc-idle-{secrets.token_hex(4)}".encode()
        self.write_w("text/plain", data)
        self.wait_value(lambda: self.read_x("UTF8_STRING"), self.wl_text(data))
        time.sleep(0.5)  # let the event-driven sync settle
        before = self.daemon_log().count("read W")
        time.sleep(6.0)
        self.assertEqual(
            self.daemon_log().count("read W"),
            before,
            "daemon read the clipboard while idle",
        )


class UnreadableOfferTest(PyclipsyncTest):
    """Unit tests for the unreadable-offer warning (no live session needed)."""

    def test_supported_unreadable_offer_is_logged(self):
        with self.assertLogs("pyclipsync", level="WARNING") as cm:
            self.pc._warn_unreadable(
                self.pc.W_LABEL,
                {"image/png", "text/plain"},
                self.pc.W_SUPPORTED,
            )
        self.assertIn("image/png", cm.output[0])

    def test_unknown_offers_are_ignored(self):
        with self.assertNoLogs("pyclipsync", level="WARNING"):
            self.pc._warn_unreadable(
                self.pc.W_LABEL, {"application/x-foo"}, self.pc.W_SUPPORTED
            )


class ReadRetryTest(PyclipsyncTest):
    """A supported offer that reads empty is retried before giving up."""

    def priority(self):
        return (self.pc._Priority("text", ("UTF8_STRING",)),)

    def supported(self):
        return {"UTF8_STRING"}

    def test_retries_then_succeeds(self):
        seen = []

        def read(mime):
            seen.append(mime)
            return None if len(seen) < 2 else b"hello"

        with patch.object(self.pc.time, "sleep"):
            _, state = self.pc._read_state(
                lambda: {"UTF8_STRING"}, read, self.priority(), self.supported()
            )
        self.assertIsNotNone(state)
        self.assertEqual(state.data, b"hello")

    def test_gives_up_after_the_retry_budget(self):
        seen = []

        def read(mime):
            seen.append(mime)
            return None

        with patch.object(self.pc.time, "sleep") as sleep:
            _, state = self.pc._read_state(
                lambda: {"UTF8_STRING"}, read, self.priority(), self.supported()
            )
        self.assertIsNone(state)
        self.assertEqual(len(seen), 1 + self.pc.READ_RETRIES)
        # Backs off exponentially between attempts.
        self.assertEqual(
            [call.args[0] for call in sleep.call_args_list],
            [
                self.pc.READ_RETRY_DELAY_SECONDS * 2**i
                for i in range(self.pc.READ_RETRIES)
            ],
        )

    def test_no_retry_when_nothing_relevant_is_offered(self):
        seen = []

        def read(mime):
            seen.append(mime)
            return None

        for offered in (set(), {"TARGETS"}, {"application/x-foo"}):
            with patch.object(self.pc.time, "sleep") as sleep:
                _, state = self.pc._read_state(
                    lambda o=offered: o, read, self.priority(), self.supported()
                )
            self.assertIsNone(state)
            self.assertEqual(seen, [])
            sleep.assert_not_called()


class WatchRecycleTest(PyclipsyncTest):
    """Unit tests for the watcher recycle helper (no live session needed)."""

    def test_recycles_a_hung_command(self):
        events = []
        start = time.monotonic()
        # A recycle is a healthy run: it must report success (no backoff).
        self.assertTrue(
            self.pc._watch_once(
                ["sh", "-c", "echo tick; exec sleep 30"], lambda: events.append(1), 0.3
            )
        )
        self.assertEqual(events, [1])
        self.assertLess(time.monotonic() - start, 5.0)

    def test_returns_when_command_exits(self):
        events = []
        start = time.monotonic()
        # The child died on its own: report failure so the caller backs off.
        self.assertFalse(
            self.pc._watch_once(["sh", "-c", "echo once"], lambda: events.append(1), 30)
        )
        self.assertEqual(events, [1])
        self.assertLess(time.monotonic() - start, 5.0)


class WatchLoopTest(PyclipsyncTest):
    """Unit tests for the resilient watcher loop (no live session needed)."""

    class _Stop(BaseException):
        """Raised by fake watchers to break out of the otherwise infinite loop."""

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


class WStateTextTest(PyclipsyncTest):
    """w_state() must read charset-qualified text, not just bare text/plain."""

    def test_reads_each_text_variant(self):
        for mime in self.pc.W_TEXT_TYPES:
            with self.subTest(mime=mime):
                with patch.object(
                    self.pc, "wl_types", return_value={mime}
                ), patch.object(self.pc, "wl_read", return_value=b"hi"):
                    state = self.pc.w_state()
                self.assertIsNotNone(state)
                self.assertEqual(state.kind, "text")


class ImageOverUriPriorityTest(PyclipsyncTest):
    """An offered image must win over a file URI when both are present.

    QQ (and Chromium/Electron generally) put image/png on the clipboard next
    to a file:// URI for a cache or temp file. The URI is not portable: for a
    sandboxed sender it names a path inside its own mount namespace, which a
    sandboxed receiver (e.g. Telegram) resolves to a non-existent, empty
    file. The image bytes paste anywhere, so they take priority.
    """

    def test_x_state_prefers_png_over_gnome_copied_files(self):
        reads = {self.pc.X_PNG: b"\x89PNG-bytes"}
        with patch.object(
            self.pc, "x_targets", return_value={self.pc.X_PNG, self.pc.X_GNOME_FILES}
        ), patch.object(self.pc, "x_read", side_effect=reads.get):
            state = self.pc.x_state()
        self.assertIsNotNone(state)
        self.assertEqual(state.kind, "png")
        self.assertEqual(state.data, b"\x89PNG-bytes")

    def test_x_state_prefers_jpeg_over_uri_list(self):
        reads = {self.pc.X_JPEG: b"jpeg-bytes"}
        with patch.object(
            self.pc, "x_targets", return_value={self.pc.X_JPEG, self.pc.X_URI}
        ), patch.object(self.pc, "x_read", side_effect=reads.get):
            state = self.pc.x_state()
        self.assertIsNotNone(state)
        self.assertEqual(state.kind, "jpeg")

    def test_x_state_falls_back_to_uri_without_an_image(self):
        with patch.object(
            self.pc, "x_targets", return_value={self.pc.X_GNOME_FILES}
        ), patch.object(
            self.pc, "x_read", return_value=b"copy\nfile:///tmp/x.png\n"
        ), patch.object(self.pc.time, "sleep"):
            state = self.pc.x_state()
        self.assertIsNotNone(state)
        self.assertEqual(state.kind, "uri")
        self.assertEqual(state.data, b"file:///tmp/x.png\n")

    def test_x_state_rechecks_and_prefers_a_late_image(self):
        # First read offers only the URI; a later read exposes image/png.
        targets = [{self.pc.X_URI}, {self.pc.X_PNG}]

        def read(mime):
            return b"file:///tmp/x.png\n" if mime == self.pc.X_URI else b"\x89PNG"

        with patch.object(self.pc, "x_targets", side_effect=targets), patch.object(
            self.pc, "x_read", side_effect=read
        ), patch.object(self.pc.time, "sleep"):
            state = self.pc.x_state()
        self.assertEqual(state.kind, "png")
        self.assertEqual(state.data, b"\x89PNG")

    def test_w_state_prefers_png_over_uri(self):
        with patch.object(
            self.pc, "wl_types", return_value={self.pc.W_PNG, self.pc.W_URI}
        ), patch.object(self.pc, "wl_read", return_value=b"img-bytes"):
            state = self.pc.w_state()
        self.assertIsNotNone(state)
        self.assertEqual(state.kind, "png")


class EnvSecondsTest(PyclipsyncTest):
    """Unit tests for the positive-float env parser."""

    def setUp(self) -> None:
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


class NormalizeUriTest(PyclipsyncTest):
    """Unit tests for the uri-list / gnome-copied-files normalizer."""

    def test_drops_copy_and_cut_headers(self):
        self.assertEqual(self.pc.normalize_uri(b"copy\nfile:///a.png\n"), b"file:///a.png\n")
        self.assertEqual(self.pc.normalize_uri(b"cut\nfile:///a.png\n"), b"file:///a.png\n")

    def test_rewrites_bare_absolute_paths(self):
        self.assertEqual(self.pc.normalize_uri(b"/tmp/a.png\n"), b"file:///tmp/a.png\n")

    def test_keeps_multiple_lines(self):
        self.assertEqual(self.pc.normalize_uri(b"/a\n/b\n"), b"file:///a\nfile:///b\n")

    def test_keeps_non_file_urls(self):
        self.assertEqual(self.pc.normalize_uri(b"https://x/y\n"), b"https://x/y\n")

    def test_empty_or_header_only_is_empty(self):
        self.assertEqual(self.pc.normalize_uri(b""), b"")
        self.assertEqual(self.pc.normalize_uri(b"copy\n"), b"")


class MissingToolsTest(PyclipsyncTest):
    """Unit tests for the startup helper check (main() stays thin)."""

    def test_reports_only_the_missing_tools(self):
        def which(tool):
            return None if tool == "xclip" else f"/bin/{tool}"

        with patch.object(self.pc.shutil, "which", side_effect=which):
            self.assertEqual(self.pc._missing_tools(), ["xclip"])

    def test_empty_when_everything_is_present(self):
        with patch.object(self.pc.shutil, "which", return_value="/bin/tool"):
            self.assertEqual(self.pc._missing_tools(), [])


class OwnerLifecycleTest(PyclipsyncTest):
    """Unit tests for spawning, tracking and cleaning up clipboard owners."""

    def setUp(self) -> None:
        self.procs = self.pc._procs
        with self.procs._owner_lock:
            self.procs._owners.clear()

    def tearDown(self) -> None:
        with self.procs._owner_lock:
            self.procs._owners.clear()

    def test_spawn_owner_records_process_group(self):
        self.assertTrue(self.procs.spawn_owner(["sh", "-c", "exit 0"], b"data"))
        with self.procs._owner_lock:
            self.assertEqual(len(self.procs._owners), 1)

    def test_spawn_owner_timeout_is_not_recorded(self):
        with patch.object(self.pc, "CLIPBOARD_TIMEOUT_SECONDS", 0.2), patch.object(
            self.pc.log, "warning"
        ):
            self.assertFalse(
                self.procs.spawn_owner(["sh", "-c", "exec sleep 30"], b"data")
            )
        with self.procs._owner_lock:
            self.assertEqual(self.procs._owners, set())

    def test_group_alive(self):
        with patch.object(self.pc.os, "killpg", side_effect=ProcessLookupError):
            self.assertFalse(self.procs._group_alive(1))
        with patch.object(self.pc.os, "killpg", side_effect=PermissionError):
            self.assertTrue(self.procs._group_alive(1))
        with patch.object(self.pc.os, "killpg"):
            self.assertTrue(self.procs._group_alive(1))

    def test_prune_owners_drops_dead_groups(self):
        with self.procs._owner_lock:
            self.procs._owners.update({111, 222})
        with patch.object(
            self.procs, "_group_alive", side_effect=lambda pgid: pgid == 222
        ):
            self.procs._prune_owners()
        with self.procs._owner_lock:
            self.assertEqual(self.procs._owners, {222})

    def test_cleanup_kills_live_groups(self):
        with self.procs._owner_lock:
            self.procs._owners.update({111111, 222222})
        killed = []
        with patch.object(self.procs, "_group_alive", return_value=True), patch.object(
            self.procs, "_kill_group", side_effect=lambda pgid, _sig: killed.append(pgid)
        ):
            self.procs.cleanup_owners()
        self.assertEqual(sorted(killed), [111111, 222222])
        with self.procs._owner_lock:
            self.assertEqual(self.procs._owners, set())

    def test_cleanup_skips_dead_groups(self):
        with self.procs._owner_lock:
            self.procs._owners.update({111111, 222222})
        killed = []
        with patch.object(
            self.procs, "_group_alive", side_effect=lambda pgid: pgid == 222222
        ), patch.object(
            self.procs, "_kill_group", side_effect=lambda pgid, _sig: killed.append(pgid)
        ):
            self.procs.cleanup_owners()
        self.assertEqual(killed, [222222])


class WatcherLifecycleTest(PyclipsyncTest):
    """Unit tests for tracking and terminating watcher children."""

    def setUp(self) -> None:
        self.procs = self.pc._procs
        with self.procs._watcher_lock:
            self.procs._watchers.clear()

    def tearDown(self) -> None:
        with self.procs._watcher_lock:
            self.procs._watchers.clear()

    def test_cleanup_terminates_and_clears(self):
        class FakeProc:
            def __init__(self):
                self.terminated = False

            def terminate(self):
                self.terminated = True

            def wait(self, timeout=None):
                return 0

        a, b = FakeProc(), FakeProc()
        with self.procs._watcher_lock:
            self.procs._watchers.update({a, b})
        self.procs.cleanup_watchers()
        self.assertTrue(a.terminated)
        self.assertTrue(b.terminated)
        with self.procs._watcher_lock:
            self.assertEqual(self.procs._watchers, set())


class SyncerTest(PyclipsyncTest):
    """Unit tests for the dedup / loop-prevention state machine."""

    def setUp(self) -> None:
        self.syncer = pyclipsync.Syncer()

    def state(self, data: bytes, kind: str = "text"):
        return self.pc.State.of(kind, data)

    def test_w2x_pushes_and_records_measured_destination(self):
        src = self.state(b"hi")
        measured = self.state(b"hi\n")
        with patch.object(self.pc, "w_state", return_value=src), patch.object(
            self.pc, "x_state", return_value=measured
        ), patch.object(self.pc, "push_w_to_x", return_value=True) as push:
            self.syncer.on_w_change()
        push.assert_called_once_with(src)
        self.assertEqual(self.syncer.last_w, src)
        self.assertEqual(self.syncer.last_x, measured)

    def test_w2x_dedup_skips_unchanged(self):
        src = self.state(b"hi")
        self.syncer.last_w = src
        with patch.object(self.pc, "w_state", return_value=src), patch.object(
            self.pc, "push_w_to_x", return_value=True
        ) as push:
            self.syncer.on_w_change()
        push.assert_not_called()

    def test_w2x_skips_when_already_on_x(self):
        src = self.state(b"hi")
        self.syncer.last_x = src
        with patch.object(self.pc, "w_state", return_value=src), patch.object(
            self.pc, "push_w_to_x", return_value=True
        ) as push:
            self.syncer.on_w_change()
        push.assert_not_called()

    def test_w2x_empty_is_ignored(self):
        with patch.object(self.pc, "w_state", return_value=None), patch.object(
            self.pc, "push_w_to_x"
        ) as push:
            self.syncer.on_w_change()
        push.assert_not_called()
        self.assertIsNone(self.syncer.last_w)
        self.assertIsNone(self.syncer.last_x)

    def test_w2x_failed_push_is_not_recorded(self):
        src = self.state(b"hi")
        with patch.object(self.pc, "w_state", return_value=src), patch.object(
            self.pc, "push_w_to_x", return_value=False
        ) as push:
            self.syncer.on_w_change()
        push.assert_called_once_with(src)
        self.assertIsNone(self.syncer.last_w)
        self.assertIsNone(self.syncer.last_x)

    def test_x2w_pushes_and_records_measured_destination(self):
        src = self.state(b"hello")
        measured = self.state(b"hello\n")
        with patch.object(self.pc, "x_state", return_value=src), patch.object(
            self.pc, "w_state", return_value=measured
        ), patch.object(self.pc, "push_x_to_w", return_value=True) as push:
            self.syncer.on_x_change()
        push.assert_called_once_with(src)
        self.assertEqual(self.syncer.last_x, src)
        self.assertEqual(self.syncer.last_w, measured)

    def test_x2w_dedup_skips_unchanged(self):
        src = self.state(b"hello")
        self.syncer.last_x = src
        with patch.object(self.pc, "x_state", return_value=src), patch.object(
            self.pc, "push_x_to_w", return_value=True
        ) as push:
            self.syncer.on_x_change()
        push.assert_not_called()

    def test_x2w_binary_skips_destination_readback(self):
        """Binary mimes survive wl-copy byte-exact, so no readback is needed."""
        src = self.state(b"\x89PNG-bytes", "png")
        with patch.object(self.pc, "x_state", return_value=src), patch.object(
            self.pc, "push_x_to_w", return_value=True
        ), patch.object(self.pc, "w_state") as readback:
            self.syncer.on_x_change()
        readback.assert_not_called()
        self.assertEqual(self.syncer.last_x, src)
        self.assertEqual(self.syncer.last_w, src)

    def test_w2x_binary_skips_destination_readback(self):
        src = self.state(b"\x89PNG-bytes", "png")
        with patch.object(self.pc, "w_state", return_value=src), patch.object(
            self.pc, "push_w_to_x", return_value=True
        ), patch.object(self.pc, "x_state") as readback:
            self.syncer.on_w_change()
        readback.assert_not_called()
        self.assertEqual(self.syncer.last_w, src)
        self.assertEqual(self.syncer.last_x, src)


class PollBackstopTest(PyclipsyncTest):
    """The backstop poll reads both sides, starting immediately."""

    def test_reads_both_sides(self):
        calls = []

        class FakeSyncer:
            def on_x_change(self):
                calls.append("x")

            def on_w_change(self):
                calls.append("w")

        threading.Thread(
            target=self.pc.watch_poll, args=(FakeSyncer(), 0.05), daemon=True
        ).start()
        time.sleep(0.2)
        self.assertIn("x", calls)
        self.assertIn("w", calls)


if __name__ == "__main__":
    unittest.main(verbosity=2)
