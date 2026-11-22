{
  description = "Quickshell front-end for YouTube that plays everything in mpv";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = {
    self,
    nixpkgs,
  }: let
    systems = ["x86_64-linux" "aarch64-linux"];
    forAllSystems = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
  in {
    packages = forAllSystems (pkgs: {
      default = pkgs.callPackage ./package.nix {};
    });

    homeManagerModules.default = import ./hm-module.nix;

    formatter = forAllSystems (pkgs: pkgs.alejandra);
  };
}
