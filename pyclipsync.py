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
                      (one watcher, any offer; xclip becomes the owner)

A per-side "last synced" state machine prevents X -> W -> X loops. The
watchers are the primary trigger; a slow per-side poll is a backstop for the
rare event they miss (a wedged watcher, a restart gap, a failed push).

Content types, highest priority wins:
  - png   image/png
  - jpeg  image/jpeg
  - uri   x-special/gnome-copied-files (GNOME file copy, QQ stickers) or
          text/uri-list (WeChat images)  -> normalized to file:// URIs,
          carried as text/uri-list on both sides
  - html  text/html   (QQ rich text)
  - text  UTF8_STRING / text/plain / STRING

Image bytes outrank a file URI when a client offers both (QQ and Chromium
put image/png next to a file:// URI for a cache/temp file). The bytes paste
anywhere, while the URI may point somewhere the receiver cannot read --
most notably a path inside the sender's sandbox namespace, which a
sandboxed receiver (e.g. Telegram) resolves to a non-existent, empty file.

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

# Kinds that survive xclip and wl-copy byte-exact (unlike text/*, which wl-copy
# reshapes with a trailing newline). After a successful push these need no
# destination readback: the pushed state *is* the destination state, so reading
# it back would just re-transfer the whole image.
_BYTE_EXACT_KINDS = frozenset({"png", "jpeg"})

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

# Backstop interval. The watchers are the primary trigger and fire on every
# change, so this poll only exists to recover the rare event they miss (a
# wedged watcher, a restart gap, a failed push). Keeping it coarse means an
# idle session does not read the whole clipboard every few seconds.
IDLE_POLL_SECONDS = _env_seconds("IDLE_POLL_SECONDS", 60.0)

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
    except subprocess.TimeoutExpired:
        # Distinguish a hung helper from an empty clipboard in the logs.
        log.warning("%s timed out after %ss", cmd[0], timeout)
        return None, b""
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


def _group_alive(pgid: int) -> bool:
    """True if a process group with this id still exists."""
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _prune_owners() -> None:
    """Forget owner groups that no longer exist.

    A recorded pgid can be recycled by the kernel once its owner exits, so
    keeping dead entries around risks signalling an unrelated group later.
    """
    with _owner_lock:
        dead = [pgid for pgid in _owner_pgids if not _group_alive(pgid)]
        for pgid in dead:
            _owner_pgids.discard(pgid)


def _register_watcher(p: subprocess.Popen) -> None:
    with _watcher_lock:
        _watcher_procs.add(p)


def _unregister_watcher(p: subprocess.Popen) -> None:
    with _watcher_lock:
        _watcher_procs.discard(p)


def _terminate_proc(p: subprocess.Popen) -> None:
    """Terminate a child, escalating to SIGKILL; never raises."""
    try:
        p.terminate()
    except OSError:
        return
    try:
        p.wait(timeout=2)
    except subprocess.TimeoutExpired:
        try:
            p.kill()
            p.wait(timeout=2)
        except (subprocess.TimeoutExpired, OSError):
            pass
    except OSError:
        pass


def _cleanup_watchers() -> None:
    """Terminate the watcher children we spawned (best effort)."""
    with _watcher_lock:
        procs = list(_watcher_procs)
        _watcher_procs.clear()
    for p in procs:
        _terminate_proc(p)


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
    _prune_owners()
    with _owner_lock:
        _owner_pgids.add(p.pid)
    return True


def _cleanup_owners() -> None:
    """Kill the clipboard-owner process groups we spawned (best effort)."""
    with _owner_lock:
        pgids = list(_owner_pgids)
        _owner_pgids.clear()
    for pgid in pgids:
        if _group_alive(pgid):
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


# Throttle for the unreadable-offer diagnostic: the state readers run on every
# event and backstop poll, so log at most once per distinct offer set per
# window instead of flooding the journal.
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

    Priority (highest first):
      png  image/png
      jpeg image/jpeg
      uri  x-special/gnome-copied-files  (GNOME file copy, QQ stickers)
      uri  text/uri-list                 (WeChat images, generic file list)
      html text/html                     (QQ rich text)
      text UTF8_STRING / text/plain / STRING

    Image bytes outrank a file URI when both are offered; see the module
    docstring for why.
    """
    targets = x_targets()
    log.debug("read X: %d targets", len(targets))
    if not targets or targets == {"TARGETS"}:
        return None
    if X_PNG in targets:
        data = x_read(X_PNG)
        if data:
            return ("png", data, h(data))
    if X_JPEG in targets:
        data = x_read(X_JPEG)
        if data:
            return ("jpeg", data, h(data))
    if X_GNOME_FILES in targets:
        data = normalize_uri(x_read(X_GNOME_FILES) or b"")
        if data:
            return ("uri", data, h(data))
    if X_URI in targets:
        data = normalize_uri(x_read(X_URI) or b"")
        if data:
            return ("uri", data, h(data))
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

    Priority: png > jpeg > uri-list > html > text. Image bytes outrank a
    file URI when both are offered; see the module docstring for why.
    """
    types = wl_types()
    log.debug("read W: %d types", len(types))
    if not types:
        return None
    if W_PNG in types:
        data = wl_read(W_PNG)
        if data:
            return ("png", data, h(data))
    if W_JPEG in types:
        data = wl_read(W_JPEG)
        if data:
            return ("jpeg", data, h(data))
    if W_URI in types:
        data = normalize_uri(wl_read(W_URI) or b"")
        if data:
            return ("uri", data, h(data))
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


def _destination_after_push(state, readback):
    """State to record for the side that just received a push.

    Binary mimes survive both helpers byte-exact, so the pushed state *is* the
    destination and the readback (which would re-transfer the whole image) is
    skipped. Text may be reshaped by wl-copy, so it is measured.
    """
    if state[0] in _BYTE_EXACT_KINDS:
        return state
    return readback() or state


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
        # failed push is retried on the next event or backstop poll instead of
        # stalling. See _destination_after_push for the destination state.
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
                self.last_w = _destination_after_push(state, w_state)

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
                self.last_x = _destination_after_push(state, x_state)


def _watch_loop(run_once, name: str) -> None:
    """Run run_once() forever, backing off only when it reports a failure.

    run_once() returns True after a healthy run (an event was handled, or the
    helper was recycled on schedule) and False when it failed. Only failures
    back off, so a one-shot helper that legitimately exits after every event
    (clipnotify) is relaunched immediately instead of being throttled as if it
    kept failing. A watcher must never die silently either, so a failure is
    retried with capped backoff rather than escaping the loop.
    """
    delay = WATCH_BACKOFF_MIN
    while not _shutdown.is_set():
        try:
            healthy = run_once()
        except Exception:  # noqa: BLE001 - a watcher must never die
            log.exception("%s failed", name)
            healthy = False
        if healthy:
            delay = WATCH_BACKOFF_MIN
            continue
        time.sleep(delay)
        delay = min(delay * 2, WATCH_BACKOFF_MAX)


def watch_clipnotify(syncer: Syncer):
    """X -> W: forward each X11 selection owner change.

    clipnotify (nixpkgs) is a one-shot trigger by design: it blocks until the
    next CLIPBOARD/PRIMARY owner-change event and then exits silently (no
    output, no flags). Its intended usage is a relaunch loop
    (`while clipnotify; do ...; done`), so we do exactly that: every exit
    means "something changed", and we re-read the X state. PRIMARY
    (click-selection) changes also trigger it, but the digest-based dedup in
    Syncer.on_x_change filters those out.
    """
    clipnotify = shutil.which("clipnotify")
    if clipnotify is None:
        log.error("clipnotify not found in PATH; X -> W sync disabled")
        return

    def once() -> bool:
        p = subprocess.Popen(
            [clipnotify], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        _register_watcher(p)
        try:
            p.wait(timeout=WATCH_RECYCLE_SECONDS)
        except subprocess.TimeoutExpired:
            # No X selection change for a whole interval: just recycle.
            return True
        finally:
            _unregister_watcher(p)
            if p.poll() is None:
                _terminate_proc(p)
        if p.returncode != 0:
            log.warning("clipnotify exited with status %s", p.returncode)
            return False
        syncer.on_x_change()
        return True

    _watch_loop(once, "clipnotify")


def _watch_once(cmd: list[str], on_event, recycle: float) -> bool:
    """Run `cmd`, call on_event() per stdout line, then recycle the child.

    Returns True when the child was recycled on schedule, False when it exited
    on its own first (a failed helper) so the caller can back off. Bounding a
    watcher's lifetime is what lets the daemon recover from a wedged helper
    without detecting the wedge: a fresh child is spawned on every call.
    """
    recycled = threading.Event()

    def recycle_child() -> None:
        recycled.set()
        p.terminate()

    with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as p:
        _register_watcher(p)
        recycler = threading.Timer(recycle, recycle_child)
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
                _terminate_proc(p)
    return recycled.is_set()


def watch_wayland(syncer: Syncer):
    """W -> X: forward each Wayland selection change.

    `wl-paste --watch echo` runs `echo` on every selection change (echo
    ignores stdin and prints one newline -- a pure change signal); with no
    --type, wl-paste falls back to any offered type, so a single watcher
    covers every offer instead of one watcher per mime type. We re-read the
    full state afterwards.

    The watch child is recycled every WATCH_RECYCLE_SECONDS (see _watch_once)
    so a wedged watcher cannot silently stall W -> X sync.
    """
    _watch_loop(
        lambda: _watch_once(
            ["wl-paste", "--watch", "echo"],
            syncer.on_w_change,
            WATCH_RECYCLE_SECONDS,
        ),
        "wl-paste --watch",
    )


def watch_poll(syncer: Syncer, interval: float = IDLE_POLL_SECONDS):
    """Slow backstop for the events the watchers miss.

    The watchers fire on every change, so this is only a safety net: a wedged
    watcher (until its recycle), the small registration gap when a watcher
    restarts, or a push that failed with no later event to retry it. Reading
    first syncs whatever is already on the clipboards at startup. on_*_change
    is digest-deduped, so a poll with nothing changed is a cheap no-op.
    """
    while not _shutdown.is_set():
        try:
            syncer.on_x_change()
            syncer.on_w_change()
        except Exception:  # noqa: BLE001 - the poller must never die
            log.exception("fallback poll failed")
        time.sleep(interval)


_shutdown = threading.Event()


def _handle_shutdown(signum, _frame) -> None:
    log.info("received signal %s, shutting down", signum)
    _shutdown.set()


def main():
    logging.basicConfig(
        level=logging.DEBUG if os.environ.get("DEBUG") else logging.INFO,
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
        target=watch_wayland, args=(syncer,), daemon=True, name="w2x"
    ).start()
    threading.Thread(
        target=watch_poll, args=(syncer,), daemon=True, name="backstop"
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
