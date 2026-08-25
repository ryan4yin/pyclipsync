#!/usr/bin/env python3
"""pyclipsync: Wayland <-> X11 clipboard synchronization daemon.

For Wayland compositors that run X11 apps through xwayland-satellite
(niri, Hyprland, ...), where neither the compositor nor the satellite
bridges the X11 CLIPBOARD selection with the Wayland data device.

The script is a thin orchestrator over battle-tested CLI tools
(same primitives as the bash tool `clipsync` by 123hi123,
https://github.com/123hi123/clipsync):

  X11 -> Wayland:     clipnotify exits      -> xclip reads  -> wl-copy
                      (relaunched in a loop, each exit = one selection event)
  Wayland -> X11:     wl-paste --watch fires -> wl-paste reads -> xclip sets
                      (xclip becomes the CLIPBOARD selection owner)

A per-side "last synced" state machine prevents X -> W -> X loops.

Content types, highest priority wins:
  - text/uri-list   (WeChat/QQ images arrive as uri-list + application/x-qt-image)
  - image/png
  - text            (UTF8_STRING / text/plain)

Dependencies: python3 (stdlib only), xclip, clipnotify, wl-clipboard.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import signal
import subprocess
import sys
import threading
import time

log = logging.getLogger("pyclipsync")

# X11 atoms
X_URI = "text/uri-list"
X_PNG = "image/png"
X_UTF8 = "UTF8_STRING"
X_STRING = "STRING"
X_PLAIN = "text/plain"
# targets that mark an uri-list as an image/file copy (WeChat, Qt, GNOME)
X_URI_MARKERS = {"application/x-qt-image", "x-special/gnome-copied-files"}

# Wayland mime types
W_TEXT = "text/plain"
W_PNG = "image/png"
W_URI = "text/uri-list"
W_WATCH_TYPES = [W_TEXT, W_PNG, W_URI]


def run(cmd: list[str], data: bytes | None = None, timeout: float = 5.0):
    """Run a command. Returns (returncode, stdout). Never raises."""
    try:
        r = subprocess.run(cmd, input=data, capture_output=True, timeout=timeout)
        return r.returncode, r.stdout
    except (subprocess.SubprocessError, OSError) as e:
        log.debug("%s failed: %s", cmd[0], e)
        return None, b""


def wl_types() -> set[str]:
    rc, out = run(["wl-paste", "--list-types"])
    if rc != 0:
        return set()
    return set(out.decode("utf-8", "replace").split())


def wl_read(mime: str) -> bytes | None:
    rc, out = run(["wl-paste", "--type", mime])
    if rc == 0 and out:
        return out
    return None


def wl_copy(mime: str, data: bytes) -> bool:
    rc, _ = run(["wl-copy", "--type", mime], data=data)
    return rc == 0


def x_targets() -> set[str]:
    rc, out = run(["xclip", "-selection", "clipboard", "-t", "TARGETS", "-o"])
    if rc != 0:
        return set()
    return set(out.decode("utf-8", "replace").replace("\x00", " ").split())


def x_read(target: str) -> bytes | None:
    rc, out = run(["xclip", "-selection", "clipboard", "-t", target, "-o"])
    if rc == 0 and out:
        return out
    return None


def x_set(target: str, data: bytes) -> bool:
    """Write the X CLIPBOARD.

    xclip forks a background child to hold the selection and serve
    SelectionRequests. That child inherits stdout/stderr, so we must NOT use
    capture_output (pipes) here: subprocess.run would block on those pipes
    until the owner child exits (or the timeout fires). Pointing them at
    /dev/null makes the call return as soon as the parent forks.
    """
    try:
        r = subprocess.run(
            ["xclip", "-selection", "clipboard", "-t", target],
            input=data,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5.0,
        )
        return r.returncode == 0
    except (subprocess.SubprocessError, OSError) as e:
        log.debug("xclip set %s failed: %s", target, e)
        return False


def normalize_uri(data: bytes) -> bytes:
    """Normalize an uri-list: drop the gnome 'copy' header, ensure file:// URIs."""
    lines = [
        line
        for line in data.decode("utf-8", "replace").splitlines()
        if line.strip() and line.strip() != "copy"
    ]
    out = []
    for line in lines:
        line = line.strip()
        if line.startswith("file://"):
            out.append(line)
        elif line.startswith("/"):
            out.append("file://" + line)
        else:
            out.append(line)
    return "\n".join(out).encode("utf-8") + b"\n" if out else b""


def h(data: bytes | None) -> str:
    return hashlib.sha256(data or b"").hexdigest()


def x_state():
    """Read the X11 CLIPBOARD. Returns (kind, data, digest) or None."""
    targets = x_targets()
    if not targets or targets == {"TARGETS"}:
        return None
    if X_URI in targets and targets & X_URI_MARKERS:
        data = x_read(X_URI)
        if data:
            data = normalize_uri(data)
            return ("uri", data, h(data))
    if X_PNG in targets:
        data = x_read(X_PNG)
        if data:
            return ("png", data, h(data))
    for target in (X_UTF8, X_PLAIN, X_STRING):
        if target in targets:
            data = x_read(target)
            if data:
                return ("text", data, h(data))
    return None


def w_state():
    """Read the Wayland clipboard. Returns (kind, data, digest) or None."""
    types = wl_types()
    if not types:
        return None
    if W_URI in types:
        data = wl_read(W_URI)
        if data:
            data = normalize_uri(data)
            return ("uri", data, h(data))
    if W_PNG in types:
        data = wl_read(W_PNG)
        if data:
            return ("png", data, h(data))
    if W_TEXT in types:
        data = wl_read(W_TEXT)
        if data:
            return ("text", data, h(data))
    return None


