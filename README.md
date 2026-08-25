# pyclipsync

**English** | [中文](./README.zh.md)

Wayland <-> X11 clipboard synchronization daemon for Wayland compositors that
run X11 apps through [xwayland-satellite](https://github.com/Supreeeme/xwayland-satellite)
(niri, Hyprland, ...).

## The problem

With xwayland-satellite, X11 apps live in a rootless X server that is *outside*
the compositor. The satellite does bridge the X11 `CLIPBOARD` selection with
the Wayland data device in both directions, but the bridge is incomplete and
fragile, so the result is a clipboard that **works sometimes and not others**:

- X↔Wayland **target/mime translation** drops X-specific targets
  (`x-special/gnome-copied-files`, `application/x-qt-image`, raw
  `UTF8_STRING`) that have no Wayland equivalent.
- X11 selection is **request-based with lazy owners**: the satellite must ask
  the current owner for the data, and some owners (certain GTK/Qt apps, and
  apps that exit right after copying) fail to serve it.
- The satellite's **selection tracking can go stale**, so an X11 app may keep
  pasting an older copy.

In practice even plain text is hit or miss, and images, rich text and file
copies from WeChat/QQ (`QT_QPA_PLATFORM=xcb`) fail most of the time. This is a
known gap in the satellite ecosystem
([xwayland-satellite#50](https://github.com/Supreeeme/xwayland-satellite/issues/50)).

## How it works

A small Python orchestrator over the same battle-tested CLI tools as the bash
tool [clipsync](https://github.com/123hi123/clipsync):

| direction      | watcher                                     | reader     | writer                 |
| -------------- | ------------------------------------------- | ---------- | ---------------------- |
| X11 -> Wayland | `clipnotify` relaunch loop + 1s poll        | `xclip`    | `wl-copy`              |
| Wayland -> X11 | `wl-paste --watch` (one per mime) + 1s poll | `wl-paste` | `xclip` (CLIPBOARD owner) |

Content types, highest priority wins (mapping follows
[linuxqq-clipsync](https://github.com/SHORiN-KiWATA/linuxqq-clipsync)):

- **file/image links** — X11: `x-special/gnome-copied-files` (QQ stickers,
  GNOME file copy) or `text/uri-list` (WeChat/QQ images); Wayland:
  `text/uri-list`. Normalized before sync: the `copy` action header is
  stripped and bare absolute paths are rewritten to `file://` URIs
- **`image/png`**, **`image/jpeg`** — same mime on both sides
- **`text/html`** — QQ rich text, same mime on both sides
- **text** — X11: `UTF8_STRING`; Wayland: `text/plain`

## Why pyclipsync

For satellite setups, the existing options each fall short (the satellite's
own bridge is covered in [The problem](#the-problem)):

| tool                                                                                   | gap                                                                                                                        |
| -------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------- |
| [clipsync](https://github.com/123hi123/clipsync) (two bash daemons)          | no X→W `text/html`; no state or polling — a failed read can wipe the Wayland side, a failed push waits for the next copy |
| [wl-x11-clipsync](https://github.com/arabianq/wl-x11-clipsync) (single Python script)   | no `gnome-copied-files` / WeChat `x-qt-image`; image→X11 "works really badly"                                              |
| [qq-wayland-clipboard](https://github.com/w568w/qq-wayland-clipboard) (Rust wrapper + Xvfb) | only for QQ in *native Wayland* mode, not X11 clients                                        |

pyclipsync adds what they lack:

- **`text/html` both ways** (QQ rich text)
- **a real state machine**: per-side sha256 digest, read + dedup + push
  atomic under one lock, destination recorded from a readback, 1s pollers
  retrying failed pushes
- **no destructive pushes**: empty or unreadable sources are never
  propagated; unservable targets fall back to the next one
- **integration-tested end to end** — the bash tools ship no tests: 12
  cases run the real daemon under a live X11 + Wayland session, every
  type byte-exact in both directions. See [Testing](#testing).

## Dependencies

- `python3` (standard library only — no third-party python packages)
- [`xclip`](https://github.com/astrand/xclip)
- [`clipnotify`](https://github.com/cdown/clipnotify)
- [`wl-clipboard`](https://github.com/bugaevc/wl-clipboard) (`wl-copy`/`wl-paste`)

All are in nixpkgs.

## Usage

Run it as a systemd user service (unit file:
[`pyclipsync.service`](./pyclipsync.service)). It needs `DISPLAY` and
`WAYLAND_DISPLAY` from the graphical session (set by the display manager on
login):

```sh
install -Dm755 pyclipsync.py ~/.local/bin/pyclipsync
install -Dm644 pyclipsync.service ~/.config/systemd/user/pyclipsync.service
systemctl --user enable --now pyclipsync
journalctl --user -u pyclipsync -f
```

For a quick try or debugging, run `pyclipsync` in a terminal; `DEBUG=1`
turns on debug logging.

## Nix

The flake exposes a `pyclipsync` package and a home-manager module that
installs it as a systemd user service:

```nix
# flake.nix
inputs.pyclipsync = {
  url = "github:ryan4yin/pyclipsync";
  inputs.nixpkgs.follows = "nixpkgs";
};
```

Recommended: let home-manager manage the service (follows the graphical
session, restarts on failure):

```nix
home-manager.users.<user> = {
  imports = [ inputs.pyclipsync.homeModules.default ];
  services.pyclipsync.enable = true;
};
```

Or just take the binary and run it yourself:

```nix
home.packages = [ inputs.pyclipsync.packages.${system}.pyclipsync ];
```

Without flakes:

```nix
{ flake ? (fetchTarball "github:ryan4yin/pyclipsync") }:
flake.packages.${builtins.currentSystem}.default
```

## Testing

Integration tests live in [`tests/test_sync.py`](./tests/test_sync.py)
(standard-library `unittest`, no extra dependencies). They start a real daemon
under a **live X11 (XWayland) + Wayland session** and verify byte-exact sync
for every supported type in both directions, including the QQ sticker
(`gnome-copied-files`) case and a rapid double-copy race. The whole suite
skips when `DISPLAY` / `WAYLAND_DISPLAY` / the helper tools are missing; on
failure the workdir is kept and the daemon log tail is printed.

```sh
# test the repo's pyclipsync.py
python3 -m unittest discover -v

# test the built binary
PYCLIPSYNC=$(nix build .#default --print-out-paths)/bin/pyclipsync \
    python3 -m unittest discover -v
```

## Credits

- Design and WeChat MIME-type handling inspired by
  [123hi123/clipsync](https://github.com/123hi123/clipsync) (MIT)
- QQ MIME-type mapping follows
  [SHORiN-KiWATA/linuxqq-clipsync](https://github.com/SHORiN-KiWATA/linuxqq-clipsync);
  also consulted [arabianq/wl-x11-clipsync](https://github.com/arabianq/wl-x11-clipsync)
  and [w568w/qq-wayland-clipboard](https://github.com/w568w/qq-wayland-clipboard)
- [xwayland-satellite](https://github.com/Supreeeme/xwayland-satellite)
- [wl-clipboard](https://github.com/bugaevc/wl-clipboard)
- [xclip](https://github.com/astrand/xclip), [clipnotify](https://github.com/cdown/clipnotify)

## License

[MIT](./LICENSE)
