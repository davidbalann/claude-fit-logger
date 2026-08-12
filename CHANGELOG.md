# Changelog

All notable changes to Claude Fit Logger, in chronological order.
Format loosely follows [Keep a Changelog](https://keepachangelog.com/).

## [0.7] - 2026-08-12
### Added
- Full documentation set: `install.tex`, `architecture.tex`,
  `troubleshooting.tex`, `security.tex`, `api_reference.tex`,
  `operations.tex`.
- `README.md` as the repo entry point.

## [0.6] - 2026-08-12
### Changed
- **Migrated from the legacy Google Fit REST API to the Google Health API
  v4.** Root cause: Google Fit and the current Google Health app (relaunched
  from Fitbit) are separate, disconnected backends — writes to Fit's API
  were succeeding but never appeared in the app actually being checked.
- Replaced `fit_client.py` with `health_client.py`, targeting
  `health.googleapis.com/v4`.
- Replaced Fit's nanosecond-timestamp-range delete mechanism with
  resource-name-based deletion (`batchDelete`).
- Updated `db.py` schema: dropped `fit_start_ns`/`fit_end_ns`, added
  `datapoint_name`.
- Requested new OAuth scopes (`googlehealth.nutrition.readonly`,
  `googlehealth.nutrition.writeonly`); re-issued refresh token.

### Fixed
- Missing `startUtcOffset`/`endUtcOffset` on nutrition writes, which
  silently defaulted to UTC+0 and misplaced entries onto the wrong local
  day/time.
- Confirmed correct `Nutrient` enum names (`PROTEIN`, `DIETARY_FIBER`,
  `SUGAR`) empirically via a probe script, after partial/incorrect
  documentation coverage.

### Security
- Multiple credential exposure incidents during rotation and screenshots
  (Google OAuth secret, refresh token, Neon password); all rotated.
- One GitHub push protection incident (secrets present in a local commit);
  resolved by resetting local git history and force-pushing a clean state.

## [0.5] - 2026-08-11
### Added
- Updated the `calorie-counter` skill to call `log_food` / `undo_entry` /
  `remove_entry` instead of performing its own separate nutrition lookup,
  eliminating duplicate/conflicting lookups between the skill and server.

### Fixed
- Google OAuth refresh tokens expiring after 7 days (a limitation of
  "Testing" publishing status for unverified apps) — published the OAuth
  consent screen to Production, requiring a privacy policy
  (`PRIVACY.md`, hosted via GitHub Pages).

## [0.4] - 2026-08-11
### Added
- Minimal OAuth 2.1 authorization-server shim (`oauth.py`): discovery
  metadata, Dynamic Client Registration, auto-approved authorize step,
  and token issuance — required because Claude's custom connector UI
  mandates OAuth even for single-user, API-key-based servers.
- API-key authentication middleware, later superseded by the OAuth shim's
  Bearer token flow.

### Fixed
- Google Fit `dataStreamId` format mismatch (required an exact
  deterministic pattern including the Google Cloud project number).
- Google Fit "mismatched value count" error — `com.google.nutrition`
  requires exactly three point values (nutrients map, meal type, food
  item), not the nutrients map alone.

## [0.3] - 2026-08-11
### Added
- Core server implementation against the legacy Google Fit API:
  `main.py` (four MCP tools), `db.py` (entry tracking), `fit_client.py`
  (Fit OAuth + REST calls), `nutrition_lookup.py` (USDA FoodData Central +
  Open Food Facts, with fallback signalling).
- Deployed to Render (free tier) and connected as a custom Claude
  connector.

### Changed
- Switched hosting plan from Fly.io to Render after discovering Fly.io no
  longer offers a permanent free tier.
- Switched entry-tracking storage from a Fly.io persistent volume (SQLite)
  to Neon (free Postgres), since Render's free tier has no persistent disk.

## [0.2] - 2026-08-11
### Added
- Google Cloud project, OAuth consent screen (Testing mode), and Desktop
  OAuth client for the one-time consent flow.
- Initial architecture plan: MCP server, nutrition lookup with Claude
  fallback, entry tracking for undo/remove, shared-secret API auth.

## [0.1] - 2026-08-11
### Added
- Project scoping: decided on a self-hosted MCP server (rather than
  MyFitnessPal, which has no usable third-party write API) as the
  integration path from Claude to a health-tracking backend.