def push_x_to_w(state):
    kind, data, _ = state
    if kind == "text" and not wl_copy(W_TEXT, data):
        log.warning("wl-copy text/plain failed")
    elif kind == "png" and not wl_copy(W_PNG, data):
        log.warning("wl-copy image/png failed")
    elif kind == "uri" and not wl_copy(W_URI, data):
        log.warning("wl-copy text/uri-list failed")
    else:
        log.info("X -> W: synced %s (%d bytes)", kind, len(data))


def push_w_to_x(state):
    kind, data, _ = state
    if kind == "text" and not x_set(X_UTF8, data):
        log.warning("xclip UTF8_STRING failed")
    elif kind == "png" and not x_set(X_PNG, data):
        log.warning("xclip image/png failed")
    elif kind == "uri" and not x_set(X_URI, data):
        log.warning("xclip text/uri-list failed")
    else:
        log.info("W -> X: synced %s (%d bytes)", kind, len(data))


class Syncer:
    def __init__(self):
        self.lock = threading.Lock()
        # last state known to be present on each side (digest form)
        self.last_x = None
        self.last_w = None

    def on_x_change(self):
        # The state read, the dedup decision and the push must be one atomic
        # step: reading the state outside the lock lets a thread capture a
        # stale clipboard, then pass a stale dedup check and push old data,
        # which is what causes X -> W -> X echo ping-pong under rapid copies.
        with self.lock:
            state = x_state()
            if state is None:
                # empty or unreadable: keep the Wayland side as-is
                return
            if state[2] == (self.last_x or ("", b"", ""))[2]:
                return
            self.last_x = state
            if state[2] == (self.last_w or ("", b"", ""))[2]:
                log.debug("X -> W: already in sync, skipping")
                return
            self.last_w = state
            log.info("X clipboard: %s", state[0])
            push_x_to_w(state)

    def on_w_change(self):
        with self.lock:
            state = w_state()
            if state is None:
                return
            if state[2] == (self.last_w or ("", b"", ""))[2]:
                return
            self.last_w = state
            if state[2] == (self.last_x or ("", b"", ""))[2]:
                log.debug("W -> X: already in sync, skipping")
                return
            self.last_x = state
            log.info("Wayland clipboard: %s", state[0])
            push_w_to_x(state)


def watch_clipnotify(syncer: Syncer):
    """X -> W: forward each X11 selection owner change.

    clipnotify (nixpkgs) is a one-shot trigger by design: it blocks until the
    next CLIPBOARD/PRIMARY owner-change event and then exits silently (no
    output, no flags). Its intended usage is a relaunch loop
    (`while clipnotify; do ...; done`), so we do exactly that: every exit
    means "something changed", and we re-read the X state afterwards.
    PRIMARY (click-selection) changes also trigger it, but the digest-based
    dedup in Syncer.on_x_change filters those out.
    """
    clipnotify = shutil.which("clipnotify")
    if clipnotify is None:
        log.error("clipnotify not found in PATH; X -> W sync disabled")
        return
    while True:
        try:
            subprocess.run(
                [clipnotify],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except (subprocess.SubprocessError, OSError) as e:
            log.debug("clipnotify run failed: %s", e)
            time.sleep(0.2)
            continue
        syncer.on_x_change()


def watch_x_poll(syncer: Syncer, interval: float = 1.0):
    """X -> W safety net.

    The clipnotify relaunch loop has a small registration gap (between one
    clipnotify exiting and the next registering its XFixes subscription) in
    which an X11 owner change can be missed. If that change is the last one,
    no further event re-triggers the sync and it stalls. Polling the X state
    periodically guarantees convergence; on_x_change is digest-deduped so the
    extra reads are cheap no-ops when nothing changed.
    """
    while True:
        time.sleep(interval)
        try:
            syncer.on_x_change()
        except Exception:  # noqa: BLE001 - never let the poller die
            log.exception("x poll failed")


def watch_wayland(syncer: Syncer, mime: str):
    """W -> X: forward each Wayland clipboard offer of the given mime type.

    wl-paste --watch takes exactly one command (exec'ed as-is, no shell):
    it runs it with the offer data on stdin whenever a new offer containing
    the given type appears. We use bare `echo`, which ignores stdin and
    prints a single newline per offer -- a pure change signal. We re-read
    the full state afterwards.
    """
    with subprocess.Popen(
        ["wl-paste", "--type", mime, "--watch", "echo"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    ) as p:
        assert p.stdout is not None
        for _ in p.stdout:
            syncer.on_w_change()


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    if os.environ.get("DEBUG"):
        logging.basicConfig(
            level=logging.DEBUG,
            format="%(asctime)s %(levelname)s %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

    if shutil.which("wl-copy") is None or shutil.which("wl-paste") is None:
        log.error("wl-clipboard (wl-copy/wl-paste) not found in PATH")
        sys.exit(1)

    syncer = Syncer()
    threading.Thread(
        target=watch_clipnotify, args=(syncer,), daemon=True, name="x2w"
    ).start()
    threading.Thread(
        target=watch_x_poll, args=(syncer,), daemon=True, name="x2w-poll"
    ).start()
    for mime in W_WATCH_TYPES:
        threading.Thread(
            target=watch_wayland, args=(syncer, mime), daemon=True, name=f"w2x-{mime}"
        ).start()

    log.info(
        "pyclipsync started (DISPLAY=%s, WAYLAND_DISPLAY=%s)",
        os.environ.get("DISPLAY"),
        os.environ.get("WAYLAND_DISPLAY"),
    )
    threading.Event().wait()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
