---
name: calorie-counter
description: Track a running daily total of calories, protein, carbs, fat, fiber, and sugar for foods and ingredients the user logs in chat, and sync each entry to Google Health via the Fit Logger connector. Use this skill whenever the user invokes /calorie-counter, or uses natural phrasing like "log this", "add this to my calories", "track my macros", mentions eating/drinking something and wants it counted, or uploads a photo of a nutrition label or food item to log. Especially relevant for Costco Canada grocery items and Canadian restaurant meals. Trigger this even if the user doesn't explicitly say "calorie counter". Any mention of logging food, tracking macros, or asking "what's my total so far" during an active food-logging conversation should use this skill.
---

# Calorie Counter

Maintains a running, in-chat log of food/drink entries and their nutritional
totals, and writes each one to Google Health via the Fit Logger MCP connector
(tools: `log_food`, `undo_entry`, `remove_entry`, `get_totals`). The chat
table and Google Health are kept in sync: every logged item goes to both.

## Prerequisite

This skill requires the Fit Logger connector to be active. If its tools
aren't available, fall back to chat-only behavior (log in the table, skip the
tool calls) and tell the user Fit Logger isn't connected.

## Timezone

Every tool call needs a `tz` argument: an IANA timezone name. Use
`America/Toronto` unless the user has said otherwise. This determines what
counts as "today" for totals, undo, and remove, and which meal a log falls
under when no meal is given.

## Triggering

- Slash command: `/calorie-counter`
- Natural language: "log this", "add to my calories", "track my macros",
  "I had X for lunch", "what's my total so far", or any mention of food/drink
  the user wants counted.
- A photo of a nutrition label, Costco product, or restaurant receipt/menu
  item, sent with intent to log it.

## First invocation in a chat

If this is the first food-related message in the conversation, briefly
introduce the workflow before starting the table:

> "Tracking your calories today, synced to Google Health. Just tell me what
> you eat/drink (or upload a label photo), and I'll keep a running table. Say
> 'remove [item]', 'undo', or give a row number to fix mistakes."

Then start with an empty log.

## Core workflow

For every food/drink item mentioned, in order:

### 1. Filter zero-calorie drinks
Water, black coffee, and plain tea (no sugar/milk/cream) are **not logged at
all**. Skip silently, don't add a row, don't call any tool.

### 2. Resolve ambiguity before logging
- **Vague generic item** (e.g. "I had a banana", no brand/size given): ask
  the user to confirm quantity/brand before logging. Don't guess.
- **Restaurant/product item with multiple variants** where nutrition differs
  meaningfully between them (size, flavor, or format, e.g. Tim Hortons'
  "Bacon Egg Cheese" ranges ~270 to 450 cal across wrap/English
  muffin/bagel/biscuit) and the variant isn't specified: ask which one before
  logging. Don't average across variants or silently pick one.
- Otherwise, proceed. The user may give you: a Costco product name, a
  fraction of a package ("half the bag"), an exact gram amount, an uploaded
  label photo, or a restaurant menu item name. Convert everything to a
  quantity in grams before calling `log_food`.

### 3. Log the item via the tool

`log_food` has three ways to be called. Never do your own web search or
nutrition lookup before the first one.

**A. Search.** Call `log_food(item_name, quantity_g, tz)`. The server does
NOT pick a food for you. It returns one of:

- `status: "candidates"`: up to 5 options, USDA first, then Open Food Facts.
  Each has `id`, `name`, `source`, `data_type`, and `per_100g` values.
  Nothing has been logged yet. Choose the candidate that best matches what the
  user actually ate, then go to B.
- `status: "not_found"`: no usable match, or the lookup sources were down.
  Go to C.

How to choose a candidate:
- Whole or generic foods: prefer USDA `Foundation` or `SR Legacy` entries.
- Packaged or branded items: prefer the entry whose name and brand match.
- Match the state the user weighed it in (raw vs cooked, with skin vs
  without). Raw and cooked values per 100 g differ a lot.
- Reject candidates whose `per_100g.calories` is implausible for that food.
- If two or more candidates are plausible but meaningfully different, ask the
  user which one. If none fits, go to C.

**B. Log a candidate.** Call `log_food` again with the same
`item_name`/`quantity_g`/`tz` plus `candidate_id` (copied exactly from the
chosen candidate). The server scales its per-100g values to `quantity_g` and
logs it. A `status: "logged"` result has `source` of `"usda"` or `"off"` and
`estimated: false`. Use the returned `nutrients` and `matched_name` for the
table row. No flag needed. If this call returns `not_found`, the source went
down between the two calls: go to C.

**C. Log your own numbers.** Look the item up via an official source
(Costco.ca, restaurant nutrition page/PDF) or web search, or read values
directly off an uploaded label photo. Then call `log_food` with the same
`item_name`/`quantity_g`/`tz` plus `calories` (required) and whichever of
`protein_g`/`carbs_g`/`fat_g`/`fiber_g`/`sugar_g` you found or estimated. This
logs with `source: "claude_estimate"` and `estimated: true`, so add the flag
line (see Flags below).

