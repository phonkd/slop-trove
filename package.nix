{ lib, python3Packages }:

# The application + CLI (`slop-trove`) and the importable `slop_trove` module.
# If any dependency is missing from your nixpkgs pin, add it here.
python3Packages.buildPythonApplication {
  pname = "slop-trove";
  version = "0.0.1";
  pyproject = true;
  src = ./.;

  build-system = [ python3Packages.setuptools ];

  dependencies = with python3Packages; [
    psycopg            # uses the system libpq at runtime
    pgvector
    httpx
    mcp
  ];

  # No test suite yet; just make sure it imports.
  pythonImportsCheck = [ "slop_trove" ];

  meta = {
    description = "Personal data embedding + semantic search platform";
    mainProgram = "slop-trove";
  };
}
