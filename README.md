# pyclipsync

Wayland <-> X11 clipboard synchronization daemon for Wayland compositors that
run X11 apps through [xwayland-satellite](https://github.com/Supreeeme/xwayland-satellite)
(niri, Hyprland, ...).

## The problem

With xwayland-satellite, X11 apps live in a rootless X server that is *outside*
the compositor. Neither the compositor nor the satellite bridges the X11
`CLIPBOARD` selection with the Wayland data device, so copies made in X11 apps
(e.g. WeChat running under `QT_QPA_PLATFORM=xcb`) never reach Wayland apps and
vice versa. This is a known gap in the satellite ecosystem
([xwayland-satellite#50](https://github.com/Supreeeme/xwayland-satellite/issues/50)).

## How it works

`pyclipsync` is a small Python orchestrator over battle-tested CLI tools — the
same primitives as the bash tool
[clipsync](https://github.com/123hi123/clipsync) by 123hi123 (see there for the
original design and the WeChat `application/x-qt-image` background):

| direction    | watcher                              | reader     | writer |
| ------------ | ------------------------------------ | ---------- | ------ |
| X11 -> Wayland | `clipnotify` (relaunched per event) + a 1s poll | `xclip` | `wl-copy` |
| Wayland -> X11 | `wl-paste --watch` (one per mime)   | `wl-paste` | `xclip` (becomes the CLIPBOARD selection owner) |

Details worth knowing:

- **`clipnotify` is a one-shot trigger by design** — it blocks until the next
  selection-owner change and then *exits silently* (no output, no flags).
  `pyclipsync` therefore relaunches it in a loop; every exit means "something
  changed". A 1-second **X poller** backs it up: the relaunch loop has a tiny
  registration gap in which an owner change can be missed, and the poller
  guarantees that such a change is still synced.
- **`wl-paste --watch`** takes exactly one command (exec'ed as-is, no shell).
  `pyclipsync` passes bare `echo`, which ignores stdin and prints one newline
  per new offer — a pure change signal.
- **`xclip` write mode forks** a background child to hold the selection; that
  child inherits stdout/stderr, so the writer points them at `/dev/null`
  (never `capture_output`) or the call would block until timeout.

A per-side "last synced" digest (sha256), evaluated under a single lock together
with the read and the push, prevents X -> W -> X feedback loops and echo
ping-pong under rapid successive copies.

Content types, highest priority wins:

- `text/uri-list` — WeChat/QQ images arrive as `text/uri-list` +
  `application/x-qt-image`; normalized to `file://` URIs on both sides
- `image/png`
- text (`UTF8_STRING` / `text/plain`)

Empty clipboards are never propagated (protects the other side's content).

## Dependencies

- `python3` (standard library only — no third-party python packages)
- [`xclip`](https://github.com/astrand/xclip)
- [`clipnotify`](https://github.com/cdown/clipnotify)
- [`wl-clipboard`](https://github.com/bugaevc/wl-clipboard) (`wl-copy`/`wl-paste`)

All are in nixpkgs.

## Usage

```sh
pyclipsync
```

or as a systemd user service:

```ini
# ~/.config/systemd/user/pyclipsync.service
[Unit]
Description=Wayland <-> X11 clipboard sync (pyclipsync)
After=graphical-session.target

[Service]
ExecStart=%h/.nix-profile/bin/pyclipsync
Restart=on-failure
RestartSec=1

[Install]
WantedBy=graphical-session.target
```

```sh
systemctl --user enable --now pyclipsync
journalctl --user -u pyclipsync -f
```

The service needs `DISPLAY` and `WAYLAND_DISPLAY` from the graphical session
(provided by the compositor's session registration).

## Nix

```nix
{ flake ? (fetchTarball "github:ryan4yin/pyclipsync") }:
flake.packages.${builtins.currentSystem}.default
```

or as a flake input:

```nix
inputs.pyclipsync.url = "github:ryan4yin/pyclipsync";
# ...
home.packages = [ inputs.pyclipsync.packages.${system}.pyclipsync ];
```

## Credits

- Design and WeChat MIME-type handling inspired by
  [123hi123/clipsync](https://github.com/123hi123/clipsync) (MIT)
- [xwayland-satellite](https://github.com/Supreeeme/xwayland-satellite)
- [wl-clipboard](https://github.com/bugaevc/wl-clipboard)
- [xclip](https://github.com/astrand/xclip), [clipnotify](https://github.com/cdown/clipnotify)

## License

[MIT](./LICENSE)
