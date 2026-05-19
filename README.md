# Marketplace Deal Finder

An automated deal-hunting tool that scrapes Facebook Marketplace and Kijiji,
scores listings against a reference price catalog, and sends Telegram alerts
when a device is priced below your target buy threshold.

Built for flipping phones, tablets, MacBooks, and smartwatches locally in Windsor, ON.

---

## Features

- Scrapes **Facebook Marketplace** and **Kijiji** on a configurable interval
- Fuzzy-matches listing titles and descriptions to identify device model,
  storage, and condition from unstructured text
- Applies **OCR on listing photos** to recover details missing from the text
- Scores each listing against a CSV pricing catalog with condition-based deductions
- Sends **Telegram push notifications** with deal details, suggested buy price,
  and estimated sell price
- Filters noise via duplicate URL suppression, blocked keywords, and a
  configurable minimum discount threshold
- Supports free listings with extended detail-page parsing
- Handles MacBook validation separately with stricter rules
  (rejects pre-2020 and Intel models by default)

---

## Tech Stack

- Python 3.11+
- Playwright (browser automation for Facebook)
- rapidfuzz (fuzzy string matching)
- openpyxl (pricing workbook parsing)
- Telegram Bot API (alerts)

---

## Project Structure
```
deal_finder.py          # Main script: scraping, matching, scoring, alerting
generate_prices_csv.py  # Rebuilds prices.csv from the pricing workbook
config.example.json     # Config template — copy to config.json and fill in
prices.csv              # Pricing catalog with fair price, max buy, deductions
requirements.txt        # Python dependencies
```

---

## Installation

```bash
# 1. Clone the repo
git clone https://github.com/abdi-1612/abdullah-khan-scraper-tool.git
cd abdullah-khan-scraper-tool

# 2. Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt
playwright install chromium
```

---

```markdown
## Configuration

Copy the example config and fill in your values:

```bash
cp config.example.json config.json
```

Then open and edit it:

```bash
nano config.json
```

Navigate to the `telegram` section and fill in your details:

```json
"telegram": {
    "bot_token": "YOUR_BOT_TOKEN_HERE",
    "chat_id": "YOUR_CHAT_ID_HERE"
}
```

Save with `Ctrl+X` → `Y` → `Enter`.

**To get your bot token:** Message @BotFather on Telegram and create a new bot.

**To get your chat ID:**
1. Paste this in your browser (replace with your token):
   `https://api.telegram.org/botYOUR_TOKEN/getUpdates`
2. Send any message to your bot on Telegram
3. Refresh the URL — your chat ID appears as `"id"` in the response

**Other settings you may want to change in `config.json`:**

| Setting | Default | Description |
|---|---|---|
| `poll_minutes` | 45 | How often the tool scans for new listings |
| `min_price` | 0 | Ignore listings below this price |
| `max_price` | 5000 | Ignore listings above this price |
| `blocked_words` | see file | Listings containing these words are skipped |
| `scroll_rounds` | 6 | How many times Facebook Marketplace is scrolled per scan |
| `pages_per_search` | 2 | How many Kijiji pages are scraped per search |
| `headless` | true | Set to `false` if Facebook blocks the scraper |


---

## Usage

```bash
# Log into Facebook (required once)
python3 deal_finder.py --facebook-login

# Test Telegram alerts
python3 deal_finder.py --test-telegram

# Run a single dry-run scan (no alerts sent)
python3 deal_finder.py --once --dry-run

# Run a live single scan
python3 deal_finder.py --once

# Run the continuous watcher (scans every poll_minutes)
python3 deal_finder.py --watch

# Clear dedupe history to re-alert old listings
python3 deal_finder.py --clear-seen

# Mark a listing as already messaged so it never alerts again
python3 deal_finder.py --mark-messaged "LISTING_URL"
```

```bash
nano config.json
```

```json
"poll_minutes": 45
```
```
---

## Pricing Logic

Pricing uses three thresholds derived from a base USD reference price:

| Threshold | Formula |
|---|---|
| Sell Price | Base × 1.4 |
| Max Buy | Base × 1.12 |
| Alert Ceiling | Max Buy × negotiation buffer |

A listing alerts if it is at or below Max Buy.
Listings above Max Buy but within the negotiation ceiling still surface,
labeled as negotiation targets.

---

## Notes

- `config.json` is gitignored — never commit your real bot token
- `seen_listings.json` and `state.json` are auto-generated at runtime
- If Facebook blocks the scraper, set `"headless": false` in config and re-run the login flow
- If the pricing workbook changes, re-run `generate_prices_csv.py`

---
