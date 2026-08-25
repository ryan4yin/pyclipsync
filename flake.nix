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
          pyclipsync = pkgs.callPackage ./nix/package.nix { };

          default = self.packages.${system}.pyclipsync;
        });

      homeModules = {
        default = import ./nix/home-module.nix;
      };
    };
}
