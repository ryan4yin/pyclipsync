{ python3Packages
, wl-clipboard
, xclip
, clipnotify
, lib
}:

python3Packages.buildPythonApplication {
  pname = "pyclipsync";
  version = "0.1.2";
  src = ../.;

  # single script, no third-party python deps
  format = "other";
  doCheck = true;

  # The unit tests need no graphical session; the integration class in
  # tests/test_sync.py skips itself without DISPLAY/WAYLAND_DISPLAY.
  checkPhase = ''
    runHook preCheck
    ${python3Packages.python.interpreter} -m unittest discover -v -s tests
    runHook postCheck
  '';

  installPhase = ''
    mkdir -p $out/bin
    install pyclipsync.py $out/bin/pyclipsync
  '';

  # The daemon shells out to wl-copy/wl-paste (wl-clipboard), xclip and
  # clipnotify at runtime. Declaring them as propagatedBuildInputs lets the
  # python wrap hook add their bin/ to the wrapper PATH automatically (and
  # propagates them to consumers, e.g. a systemd service). The python
  # interpreter itself (absolute shebang + PATH) is handled by
  # buildPythonApplication.
  propagatedBuildInputs = [
    wl-clipboard
    xclip
    clipnotify
  ];

  meta = with lib; {
    description = "Wayland <-> X11 clipboard sync daemon for xwayland-satellite setups";
    longDescription = ''
      Bridges the X11 CLIPBOARD selection and the Wayland data device
      for compositors that run X11 apps through xwayland-satellite
      (niri, Hyprland, ...), which have no compositor-level clipboard
      bridging. Orchestrates xclip, clipnotify and wl-clipboard.
    '';
    homepage = "https://github.com/ryan4yin/pyclipsync";
    license = licenses.mit;
    platforms = platforms.linux;
    mainProgram = "pyclipsync";
  };
}
