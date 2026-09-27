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

  exportCfg = cfg.sources.discord.export;

  # Where the wrapper stages a token handed in at run time. Root-only dir on
  # tmpfs; systemd reads it as root for LoadCredential before dropping to the
  # service user, and ExecStopPost shreds it however the run ends.
  runtimeTokenPath = "/run/slop-trove/discord-token";
  tokenSource = if exportCfg.tokenFile != null then exportCfg.tokenFile else runtimeTokenPath;

  # DCE takes the token from --token or from DISCORD_TOKEN. Use the env var:
  # a user token is a full account credential, and argv is world-readable via
  # `ps` and gets echoed into the journal on failure.
  discordExportScript = pkgs.writeShellScript "slop-trove-export-discord" ''
    set -euo pipefail
    DISCORD_TOKEN="$(cat "$CREDENTIALS_DIRECTORY/discord-token")"
    export DISCORD_TOKEN
    export FUCK_RUSSIA=true   # suppress the interactive banner
    mkdir -p ${lib.escapeShellArg exportCfg.outputPath}
    exec ${lib.getExe exportCfg.package} \
      ${if exportCfg.scope == "dm" then "exportdm" else "exportall"} \
      --format Json \
      --utc \
      --parallel 1 \
      --output ${lib.escapeShellArg (exportCfg.outputPath + "/")}
  '';

  # The imperative front door. `systemctl start` cannot take arguments, so
  # taking the token at invocation means staging it somewhere the unit can
  # read -- that plus starting the unit is all this does. Running the export
  # *as* a unit rather than inline is deliberate: a first full export is hours
  # long, and a unit outlives the ssh session that kicked it off.
  discordExportWrapper = pkgs.writeShellApplication {
    name = "slop-trove-discord-export";
    runtimeInputs = with pkgs; [ coreutils systemd ];
    text = ''
      scope=${lib.escapeShellArg exportCfg.scope}
      token=""
      tokenfile=""
      reingest=0

      usage() {
        cat >&2 <<'USAGE'
      Usage: sudo slop-trove-discord-export [options]

        --token TOKEN       Discord *user* token (prefer the prompt: argv is
                            visible in `ps`)
        --token-file PATH   read the token from a file instead
        --reingest          after the export, purge source=discord and re-ingest
        -h, --help          this

      With no token option and a terminal attached, prompts for it (hidden).
      DISCORD_TOKEN in the environment is also honoured.

      Getting the token: Discord (desktop or web) -> Ctrl+Shift+I -> Network
      tab -> click any request to discord.com/api -> copy the `Authorization`
      request header verbatim.
      USAGE
      }

      while [ $# -gt 0 ]; do
        case "$1" in
          --token)      token="''${2:-}"; shift 2 ;;
          --token-file) tokenfile="''${2:-}"; shift 2 ;;
          --scope)      scope="''${2:-}"; shift 2 ;;
          --reingest)   reingest=1; shift ;;
          -h|--help)    usage; exit 0 ;;
          *) echo "unknown argument: $1" >&2; usage; exit 2 ;;
        esac
      done

      if [ "$(id -u)" -ne 0 ]; then
        echo "must run as root (stages a 0400 token under /run and starts a unit)" >&2
        echo "try: sudo slop-trove-discord-export" >&2
        exit 1
      fi

      if [ -z "$token" ]; then
        if [ -n "$tokenfile" ]; then
          token="$(cat "$tokenfile")"
        elif [ -n "''${DISCORD_TOKEN:-}" ]; then
          token="$DISCORD_TOKEN"
        elif [ -t 0 ]; then
          printf 'Discord user token (hidden): ' >&2
          read -rs token
          printf '\n' >&2
        else
          echo "no token given and no terminal to prompt on; see --help" >&2
          exit 2
        fi
      fi
      [ -n "$token" ] || { echo "empty token" >&2; exit 2; }

      # 0700 root-only dir on tmpfs. systemd's LoadCredential runs as root, so
      # the service user never needs access to the staged copy.
      install -d -m 0700 -o root -g root /run/slop-trove
      ( umask 077; printf '%s' "$token" > ${lib.escapeShellArg runtimeTokenPath} )
      chmod 0400 ${lib.escapeShellArg runtimeTokenPath}

      echo "starting export (scope=$scope) -- this can take hours" >&2
      echo "follow with: journalctl -fu slop-trove-export-discord" >&2

      if [ "$reingest" -eq 1 ]; then
        # Blocking: a oneshot's `systemctl start` returns when it is done.
        systemctl start slop-trove-export-discord
        echo "export done; replacing source=discord in the index" >&2
        systemctl start slop-trove-purge-discord
        systemctl start slop-trove-ingest-discord
      else
        systemctl start --no-block slop-trove-export-discord
        echo "then: sudo slop-trove-discord-export --reingest   # or, by hand:" >&2
        echo "      systemctl start slop-trove-purge-discord slop-trove-ingest-discord" >&2
      fi
    '';
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
      enable = lib.mkEnableOption "the Discord ingester (manual trigger)";
      path = lib.mkOption {
        type = lib.types.path;
        example = "/var/lib/slop-trove/exports/discord";
        description = ''
          Path to a Discord export. The layout is auto-detected: an unzipped
          GDPR data package (a `Messages/` dir — only your own messages), or a
          DiscordChatExporter JSON output directory (both sides, with authors).
        '';
      };

      # Acquiring the history is separate from ingesting it: the GDPR package
      # is something you download by hand, DCE is something we can run.
      export = {
        enable = lib.mkEnableOption ''
          pulling Discord history with DiscordChatExporter. Purely imperative:
          installs `slop-trove-discord-export` plus the oneshot unit it starts.
          There is no timer, and by default no stored token.

          Note this drives the Discord API with a *user* token, which is a
          self-bot under Discord's ToS. Deliberately opt-in
        '';
        package = lib.mkOption {
          type = lib.types.package;
          default = pkgs.discordchatexporter-cli;
          defaultText = lib.literalExpression "pkgs.discordchatexporter-cli";
        };
        tokenFile = lib.mkOption {
          type = lib.types.nullOr lib.types.path;
          default = null;
          example = "/run/secrets/discord-user-token";
          description = ''
            File holding the Discord user token, read via systemd
            LoadCredential and passed to DCE through DISCORD_TOKEN -- never on
            the command line.

            Default `null` means **no stored credential**: you hand the token
            over per run with `sudo slop-trove-discord-export`, which stages it
            under /run for the unit and shreds it when the run ends. That suits
            a token you use a handful of times a year and would rather not have
            sitting in sops. Point this at a secret only if you want the export
            to run unattended.
          '';
        };
        outputPath = lib.mkOption {
          type = lib.types.path;
          default = "${cfg.stateDir}/exports/discord-dce";
          defaultText = lib.literalExpression ''"''${cfg.stateDir}/exports/discord-dce"'';
          description = "Directory DCE writes one JSON per channel into.";
        };
        scope = lib.mkOption {
          type = lib.types.enum [ "dm" "all" ];
          default = "dm";
          description = ''
            "dm" exports direct and group DMs only (`exportdm`); "all" adds
            every guild channel you can read (`exportall`), which is very much
            larger and mostly public chatter.
          '';
        };
      };
    };

    sources.claude = {
      enable = lib.mkEnableOption "the Claude.ai data export ingester (manual trigger)";
      path = lib.mkOption {
        type = lib.types.path;
        example = "/var/lib/slop-trove/exports/claude";
        description = "Path to the unzipped Claude.ai data export root.";
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

    # Install the pgvector extension as the postgres superuser (pgvector is
    # not "trusted", so the app role cannot CREATE EXTENSION itself). This
    # must run on postgresql-setup.service — that's the unit that creates the
    # database via ensureDatabases; postgresql.service's postStart runs before
    # the database exists.
    systemd.services.postgresql-setup.postStart = lib.mkIf cfg.database.createLocally (
      lib.mkAfter ''
        ${config.services.postgresql.package}/bin/psql -d "${cfg.database.name}" \
          -tAc "CREATE EXTENSION IF NOT EXISTS vector"
      ''
    );

    # ── MCP search server ────────────────────────────────────────────────
    systemd.services.slop-trove-mcp = {
      description = "slop-trove MCP search server";
      wantedBy = [ "multi-user.target" ];
      # postgresql-setup creates the role/db and (via postStart above) the
      # pgvector extension; init-db in ExecStartPre needs all of that.
      after = [ "network-online.target" ]
        ++ lib.optionals cfg.database.createLocally [ "postgresql.service" "postgresql-setup.service" ];
      wants = [ "network-online.target" ];
      requires = lib.optionals cfg.database.createLocally [ "postgresql.service" "postgresql-setup.service" ];
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

    # ── Source ingesters (oneshot: `systemctl start slop-trove-ingest-<name>`) ─
    systemd.services.slop-trove-ingest-discord = lib.mkIf cfg.sources.discord.enable {
      description = "slop-trove: ingest the Discord export";
      after = lib.optionals cfg.database.createLocally [ "postgresql.service" "postgresql-setup.service" ];
      requires = lib.optionals cfg.database.createLocally [ "postgresql.service" "postgresql-setup.service" ];
      environment = commonEnv;
      serviceConfig = {
        Type = "oneshot";
        User = cfg.user;
        Group = cfg.group;
        ExecStart = "${lib.getExe pkg} ingest --source discord --path ${cfg.sources.discord.path}";
      };
    };

    # `slop-trove-discord-export` is the intended entry point; the unit exists
    # so the run survives the session that started it.
    environment.systemPackages =
      lib.mkIf (cfg.sources.discord.enable && exportCfg.enable) [ discordExportWrapper ];

    # Acquisition, not ingestion: writes JSON to disk, nothing touches the DB.
    # Deliberately no timer -- a full export is hours of rate-limited paging
    # against a ToS-sensitive endpoint, which is not something to run on a
    # schedule behind your back.
    systemd.services.slop-trove-export-discord =
      lib.mkIf (cfg.sources.discord.enable && exportCfg.enable) {
        description = "slop-trove: export Discord history (DiscordChatExporter)";
        after = [ "network-online.target" ];
        wants = [ "network-online.target" ];
        # No wantedBy: started by hand, never at boot.
        serviceConfig = {
          Type = "oneshot";
          User = cfg.user;
          Group = cfg.group;
          LoadCredential = [ "discord-token:${tokenSource}" ];
          ExecStart = discordExportScript;
          # '+' runs as root, which is what can remove a root-owned staging
          # file. Runs however the unit ends -- success, failure or abort -- so
          # a handed-over token never outlives its run.
          ExecStopPost = lib.mkIf (exportCfg.tokenFile == null) [
            "+${pkgs.coreutils}/bin/rm -f ${runtimeTokenPath}"
          ];
          # A first full export is hours of rate-limited paging; don't let the
          # default start timeout shoot it partway through.
          TimeoutStartSec = "infinity";
          StateDirectory = "slop-trove";
        };
      };

    # The cutover half of a re-export: DCE chunks hash-key differently from
    # GDPR ones, so without this the two coexist and your own messages land in
    # the index twice.
    systemd.services.slop-trove-purge-discord =
      lib.mkIf (cfg.sources.discord.enable && exportCfg.enable) {
        description = "slop-trove: drop every stored discord record";
        after = lib.optionals cfg.database.createLocally [ "postgresql.service" "postgresql-setup.service" ];
        requires = lib.optionals cfg.database.createLocally [ "postgresql.service" "postgresql-setup.service" ];
        environment = commonEnv;
        serviceConfig = {
          Type = "oneshot";
          User = cfg.user;
          Group = cfg.group;
          ExecStart = "${lib.getExe pkg} purge --source discord --yes";
        };
      };

    systemd.services.slop-trove-ingest-claude = lib.mkIf cfg.sources.claude.enable {
      description = "slop-trove: ingest the Claude.ai data export";
      after = lib.optionals cfg.database.createLocally [ "postgresql.service" "postgresql-setup.service" ];
      requires = lib.optionals cfg.database.createLocally [ "postgresql.service" "postgresql-setup.service" ];
      environment = commonEnv;
      serviceConfig = {
        Type = "oneshot";
        User = cfg.user;
        Group = cfg.group;
        ExecStart = "${lib.getExe pkg} ingest --source claude --path ${cfg.sources.claude.path}";
      };
    };
  };
}
