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
| `ingest/discord.py` | Parse a Discord GDPR package *or* a DiscordChatExporter dump → chunked `Record`s |
| `embed.py` | Text embeddings via an Ollama `/api/embed` endpoint |
| `db.py` | pgvector schema, idempotent upsert, cosine search |
| `mcp_server.py` | HTTP MCP server exposing `search_personal_data` |
| `cli.py` | `slop-trove {init-db,ingest,purge,query,serve}` |
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
    path = "/var/lib/slop-trove/exports/discord-dce";
    export = {
      enable = true;                                  # see "Two Discord exports"
      tokenFile = config.sops.secrets."discord-user-token".path;
      scope = "dm";                                   # or "all" to add guilds
    };
  };
};

# expose it to Hermes
services.hermes-agent.settings.mcp_servers.slop_trove = {
  url = "http://127.0.0.1:9120/mcp";
  tools.include = [ "search_personal_data" ];
};
```

Ingest is a manual oneshot: `systemctl start slop-trove-ingest-discord`.

## Two Discord exports

`sources.discord.path` auto-detects which of two layouts it is pointed at, and
they are **not** equivalent:

| | GDPR data package | DiscordChatExporter (DCE) |
|---|---|---|
| How you get it | Request it from Discord, wait, download a ZIP | `systemctl start slop-trove-export-discord` |
| Whose messages | **Only yours** — no author field, because there is nothing to disambiguate | Everyone's, with author names |
| Chunk reads as | A monologue with no context | `"<author>: <text>"` dialogue turns |
| Caveat | Half the conversation is simply absent | Uses a **user** token, i.e. a self-bot under Discord's ToS |

The GDPR path is kept as the no-token fallback. DCE is what you want if you
care about what other people said to you.

Because chunk boundaries (and therefore content hashes) differ between the two,
DCE records are hash-keyed under a `dce:` prefix and will happily **coexist**
with GDPR rows for the same channel — indexing your own messages twice. When
switching a channel's history from one to the other, purge first:

```sh
slop-trove purge --source discord          # dry run, prints the row count
slop-trove purge --source discord --yes    # actually delete
systemctl start slop-trove-ingest-discord
```

> During active dev, override the input to your local checkout instead of
> pushing each change:
> `nixos-rebuild ... --override-input slop-trove path:/home/phonkd/git/slop-trove`

## Roadmap

- **v0 (this):** Discord, text, full spine end-to-end.
- **v1:** email + purchases; incremental ingest on a timer; source/time filters.
- **v2:** photos — multimodal embeddings in a separate vector space.
