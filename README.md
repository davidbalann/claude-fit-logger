# Claude Fit Logger

Log food and drink to Google Health by just talking to Claude — from your
phone, no manual entry. Claude looks up nutrition automatically (USDA /
Open Food Facts, falling back to its own estimate when nothing matches),
writes it to Google Health, and keeps a running daily table in chat with
support for undo and corrections.

## How it works

```
You (phone) → Claude (chat + skill) → MCP server (Render)
                                          ├─ Nutrition lookup (USDA / Open Food Facts)
                                          ├─ Entry log (Neon Postgres) — powers undo/remove
                                          └─ Google Health API — the actual food log
```

Full breakdown, including a diagram, in [`docs/architecture.tex`](docs/architecture.tex).

## Status

Working end-to-end: logging, automatic nutrition lookup with manual
fallback, undo, remove, and running totals all sync correctly to the
Google Health app. Single-user by design — see
[`docs/security.tex`](docs/security.tex) for why that matters before
extending this to anyone else.

## Repository structure

```
claude-fit-logger/
├── docs/              # Full documentation (see below)
├── server/            # Deployable MCP server (Render root directory)
│   ├── main.py         # Tool definitions + auth
│   ├── db.py            # Neon-backed entry log
│   ├── health_client.py # Google Health API client
│   ├── nutrition_lookup.py
│   ├── oauth.py          # OAuth 2.1 shim for Claude's connector requirement
│   └── requirements.txt
├── PRIVACY.md          # Required for Google OAuth production publishing
└── .gitignore
```

## Documentation

| Document | Covers |
|---|---|
| [`docs/install.tex`](docs/install.tex) | Full setup from scratch: Google Cloud, Neon, Render, Claude connector |
| [`docs/architecture.tex`](docs/architecture.tex) | System design, data flow, component breakdown |
| [`docs/api_reference.tex`](docs/api_reference.tex) | The four MCP tools: parameters and response shapes |
| [`docs/operations.tex`](docs/operations.tex) | Deploying, rolling back, reconnecting the connector |
| [`docs/troubleshooting.tex`](docs/troubleshooting.tex) | Known issues and fixes, including the Fit → Health migration |
| [`docs/security.tex`](docs/security.tex) | Credential inventory and rotation procedures |

## Tech stack

Python (`fastmcp`, Starlette, uvicorn) · Render (hosting) · Neon (Postgres)
· Google Health API v4 · USDA FoodData Central · Open Food Facts

## Setup

Start with [`docs/install.tex`](docs/install.tex) — it walks through every
account, credential, and deployment step needed to stand this up from
nothing.

## Notes

- Requires the `calorie-counter` skill (configured in Claude's Skill
  settings, not part of this repo) to drive the conversational side.
- Render's free tier spins down after inactivity — first request after a
  while may be slow to wake up.
