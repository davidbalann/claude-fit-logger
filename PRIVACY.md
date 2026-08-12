# Privacy Policy — claude-fit-logger

_Last updated: August 2026_

**claude-fit-logger** is a personal, single-user tool built by David Balan.
It is not a public product or service, and it is not available for other
users to sign up for or connect their accounts to.

## What this tool does

claude-fit-logger reads food/drink descriptions supplied by the account
owner via Claude, looks up or estimates their nutritional content, and
writes that nutrition data to the account owner's own Google Fit account
using the Google Fitness API. It can also read and delete nutrition entries
it previously wrote, in order to support correcting or undoing a log entry.

## What data is accessed

- **Google Fit nutrition data** (`fitness.nutrition.read`,
  `fitness.nutrition.write`): used solely to write, read back, and delete
  nutrition entries logged by the account owner. No other Google Fit data
  types (activity, sleep, location, heart rate, etc.) are accessed.

## What data is stored

- A record of logged food items (name, quantity, nutrition values, and
  timestamp) is stored in a private database, used only to support undo/
  remove functionality and daily totals.
- No data is sold, shared, or made available to any third party.
- No data is used for advertising or analytics.

## Who can access this data

Only the account owner (David Balann). This tool is not distributed,
published, or offered to any other user, and no other Google account can
authorize or use it.

## Third-party services used

- **Google Fitness API** — to store/retrieve nutrition data, per the scopes
  above.
- **USDA FoodData Central** and **Open Food Facts** — public nutrition
  databases queried by food name to determine nutritional values. No
  personal data is sent to these services beyond the food name being
  looked up.

## Contact

For questions about this tool or its data handling, contact the developer
directly through the associated GitHub repository.
