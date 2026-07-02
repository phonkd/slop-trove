{
  description = "slop-trove — personal data embedding + semantic search platform";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs =
    { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" "aarch64-darwin" ];
      forAll = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
    in
    {
      packages = forAll (pkgs: {
        default = pkgs.callPackage ./package.nix { };
        slop-trove = self.packages.${pkgs.system}.default;
      });

      devShells = forAll (pkgs: {
        default = pkgs.mkShell {
          packages = [
            (pkgs.python3.withPackages (ps: with ps; [ psycopg pgvector httpx mcp ]))
            pkgs.postgresql
          ];
        };
      });

      # Consumed by nixconfig: imports the service module.
      nixosModules.default = import ./nixos-module.nix self;

      formatter = forAll (pkgs: pkgs.nixfmt-rfc-style);
    };
}
