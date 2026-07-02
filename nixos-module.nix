self:
{ config, lib, pkgs, ... }:

let
  cfg = config.services.slop-trove;
  pkg = cfg.package;

  commonEnv = {
    SLOP_TROVE_DB_URL = cfg.database.url;
    SLOP_TROVE_EMBED_ENDPOINT = cfg.embedding.endpoint;
    SLOP_TROVE_EMBED_MODEL = cfg.embedding.model;
    SLOP_TROVE_EMBED_DIM = toString cfg.embedding.dim;
    SLOP_TROVE_MCP_HOST = cfg.mcp.host;
    SLOP_TROVE_MCP_PORT = toString cfg.mcp.port;
  };
in
{
  options.services.slop-trove = {
    enable = lib.mkEnableOption "slop-trove personal data search";

    package = lib.mkOption {
      type = lib.types.package;
      default = self.packages.${pkgs.system}.default;
      defaultText = lib.literalExpression "slop-trove.packages.\${system}.default";
      description = "The slop-trove package to use.";
    };

    user = lib.mkOption {
      type = lib.types.str;
      default = "slop-trove";
      description = "User the services run as (also the local Postgres role).";
    };
    group = lib.mkOption {
      type = lib.types.str;
      default = "slop-trove";
    };
    stateDir = lib.mkOption {
      type = lib.types.path;
      default = "/var/lib/slop-trove";
    };

    database = {
      createLocally = lib.mkOption {
        type = lib.types.bool;
        default = true;
        description = "Provision a local Postgres + pgvector and a peer-auth role/db.";
      };
      name = lib.mkOption {
        type = lib.types.str;
        default = "slop-trove";
      };
      url = lib.mkOption {
        type = lib.types.str;
        default = "dbname=${cfg.database.name}";
        defaultText = lib.literalExpression ''"dbname=''${cfg.database.name}"'';
        description = "libpq connection string. Default uses the local socket + peer auth.";
      };
    };

    embedding = {
      endpoint = lib.mkOption {
        type = lib.types.str;
        example = "http://blac:11434";
        description = "Ollama-compatible /api/embed base URL.";
      };
      model = lib.mkOption {
        type = lib.types.str;
        default = "nomic-embed-text";
      };
      dim = lib.mkOption {
        type = lib.types.int;
        default = 768;
        description = "Embedding dimensionality; must match the model.";
      };
    };

    mcp = {
      host = lib.mkOption {
        type = lib.types.str;
        default = "127.0.0.1";
      };
      port = lib.mkOption {
        type = lib.types.port;
        default = 9120;
      };
    };

    sources.discord = {
      enable = lib.mkEnableOption "the Discord GDPR ingester (manual trigger)";
      path = lib.mkOption {
        type = lib.types.path;
        example = "/var/lib/slop-trove/exports/discord";
        description = "Path to the unzipped Discord data package.";
      };
    };
  };

  config = lib.mkIf cfg.enable {
    users.users.${cfg.user} = lib.mkIf (cfg.user == "slop-trove") {
      isSystemUser = true;
      group = cfg.group;
      home = cfg.stateDir;
      createHome = true;
    };
    users.groups.${cfg.group} = lib.mkIf (cfg.group == "slop-trove") { };

    # ── Optional local Postgres + pgvector ───────────────────────────────
    services.postgresql = lib.mkIf cfg.database.createLocally {
      enable = true;
      extensions = ps: with ps; [ pgvector ];
      ensureDatabases = [ cfg.database.name ];
      ensureUsers = [
        {
          name = cfg.user;
          ensureDBOwnership = true;
        }
      ];
    };

    # Install the pgvector extension as the postgres superuser (the app role
    # may not have privilege to CREATE EXTENSION itself).
    systemd.services.postgresql.postStart = lib.mkIf cfg.database.createLocally (
      lib.mkAfter ''
        ${config.services.postgresql.package}/bin/psql -d ${cfg.database.name} \
          -tAc "CREATE EXTENSION IF NOT EXISTS vector" || true
      ''
    );

    # ── MCP search server ────────────────────────────────────────────────
    systemd.services.slop-trove-mcp = {
      description = "slop-trove MCP search server";
      wantedBy = [ "multi-user.target" ];
      after = [ "network-online.target" ] ++ lib.optional cfg.database.createLocally "postgresql.service";
      wants = [ "network-online.target" ];
      requires = lib.optional cfg.database.createLocally "postgresql.service";
      environment = commonEnv;
      serviceConfig = {
        User = cfg.user;
        Group = cfg.group;
        ExecStartPre = "${lib.getExe pkg} init-db";
        ExecStart = "${lib.getExe pkg} serve";
        Restart = "on-failure";
        RestartSec = 5;
        StateDirectory = "slop-trove";
      };
    };

    # ── Discord ingest (oneshot: `systemctl start slop-trove-ingest-discord`) ─
    systemd.services.slop-trove-ingest-discord = lib.mkIf cfg.sources.discord.enable {
      description = "slop-trove: ingest the Discord export";
      after = lib.optional cfg.database.createLocally "postgresql.service";
      requires = lib.optional cfg.database.createLocally "postgresql.service";
      environment = commonEnv;
      serviceConfig = {
        Type = "oneshot";
        User = cfg.user;
        Group = cfg.group;
        ExecStart = "${lib.getExe pkg} ingest --source discord --path ${cfg.sources.discord.path}";
      };
    };
  };
}
