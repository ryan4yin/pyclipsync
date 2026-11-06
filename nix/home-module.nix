# Home-manager module: runs pyclipsync as a systemd user service.
#
# The flake applies this file to itself (`homeModules.default`), so consumers
# only need:
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
self:
{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.services.pyclipsync;
in
{
  options.services.pyclipsync = {
    enable = lib.mkEnableOption "pyclipsync, the Wayland <-> X11 clipboard sync daemon";

    package = lib.mkOption {
      type = lib.types.package;
      default = self.packages.${pkgs.stdenv.hostPlatform.system}.pyclipsync;
      defaultText = lib.literalExpression "pyclipsync.packages.\${pkgs.stdenv.hostPlatform.system}.pyclipsync";
      description = "The pyclipsync package to run.";
    };
  };

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
        ExecStart = lib.getExe cfg.package;
        Restart = "on-failure";
        RestartSec = 1;
        TimeoutStopSec = 5;
      };
    };
  };
}
