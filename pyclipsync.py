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

Content types, highest priority wins (matching linuxqq-clipsync for the
WeChat/QQ cases):
  - uri   x-special/gnome-copied-files (GNOME file copy, QQ stickers) or
          text/uri-list (WeChat images)  -> normalized to file:// URIs,
          carried as text/uri-list on both sides
  - png   image/png
  - jpeg  image/jpeg
  - html  text/html   (QQ rich text)
  - text  UTF8_STRING / text/plain / STRING

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
X_GNOME_FILES = "x-special/gnome-copied-files"
X_PNG = "image/png"
X_JPEG = "image/jpeg"
X_HTML = "text/html"
X_UTF8 = "UTF8_STRING"
X_STRING = "STRING"
X_PLAIN = "text/plain"

# Wayland mime types
W_TEXT = "text/plain"
W_PNG = "image/png"
W_JPEG = "image/jpeg"
W_HTML = "text/html"
W_URI = "text/uri-list"
# one wl-paste --watch thread per offered mime type
W_WATCH_TYPES = [W_TEXT, W_PNG, W_JPEG, W_HTML, W_URI]

# kind -> target/mime per direction (uri-list always maps to text/uri-list on
# both sides; that is what WeChat and QQ read for pasted file/image links)
W_TARGETS = {
    "text": W_TEXT,
    "uri": W_URI,
    "png": W_PNG,
    "jpeg": W_JPEG,
    "html": W_HTML,
}
X_TARGETS = {
    "text": X_UTF8,
    "uri": X_URI,
    "png": X_PNG,
    "jpeg": X_JPEG,
    "html": X_HTML,
}


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
    """Write the Wayland clipboard.

    wl-copy (like xclip) keeps a background child alive to serve the data;
    that child inherits stdout/stderr, so capture_output would block until
    timeout even though the data was already delivered. Point them at
    /dev/null so the call returns as soon as the parent exits.

    wl-copy also appends exactly one trailing newline to piped text/* input.
    To keep the Wayland clipboard clean (a single trailing newline, no empty
    final line), strip one trailing newline first for text mimes: the net
    effect is that text always lands on W with exactly one trailing newline.
    """
    if mime.startswith("text/") and data.endswith(b"\n"):
        data = data[:-1]
    try:
        r = subprocess.run(
            ["wl-copy", "--type", mime],
            input=data,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5.0,
        )
        return r.returncode == 0
    except (subprocess.SubprocessError, OSError) as e:
        log.debug("wl-copy %s failed: %s", mime, e)
        return False


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
    """Normalize an uri-list / gnome-copied-files payload.

    Drops the leading 'copy'/'cut' action header that
    x-special/gnome-copied-files carries, and rewrites bare absolute paths to
    file:// URIs. Returns b"" when nothing usable remains.
    """
    lines = [
        line
        for line in data.decode("utf-8", "replace").splitlines()
        if line.strip() and line.strip() not in ("copy", "cut")
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
    """Read the X11 CLIPBOARD. Returns (kind, data, digest) or None.

    Priority (highest first), matching linuxqq-clipsync:
      uri  x-special/gnome-copied-files  (GNOME file copy, QQ stickers)
      uri  text/uri-list                 (WeChat images, generic file list)
      png  image/png
      jpeg image/jpeg
      html text/html                     (QQ rich text)
      text UTF8_STRING / text/plain / STRING
    """
    targets = x_targets()
    if not targets or targets == {"TARGETS"}:
        return None
    if X_GNOME_FILES in targets:
        data = normalize_uri(x_read(X_GNOME_FILES) or b"")
        if data:
            return ("uri", data, h(data))
    if X_URI in targets:
        data = normalize_uri(x_read(X_URI) or b"")
        if data:
            return ("uri", data, h(data))
    if X_PNG in targets:
        data = x_read(X_PNG)
        if data:
            return ("png", data, h(data))
    if X_JPEG in targets:
        data = x_read(X_JPEG)
        if data:
            return ("jpeg", data, h(data))
    if X_HTML in targets:
        data = x_read(X_HTML)
        if data:
            return ("html", data, h(data))
    for target in (X_UTF8, X_PLAIN, X_STRING):
        if target in targets:
            data = x_read(target)
            if data:
                return ("text", data, h(data))
    return None


def w_state():
    """Read the Wayland clipboard. Returns (kind, data, digest) or None.

    Priority: uri-list > png > jpeg > html > text.
    """
    types = wl_types()
    if not types:
        return None
    if W_URI in types:
        data = normalize_uri(wl_read(W_URI) or b"")
        if data:
            return ("uri", data, h(data))
    if W_PNG in types:
        data = wl_read(W_PNG)
        if data:
            return ("png", data, h(data))
    if W_JPEG in types:
        data = wl_read(W_JPEG)
        if data:
            return ("jpeg", data, h(data))
    if W_HTML in types:
        data = wl_read(W_HTML)
        if data:
            return ("html", data, h(data))
    if W_TEXT in types:
        data = wl_read(W_TEXT)
        if data:
            return ("text", data, h(data))
    return None


def push_x_to_w(state) -> bool:
    kind, data, _ = state
    mime = W_TARGETS.get(kind)
    if mime is None:
        log.warning("X -> W: unknown kind %s", kind)
        return False
    if not wl_copy(mime, data):
        log.warning("wl-copy %s failed", mime)
        return False
    log.info("X -> W: synced %s (%d bytes)", kind, len(data))
    return True


def push_w_to_x(state) -> bool:
    kind, data, _ = state
    target = X_TARGETS.get(kind)
    if target is None:
        log.warning("W -> X: unknown kind %s", kind)
        return False
    if not x_set(target, data):
        log.warning("xclip %s failed", target)
        return False
    log.info("W -> X: synced %s (%d bytes)", kind, len(data))
    return True


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
        # Bookkeeping (last_*) is only updated once the push succeeds, so a
        # failed push is retried on the next event/poll instead of stalling.
        # The destination side is recorded from a readback, not from the
        # pushed payload: wl-copy does not preserve text byte-exactly (it
        # appends a trailing newline to piped input), so the measured state
        # is the only source of truth for future dedup decisions.
        with self.lock:
            state = x_state()
            if state is None:
                # empty or unreadable: keep the Wayland side as-is
                return
            if state[2] == (self.last_x or ("", b"", ""))[2]:
                return
            if state[2] == (self.last_w or ("", b"", ""))[2]:
                log.debug("X -> W: already in sync, skipping")
                return
            log.info("X clipboard: %s", state[0])
            if push_x_to_w(state):
                self.last_x = state
                self.last_w = w_state() or state

    def on_w_change(self):
        with self.lock:
            state = w_state()
            if state is None:
                return
            if state[2] == (self.last_w or ("", b"", ""))[2]:
                return
            if state[2] == (self.last_x or ("", b"", ""))[2]:
                log.debug("W -> X: already in sync, skipping")
                return
            log.info("Wayland clipboard: %s", state[0])
            if push_w_to_x(state):
                self.last_w = state
                self.last_x = x_state() or state


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


def watch_w_poll(syncer: Syncer, interval: float = 1.0):
    """W -> X safety net, symmetric to watch_x_poll.

    wl-paste --watch only fires on *new* offers; if a W -> X push fails
    (e.g. xclip transiently unavailable) there is no further W event to
    trigger a retry, so the sync would stall until the user copies again.
    Polling the Wayland state guarantees convergence; on_w_change is
    digest-deduped so the extra reads are cheap no-ops when nothing changed.
    """
    while True:
        time.sleep(interval)
        try:
            syncer.on_w_change()
        except Exception:  # noqa: BLE001 - never let the poller die
            log.exception("w poll failed")


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
    threading.Thread(
        target=watch_w_poll, args=(syncer,), daemon=True, name="w2x-poll"
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
