# koha-triage

Semantic triage tool for the [Koha ILS](https://koha-community.org/) community Bugzilla.

Harvests bugs from [bugs.koha-community.org](https://bugs.koha-community.org/bugzilla3/), provides semantic search with AI-generated verdicts, per-bug fix recommendations using Koha coding guidelines, and code generation with downloadable `git format-patch` output for the `git bz` workflow.

## Features

- **Harvest** — Pulls bugs and comments from Koha Bugzilla REST API (incremental via `last_change_time`)
- **Semantic search** — BAAI/bge-small-en-v1.5 embeddings with cosine similarity ranking
- **AI classification** — Claude-generated verdicts per search result (has_patch, resolved_fixed, likely_duplicate, etc.)
- **Bug grouping** — Manually cluster related bugs
- **AI recommendations** — Per-bug fix recommendations using bundled Koha coding guidelines + handbook
- **Code generation** — Claude generates fixes, displayed as git-style diffs
- **Patch export** — Download `git format-patch` output ready for `git bz attach`
- **Google OAuth** — Optional domain-restricted authentication
- **Web dashboard** — Browse, filter, search, and triage bugs in a dark-themed UI

## Quick start

```bash
cp .env.example .env
# edit .env — add at minimum KOHA_TRIAGE_ANTHROPIC_API_KEY

# Install and run
pip install -e .
koha-triage harvest          # fetch bugs from Bugzilla
koha-triage embed            # compute embeddings
koha-triage serve            # start web UI on http://localhost:8000

# Or with Docker
docker compose up -d
docker compose run --rm triage harvest
docker compose run --rm triage embed
```

## Patch workflow

Koha does not accept pull requests. The workflow is:

1. Generate a code fix on any bug detail page
2. Download the `.patch` file
3. `git am bug_XXXXX.patch`
4. Review, test, adjust
5. `git bz attach XXXXX HEAD`

## License

MIT
