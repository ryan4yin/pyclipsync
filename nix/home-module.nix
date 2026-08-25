# Home-manager module: runs pyclipsync as a systemd user service.
#
# Expects the `pyclipsync` module argument to be the flake (output set),
# e.g. via specialArgs:
#
#   inputs.pyclipsync = {
#     url = "github:ryan4yin/pyclipsync";
#     inputs.nixpkgs.follows = "nixpkgs";
#   };
#
#   home-manager.users.<user> = {
#     imports = [ inputs.pyclipsync.homeModules.default ];
#     services.pyclipsync.enable = true;
#   };
{
  config,
  lib,
  pkgs,
  pyclipsync,
  ...
}:
let
  cfg = config.services.pyclipsync;
in
{
  options.services.pyclipsync.enable =
    lib.mkEnableOption "pyclipsync, the Wayland <-> X11 clipboard sync daemon";

  config = lib.mkIf cfg.enable {
    # Needs DISPLAY + WAYLAND_DISPLAY from the graphical session (provided
    # by the compositor's session registration).
    systemd.user.services.pyclipsync = {
      Unit = {
        Description = "Wayland <-> X11 clipboard sync (pyclipsync)";
        After = [ "graphical-session.target" ];
        PartOf = [ "graphical-session.target" ];
      };
      Install.WantedBy = [ "graphical-session.target" ];
      Service = {
        ExecStart = "${pyclipsync.packages.${pkgs.system}.pyclipsync}/bin/pyclipsync";
        Restart = "on-failure";
        RestartSec = 1;
        TimeoutStopSec = 5;
      };
    };
  };
}
