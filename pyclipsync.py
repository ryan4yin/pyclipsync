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


def _env_seconds(name: str, default: float) -> float:
    """Read a positive float from the environment, falling back to `default`."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        log.warning("%s=%r is not a number, using %s", name, raw, default)
        return default
    if value <= 0:
        log.warning("%s must be > 0, using %s", name, default)
        return default
    return value


# X11 atoms
X_URI = "text/uri-list"
X_GNOME_FILES = "x-special/gnome-copied-files"
X_PNG = "image/png"
X_JPEG = "image/jpeg"
X_HTML = "text/html"
X_UTF8 = "UTF8_STRING"
X_STRING = "STRING"
X_PLAIN = "text/plain"

# Wayland mime types. Text is offered as either text/plain or the
# charset-qualified form (Qt, GTK and Chromium all use the latter), so read both
# and prefer UTF-8. UTF8_STRING/TEXT/STRING are X11 atom names, not Wayland
# types; GTK/Qt translate them to text/plain;charset=utf-8 on this side.
W_TEXT = "text/plain"
W_TEXT_UTF8 = "text/plain;charset=utf-8"
W_PNG = "image/png"
W_JPEG = "image/jpeg"
W_HTML = "text/html"
W_URI = "text/uri-list"
W_TEXT_TYPES = (W_TEXT_UTF8, W_TEXT)
# one wl-paste --watch thread per offered mime type
W_WATCH_TYPES = [*W_TEXT_TYPES, W_PNG, W_JPEG, W_HTML, W_URI]

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

# Types this tool knows how to read. Used only by the diagnostic below, so an
# unrelated MIME (application/*, primary selection, ...) does not warn.
W_SUPPORTED = set(W_TEXT_TYPES) | {W_PNG, W_JPEG, W_HTML, W_URI}
X_SUPPORTED = {X_UTF8, X_STRING, X_PLAIN, X_PNG, X_JPEG, X_HTML, X_URI, X_GNOME_FILES}

# Recycle each watcher child every N seconds. A helper can wedge (stay alive but
# stop delivering events), which would otherwise stall sync until the whole
# service is restarted; bounding its lifetime makes it self-heal. Env-overridable
# for tuning and tests.
WATCH_RECYCLE_SECONDS = _env_seconds("WATCH_RECYCLE_SECONDS", 3600.0)

# Timeout for a single clipboard helper call. The syncer lock is held across
# these calls, so a hung helper stalls both directions; keep it short so the
# stall is bounded (a read that times out also trips the unreadable-offer log).
CLIPBOARD_TIMEOUT = _env_seconds("CLIPBOARD_TIMEOUT", 3.0)

# Poller interval. Watchers are event-driven; the pollers are only a safety net
# for missed events, so this can be coarse, which keeps idle CPU low.
POLL_INTERVAL_SECONDS = _env_seconds("POLL_INTERVAL_SECONDS", 5.0)

# Watcher retry policy: a watcher loop must survive helper failures without
# dying (silent stall) or hot-looping (a fast respawn storm). Each failure waits
# `delay`, which doubles up to WATCH_BACKOFF_MAX; a run that lasted at least
# WATCH_BACKOFF_MAX (e.g. a clean recycle) resets the backoff.
WATCH_BACKOFF_MIN = 0.2
WATCH_BACKOFF_MAX = 30.0

# Clipboard owners we spawned. wl-copy/xclip fork a child that keeps serving the
# selection; that child outlives us, so track its process group and kill it on
# shutdown instead of leaving a stale owner behind after a restart.
_owner_pgids: set[int] = set()
_owner_lock = threading.Lock()

# Watcher children (wl-paste --watch, clipnotify). They are also reparented and
# would keep running if we exit, so terminate them on shutdown too. (Under
# systemd the cgroup kill covers this, but not when run by hand.)
_watcher_procs: set[subprocess.Popen] = set()
_watcher_lock = threading.Lock()


def run(cmd: list[str], data: bytes | None = None, timeout: float = CLIPBOARD_TIMEOUT):
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


def _kill_group(pgid: int, sig: int) -> None:
    """Signal a process group we created, ignoring an already-gone group."""
    try:
        os.killpg(pgid, sig)
    except OSError:
        pass


def _register_watcher(p: subprocess.Popen) -> None:
    with _watcher_lock:
        _watcher_procs.add(p)


def _unregister_watcher(p: subprocess.Popen) -> None:
    with _watcher_lock:
        _watcher_procs.discard(p)


def _cleanup_watchers() -> None:
    """Terminate the watcher children we spawned (best effort)."""
    with _watcher_lock:
        procs = list(_watcher_procs)
        _watcher_procs.clear()
    for p in procs:
        try:
            p.terminate()
        except OSError:
            pass


def _spawn_owner(cmd: list[str], data: bytes) -> bool:
    """Run a clipboard-owner command and remember its process group.

    wl-copy and xclip fork a child that keeps serving the selection after the
    parent exits; that child outlives us and would keep owning the clipboard
    with stale data. Running it in its own session lets _cleanup_owners kill the
    whole group on shutdown.
    """
    try:
        p = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as e:
        log.debug("%s failed: %s", cmd[0], e)
        return False
    try:
        p.communicate(input=data, timeout=CLIPBOARD_TIMEOUT)
    except subprocess.TimeoutExpired:
        # Kill the whole group: the direct child may already have forked the
        # owner child that holds the selection.
        _kill_group(p.pid, signal.SIGKILL)
        p.kill()
        p.communicate()
        log.warning("%s did not return within %ss", cmd[0], CLIPBOARD_TIMEOUT)
        return False
    if p.returncode != 0:
        log.debug("%s exited with %s", cmd[0], p.returncode)
        return False
    with _owner_lock:
        _owner_pgids.add(p.pid)
    return True


def _cleanup_owners() -> None:
    """Kill the clipboard-owner process groups we spawned (best effort)."""
    with _owner_lock:
        pgids = list(_owner_pgids)
        _owner_pgids.clear()
    for pgid in pgids:
        _kill_group(pgid, signal.SIGTERM)


def wl_copy(mime: str, data: bytes) -> bool:
    """Write the Wayland clipboard.

    wl-copy forks a background child that holds the selection; it inherits
    stdout/stderr, so those go to /dev/null (see _spawn_owner).

    wl-copy also appends exactly one trailing newline to piped text/* input.
    To keep the Wayland clipboard clean (a single trailing newline, no empty
    final line), strip one trailing newline first for text mimes: the net
    effect is that text always lands on W with exactly one trailing newline.
    """
    if mime.startswith("text/") and data.endswith(b"\n"):
        data = data[:-1]
    return _spawn_owner(["wl-copy", "--type", mime], data)


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

    xclip forks a background child that holds the selection and answers
    SelectionRequests; see _spawn_owner. stdout/stderr go to /dev/null so the
    call returns as soon as the parent forks (using pipes would block until the
    owner child exits).
    """
    return _spawn_owner(["xclip", "-selection", "clipboard", "-t", target], data)


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


# Throttle for the unreadable-offer diagnostic: the 1s pollers call the state
# readers continuously, so log at most once per distinct offer set per window
# instead of flooding the journal.
_MISS_LOG_INTERVAL = 30.0
_miss_log: dict[str, tuple[frozenset[str], float]] = {}


def _log_unreadable(side: str, offered: set[str], supported: set[str]) -> None:
    """Warn (throttled) when a supported type is offered but cannot be read.

    Reading is best-effort: `wl-paste`/`xclip` can return nothing even though
    the selection advertises a type we handle -- e.g. a client that keeps
    selection ownership but refuses to serve a background/data-control reader
    (observed with Chromium on Wayland). Without this the miss is silent: no
    sync happens and no log line says why.
    """
    stuck = offered & supported
    if not stuck:
        return
    now = time.monotonic()
    prev = _miss_log.get(side)
    if prev is not None and prev[0] == frozenset(stuck) and now - prev[1] < _MISS_LOG_INTERVAL:
        return
    _miss_log[side] = (frozenset(stuck), now)
    log.warning(
        "%s: %d supported type(s) offered but unreadable, sync skipped: %s",
        side,
        len(stuck),
        " ".join(sorted(stuck)),
    )


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
    _log_unreadable("X11 clipboard", targets, X_SUPPORTED)
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
    for text_mime in W_TEXT_TYPES:
        if text_mime in types:
            data = wl_read(text_mime)
            if data:
                return ("text", data, h(data))
    _log_unreadable("Wayland clipboard", types, W_SUPPORTED)
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


def _watch_loop(run_once, name: str) -> None:
    """Run run_once() forever, surviving failures with capped backoff.

    A watcher must never die silently (that would stall its direction until a
    service restart) and must not hot-loop when the helper fails immediately.
    """
    delay = WATCH_BACKOFF_MIN
    while True:
        started = time.monotonic()
        try:
            run_once()
        except Exception:  # noqa: BLE001 - a watcher must never die
            log.exception("%s failed", name)
        if time.monotonic() - started >= WATCH_BACKOFF_MAX:
            delay = WATCH_BACKOFF_MIN
        time.sleep(delay)
        delay = min(delay * 2, WATCH_BACKOFF_MAX)


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

    def once() -> None:
        p = subprocess.Popen(
            [clipnotify], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        _register_watcher(p)
        try:
            p.wait(timeout=WATCH_RECYCLE_SECONDS)
        except subprocess.TimeoutExpired:
            # No X selection change for a whole interval: just recycle.
            return
        finally:
            _unregister_watcher(p)
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    p.kill()
                    p.wait(timeout=2)
        syncer.on_x_change()

    _watch_loop(once, "clipnotify")


def watch_x_poll(syncer: Syncer, interval: float = POLL_INTERVAL_SECONDS):
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


def watch_w_poll(syncer: Syncer, interval: float = POLL_INTERVAL_SECONDS):
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


def _watch_once(cmd: list[str], on_event, recycle: float) -> None:
    """Run `cmd`, call on_event() per stdout line, then recycle the child.

    Returns when the command exits on its own or after `recycle` seconds, so
    the caller can restart it. Bounding a watcher's lifetime is what lets the
    daemon recover from a wedged helper without detecting the wedge: a fresh
    child is spawned on every call.
    """
    with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as p:
        _register_watcher(p)
        recycler = threading.Timer(recycle, p.terminate)
        recycler.daemon = True
        recycler.start()
        try:
            assert p.stdout is not None
            for _ in p.stdout:
                on_event()
        finally:
            _unregister_watcher(p)
            recycler.cancel()
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    p.kill()
                    p.wait(timeout=2)


def watch_wayland(syncer: Syncer, mime: str):
    """W -> X: forward each Wayland clipboard offer of the given mime type.

    wl-paste --watch takes exactly one command (exec'ed as-is, no shell):
    it runs it with the offer data on stdin whenever a new offer containing
    the given type appears. We use bare `echo`, which ignores stdin and
    prints a single newline per offer -- a pure change signal. We re-read
    the full state afterwards.

    The watch child is recycled every WATCH_RECYCLE_SECONDS (see _watch_once)
    so a wedged watcher cannot silently stall W -> X sync; an offer missed in
    the brief restart gap is caught by watch_w_poll.
    """
    cmd = ["wl-paste", "--type", mime, "--watch", "echo"]

    def on_event() -> None:
        log.debug("watch %s: new offer", mime)
        syncer.on_w_change()

    _watch_loop(
        lambda: _watch_once(cmd, on_event, WATCH_RECYCLE_SECONDS), f"watch {mime}"
    )


_shutdown = threading.Event()


def _handle_shutdown(signum, _frame) -> None:
    log.info("received signal %s, shutting down", signum)
    _shutdown.set()


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

    signal.signal(signal.SIGTERM, _handle_shutdown)
    signal.signal(signal.SIGINT, _handle_shutdown)

    syncer = Syncer()
    threading.Thread(
        target=watch_clipnotify, args=(syncer,), daemon=True, name="x2w"
    ).start()
    threading.Thread(
        target=watch_x_poll,
        args=(syncer, POLL_INTERVAL_SECONDS),
        daemon=True,
        name="x2w-poll",
    ).start()
    threading.Thread(
        target=watch_w_poll,
        args=(syncer, POLL_INTERVAL_SECONDS),
        daemon=True,
        name="w2x-poll",
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
    _shutdown.wait()
    _cleanup_watchers()
    _cleanup_owners()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