If a nutrition label photo was uploaded, skip the search and go straight to
C with the values read off the label.

**Meal type.** `log_food` takes an optional `meal_type`: `BREAKFAST`,
`LUNCH`, `DINNER`, or `SNACK`. Pass it when the user says which meal this is
("for lunch", "breakfast was...", "new meal: dinner"), and keep passing it
for the items that follow in that meal. Otherwise leave it out and the server
picks one from the local hour. Never pass anything else.

**Errors.** If `log_food` returns a tool error, nothing was logged. Do not add
a row to the chat table, do not retry the same call, and show the user the
error text (it contains Google's own message). Tell them the item was not
saved.

### 4. Sanity-check
If the resulting calorie value seems implausible for the stated portion
(e.g. wildly high/low vs. typical values for that food), flag it in a short
note rather than silently logging a bad number. This applies whether the
number came from the server's lookup or your own estimate.

### 5. Update the chat table and reprint
After each successful `log_food` call (`status: "logged"`), add one row to the
chat log table using the nutrients the tool returned, and reprint both tables
in full (see Table format). The tool's response also includes `today_totals`.
Use that directly instead of re-summing the table yourself; the two should
always agree since they're both backed by the same log.

Do not summarize or skip reprinting on multi-item messages; each item gets
its own row and the table is reprinted after all items in one message are
processed.

## Table format

**Log table**: exactly these 4 columns, values to 1 decimal place, quantity
in grams:

| Item | Qty (g) | Cal | Protein (g) |
|---|---|---|---|
| Costco Kirkland Almonds | 30.0 | 173.0 | 6.3 |
| Chipotle Chicken Bowl | 450.0 | 630.0 | 45.0 |

**Totals block**: separate small table below the log, values to 1 decimal
place for grams/calories, whole-number percentages. Use the `today_totals`
returned by the tools rather than recomputing by hand:

| Metric | Value |
|---|---|
| Total Calories | 803.0 |
| Total Protein | 51.3 g |
| Total Carbs | 62.1 g |
| Total Fat | 38.4 g |
| Total Fiber | 9.2 g |
| Total Sugar | 14.0 g |
| % Calories from Protein | 26% |
| % Calories from Carbs | 31% |
| % Calories from Fat | 43% |

Percentage formulas (protein/carbs/fat should sum to ~100%, rounding aside):
```
protein_pct = round(protein_g * 4 / total_calories * 100)
carbs_pct   = round(carbs_g   * 4 / total_calories * 100)
fat_pct     = round(fat_g     * 9 / total_calories * 100)
```
Fiber and sugar are informational only. They are not part of the percentage
split (they're subsets of total carbs, not additional calories).

No commentary, no one-liner summaries. Output is the two tables plus any
flags (see below). Keep it clean and direct.

## Flags

Add a single flagged line directly below the tables when:
- The item was logged with your own numbers (path C, `source:
  "claude_estimate"`), e.g.:
  > ⚠️ *Estimated: "Kirkland Protein Bar", no match in USDA/Open Food Facts, used web-sourced values.*
- The sanity check triggered.

Don't add a flag line for entries logged from a candidate (`source: "usda"`
or `"off"`).

## Corrections

Support all of the following, applied to the most recent matching entry
unless a row number is given:
- `remove [item name]`: call `remove_entry(item_name, tz)`, then delete
  that row from the chat table
- `undo`: call `undo_entry(tz)`, then delete the most recently added row
  from the chat table
- A row number: remove that specific chat-table row, and also call
  `remove_entry` with that row's item name so Google Health stays in sync

After any correction, use the `today_totals` returned by the tool call to
recompute and reprint both tables in full. Don't just delete the row locally
without also calling the tool, or the chat table and Google Health will drift
out of sync.

If a correction tool call returns `status: "not_found"` or `"empty"`, tell
the user there was nothing matching to remove rather than silently editing
the chat table.

## Meal grouping in the chat table

Default: one continuous running table for the whole day, no meal grouping.

If the user explicitly says something like "start a new meal" or "new meal:
lunch", insert a divider row in the log table (e.g. `| **Lunch** | | | |`)
before the next entries, but keep the same 4-column structure. Do not add a
separate meal column. The divider is chat-table only; the meal itself reaches
Google Health through `meal_type` (see step 3).

## Notes on sourcing (Canadian context)

These apply only to path C (your own numbers):
- Groceries: assume Costco Canada (Costco.ca) unless the user names another
  retailer.
- Restaurants: assume Canadian menus/nutrition data (e.g. .ca sites).
  Canadian restaurant nutrition can differ from US versions of the same
  chain, so don't reuse US figures without checking.
