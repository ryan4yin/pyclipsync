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


if __name__ == "__main__":
    unittest.main(verbosity=2)
