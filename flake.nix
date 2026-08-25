{
  description = "Wayland <-> X11 clipboard sync daemon for xwayland-satellite setups";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  };

  outputs =
  {
    self,
    nixpkgs,
  }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
      ];
      forAllSystems = nixpkgs.lib.genAttrs systems;
    in
    {
      packages = forAllSystems (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
        in
        {
          pyclipsync = pkgs.python3Packages.buildPythonApplication {
            pname = "pyclipsync";
            version = "0.1.0";
            src = ./.;

            # single script, no third-party python deps
            format = "other";
            dependencies = [ ];
            doCheck = false;

            nativeBuildInputs = [ pkgs.makeWrapper ];
            # The script keeps its `#!/usr/bin/env python3` shebang; python3
            # plus wl-copy/wl-paste (wl-clipboard), xclip and clipnotify are
            # all resolved from the wrapper's PATH at runtime.
            installPhase = ''
              mkdir -p $out/bin
              install -m 755 pyclipsync.py $out/bin/pyclipsync
              wrapProgram $out/bin/pyclipsync \
                --prefix PATH : '${pkgs.python3}/bin' \
                --prefix PATH : '${pkgs.wl-clipboard}/bin' \
                --prefix PATH : '${pkgs.xclip}/bin' \
                --prefix PATH : '${pkgs.clipnotify}/bin'
            '';

            meta = with pkgs.lib; {
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
          };

          default = self.packages.${system}.pyclipsync;
        });
    };
}
