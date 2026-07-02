# slop-trove

Personal-data embedding + semantic search. Ingest your own exports (Discord
first; email / purchases / photos later), embed them, store vectors in
Postgres + pgvector, and expose semantic search as an MCP tool the
[Hermes agent](https://github.com/NousResearch/hermes-agent) can call.

This repo is **"the thing"** — code, packaging, and the NixOS module. Your
`nixconfig` is **"the config"** — it consumes this as a flake input and sets
host/secret/path values. (See *Wiring into nixconfig* below.)

## Components

| Path | Role |
|------|------|
| `ingest/discord.py` | Parse a Discord GDPR package → chunked `Record`s |
| `embed.py` | Text embeddings via an Ollama `/api/embed` endpoint |
| `db.py` | pgvector schema, idempotent upsert, cosine search |
| `mcp_server.py` | HTTP MCP server exposing `search_personal_data` |
| `cli.py` | `slop-trove {init-db,ingest,query,serve}` |
| `nixos-module.nix` | `services.slop-trove.*` |

## Local dev

```sh
nix develop                       # python + deps + postgres
# point at a local/remote Ollama and Postgres:
export SLOP_TROVE_DB_URL="dbname=slop-trove"
export SLOP_TROVE_EMBED_ENDPOINT="http://blac:11434"

slop-trove init-db
slop-trove ingest --source discord --path /path/to/discord-package
slop-trove query "that argument about coffee setups"
slop-trove serve                  # MCP server on 127.0.0.1:9120
```

Pull the embedding model once on the Ollama host: `ollama pull nomic-embed-text`.

## Wiring into nixconfig

```nix
# flake.nix
inputs.slop-trove.url = "github:phonkd/slop-trove";
# import inputs.slop-trove.nixosModules.default via your builder

# modules/hosts/204-agent.nix
services.slop-trove = {
  enable = true;
  database.createLocally = true;            # local Postgres + pgvector
  embedding.endpoint = "http://blac:11434"; # reuse the blac Ollama
  mcp.port = 9120;
  sources.discord = {
    enable = true;
    path = "/var/lib/slop-trove/exports/discord";
  };
};

# expose it to Hermes
services.hermes-agent.settings.mcp_servers.slop_trove = {
  url = "http://127.0.0.1:9120/mcp";
  tools.include = [ "search_personal_data" ];
};
```

Ingest is a manual oneshot: `systemctl start slop-trove-ingest-discord`.

> During active dev, override the input to your local checkout instead of
> pushing each change:
> `nixos-rebuild ... --override-input slop-trove path:/home/phonkd/git/slop-trove`

## Roadmap

- **v0 (this):** Discord, text, full spine end-to-end.
- **v1:** email + purchases; incremental ingest on a timer; source/time filters.
- **v2:** photos — multimodal embeddings in a separate vector space.
