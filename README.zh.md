# pyclipsync

**English** | 中文

给 niri、Hyprland 这类通过 [xwayland-satellite](https://github.com/Supreeeme/xwayland-satellite) 跑 X11 应用的 Wayland 合成器用的剪贴板同步守护进程，让 Wayland 原生应用和 X11 应用能正常共享剪贴板。

## 问题

用 xwayland-satellite 的时候，X11 应用（微信、QQ 这些）跑在合成器外面的一个 rootless X server 里。satellite 其实也做了 X11 `CLIPBOARD` 和 Wayland 剪贴板之间的双向桥接，但做得不完整也不可靠，所以剪贴板**时灵时不灵**：

- X11 和 Wayland 的剪贴板格式（target/mime）并不一一对应，`x-special/gnome-copied-files`、`application/x-qt-image`、裸的 `UTF8_STRING` 这些 X 专有格式没有 Wayland 对应物，直接被丢掉；
- X11 剪贴板是请求式的：复制的那个应用当 owner，别人粘贴得现场找它要数据。有些应用（某些 GTK/Qt 程序、复制完就退出的程序）这时候给不出来；
- satellite 对 owner 的跟踪有时会失效，X11 应用就一直粘到旧内容。

实际用起来连纯文本都看运气，微信/QQ（`QT_QPA_PLATFORM=xcb`）里复制图片、富文本、文件更是基本必挂。这是 satellite 生态的已知问题（[xwayland-satellite#50](https://github.com/Supreeeme/xwayland-satellite/issues/50)）。

## 原理

本质就是一个 Python 小脚本，调度一套久经考验的 CLI 工具（和 bash 版 [clipsync](https://github.com/123hi123/clipsync) 用的是同一套）：

| 方向          | 怎么发现变化                                | 怎么读     | 怎么写                       |
| ------------- | ------------------------------------------- | ---------- | ---------------------------- |
| X11 → Wayland | `clipnotify`（循环重启）+ 1 秒轮询          | `xclip`    | `wl-copy`                    |
| Wayland → X11 | `wl-paste --watch`（每个 mime 一个）+ 1 秒轮询 | `wl-paste` | `xclip`（接管 CLIPBOARD）    |

支持的类型，按优先级从高到低（映射参考 [linuxqq-clipsync](https://github.com/SHORiN-KiWATA/linuxqq-clipsync)）：

- **文件/图片链接** — X11 侧：`x-special/gnome-copied-files`（QQ 表情、GNOME 文件复制）或 `text/uri-list`（微信/QQ 图片）；Wayland 侧：`text/uri-list`。同步前归一：去掉 `copy` 头，裸路径统一改写成 `file://`
- **`image/png`**、**`image/jpeg`** — 两侧同名
- **`text/html`** — QQ 富文本，两侧同名
- **纯文本** — X11 侧 `UTF8_STRING`，Wayland 侧 `text/plain`

## 为什么选 pyclipsync

satellite 场景下现成的方案都有硬伤（satellite 自带的桥接为什么不行，见[问题](#问题)）：

| 工具                                                                                     | 问题                                                                                                        |
| ---------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------- |
| [clipsync](https://github.com/123hi123/clipsync)（两个 bash 守护进程）             | X→W 不支持 `text/html`；没有状态、没有轮询——X 侧读失败可能把 Wayland 侧清空，推送失败就得等下一次复制 |
| [wl-x11-clipsync](https://github.com/arabianq/wl-x11-clipsync)（单个 Python 脚本）   | 不支持 `gnome-copied-files` / 微信 `x-qt-image`；作者自己都说图片→X11 "works really badly"        |
| [qq-wayland-clipboard](https://github.com/w568w/qq-wayland-clipboard)（Rust wrapper + Xvfb） | 只修原生 Wayland 模式的 QQ，X11 客户端用不上                                   |

pyclipsync 补的洞：

- `text/html` 双向都同步（QQ 富文本）
- 真正的状态机：两边各记一份 sha256，读取、判重、推送在同一把锁里一次做完，不会互相打架；目标侧状态以实际读回为准（`wl-copy` 偷偷加换行这种坑不会把判重带偏）；1 秒轮询兜底，推失败了下轮自动重试
- 绝不推垃圾：剪贴板是空的、或者读不出来，就什么都不动；某个 target 读不出来就换下一个试
- 自带端到端集成测试（bash 工具一个测试都没有）：12 个用例在真实的 X11 + Wayland 会话里跑真实守护进程，每种类型双向逐字节校验。详见[测试](#测试)

## 依赖

- `python3`（只用标准库，没有第三方 Python 包）
- [`xclip`](https://github.com/astrand/xclip)
- [`clipnotify`](https://github.com/cdown/clipnotify)
- [`wl-clipboard`](https://github.com/bugaevc/wl-clipboard)（`wl-copy`/`wl-paste`）

都在 nixpkgs 里。

## 使用

用 systemd 用户服务跑（unit 文件见 [`pyclipsync.service`](./pyclipsync.service)）。需要图形会话里的 `DISPLAY` 和 `WAYLAND_DISPLAY`（登录时由 display manager 设好）：

```sh
install -Dm755 pyclipsync.py ~/.local/bin/pyclipsync
install -Dm644 pyclipsync.service ~/.config/systemd/user/pyclipsync.service
systemctl --user enable --now pyclipsync
journalctl --user -u pyclipsync -f
```

想在前台试跑或调试的话，直接在终端执行 `pyclipsync`；加 `DEBUG=1` 可以开 debug 日志。

## Nix

flake 提供 `pyclipsync` 包和一个 home-manager module（装好并作为 systemd 用户服务运行）：

```nix
# flake.nix
inputs.pyclipsync = {
  url = "github:ryan4yin/pyclipsync";
  inputs.nixpkgs.follows = "nixpkgs";
};
```

推荐让 home-manager 管这个服务（跟着图形会话启用，挂了自动重启）：

```nix
home-manager.users.<user> = {
  imports = [ inputs.pyclipsync.homeModules.default ];
  services.pyclipsync.enable = true;
};
```

或者只拿二进制自己跑：

```nix
home.packages = [ inputs.pyclipsync.packages.${system}.pyclipsync ];
```

不用 flake：

```nix
{ flake ? (fetchTarball "github:ryan4yin/pyclipsync") }:
flake.packages.${builtins.currentSystem}.default
```

## 测试

集成测试在 [`tests/test_sync.py`](./tests/test_sync.py)，标准库 `unittest`，没有额外依赖。会在真实的 X11 (XWayland) + Wayland 会话里把守护进程跑起来，把每种类型双向同步都按字节校验一遍，包括 QQ 表情（`gnome-copied-files`）和快速连续复制两次的竞态。没有 `DISPLAY` / `WAYLAND_DISPLAY` / 辅助工具时整套自动跳过；失败了会保留工作目录、打印守护进程日志尾部，方便排查。

```sh
# 测仓库里的 pyclipsync.py
python3 -m unittest discover -v

# 测构建出来的二进制
PYCLIPSYNC=$(nix build .#default --print-out-paths)/bin/pyclipsync \
    python3 -m unittest discover -v
```

## 致谢

- 设计思路、微信 MIME 类型处理参考 [123hi123/clipsync](https://github.com/123hi123/clipsync)（MIT）
- QQ MIME 类型映射参考 [SHORiN-KiWATA/linuxqq-clipsync](https://github.com/SHORiN-KiWATA/linuxqq-clipsync)，另外还看了 [arabianq/wl-x11-clipsync](https://github.com/arabianq/wl-x11-clipsync) 和 [w568w/qq-wayland-clipboard](https://github.com/w568w/qq-wayland-clipboard)
- [xwayland-satellite](https://github.com/Supreeeme/xwayland-satellite)
- [wl-clipboard](https://github.com/bugaevc/wl-clipboard)
- [xclip](https://github.com/astrand/xclip)、[clipnotify](https://github.com/cdown/clipnotify)

## 许可证

[MIT](./LICENSE)
