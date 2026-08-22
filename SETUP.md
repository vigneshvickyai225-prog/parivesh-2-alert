# PARIVESH 2.0 free cloud monitor

This uses GitHub Actions + Playwright/Chromium + your existing Telegram bot.
It monitors the actual PARIVESH 2.0 EC route:
https://parivesh.nic.in/#/ec
States: Tamil Nadu, Karnataka, Telangana.

## Setup
1. Create a GitHub account and a NEW PUBLIC repository, e.g. `parivesh-2-alert`.
2. Upload all files from this package, preserving `.github/workflows/monitor.yml`.
3. Repository -> Settings -> Secrets and variables -> Actions -> New repository secret:
   - TELEGRAM_BOT_TOKEN = your BotFather token
   - TELEGRAM_CHAT_ID = your chat ID
4. Go to Actions -> PARIVESH 2.0 Telegram Monitor -> Run workflow.
5. Confirm the workflow completes and Telegram receives an alert when a new matching item is found.
6. Scheduled monitoring then runs every 15 minutes. Your laptop can be OFF.

## Important
Do NOT put Telegram credentials in code or files committed to GitHub.
Public repositories are recommended for the free setup.
GitHub currently documents free/unlimited standard GitHub-hosted runners for public repositories. Scheduled workflows support cron and the documented shortest interval is 5 minutes. GitHub also notes public scheduled workflows can be disabled after 60 days without repository activity.

## PARIVESH caveat
PARIVESH 2.0 is a JavaScript SPA. This monitor uses Chromium to render the live `/#/ec` route. If the portal changes its UI, the scraper may need updating.
