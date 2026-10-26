# Competitor Monitor

Enter a competitor's domain. The tool scrapes its blog with its own crawler, stores every post URL, flags only what is new on later runs, enriches new posts with a target keyword and search volume, tracks pricing-page changes, and sends a Slack/email alert.

## How it works

1. **Scrape (no third-party API).** `app/scraper.py` combines four sources: RSS/Atom feeds, `robots.txt` and `sitemap.xml` (including sitemap indexes), links on the homepage, and blog listing pages (`/blog`, `/resources`, `/news`, ...). Each new post page is fetched for title, publish date (OpenGraph, JSON-LD, `<time>`), and meta description.
2. **Detect new content.** Every discovered URL is stored (`posts` table, unique on domain + normalized URL). A run reports only URLs not already stored. First run is a baseline: all URLs are indexed, the 12 newest are processed and alerted.
3. **Pricing.** Finds the pricing page, extracts price strings, and diffs them against the last snapshot. A price change triggers an alert.
4. **Enrich.** For each new post, up to 8 candidate keywords are generated (meta keywords, URL slug, title, title n-grams). One batched call to DataForSEO (Google Ads search volume) or Keywords Everywhere returns volumes; the highest-volume candidate becomes the target keyword. This is a heuristic, not a ranking-based keyword.
5. **Alert.** Slack incoming webhook (server default or per run in the UI) and/or SMTP email.

## Failure behavior

| Situation | Result |
|---|---|
| No blog/feed/sitemap | `status: no_blog`, clear message, no alert |
| Bot protection (403/429/Cloudflare challenge) | `status: blocked`, clear message |
| Domain unreachable / invalid / private IP | `unreachable` or HTTP 400 |
| Keyword API key invalid or missing | Posts and alert still go out; keyword is a best guess, volume `n/a`, warning shown |
| Slack/email fails | Error is returned per channel; posts are still saved |
| Pricing page is JavaScript-rendered | Reported as "no prices found", never breaks the post check |

Limits: no JavaScript rendering, so sites that render the post list client-side and have no sitemap or feed will not be discovered. A run has a 75 s time budget (`RUN_BUDGET_SECONDS`).

## Run locally

```bash
pip install -r requirements.txt
cp .env.example .env      # add keys
uvicorn app.main:app --reload
python -m tests.test_flow # offline test: run twice, new post, price change
```

Free keyword data: DataForSEO trial credentials, or set `DATAFORSEO_BASE=https://sandbox.dataforseo.com` for free dummy volumes while testing.

## Deploy

Storage must persist between runs, so use a hosted Postgres. A free Supabase project works: copy its **connection pooler** string (port 6543) into `DATABASE_URL`. Then deploy the Dockerfile on Render, Railway or Fly.io and set the env vars from `.env.example`. Tables are created on startup.

Set `AUTO_CHECK_HOURS=24` to re-check every tracked domain automatically.

---

## Two ways to deploy this same repo

| Target | Entry point | Notes |
|---|---|---|
| **Vercel** | `index.py` (single-file backend) + `public/index.html` | Import the repo, leave Root Directory blank. `pyproject.toml` sets `entrypoint = "index:app"`. Daily re-check via Vercel Cron (`CRON_SECRET`). |
| **Render / Railway / Fly (Docker)** | `app/` package (multi-file backend) + `Dockerfile` | `render.yaml` is included. |

Both use the same logic. `index.py` is the `app/` package merged into one file. Either way `DATABASE_URL` should be a Postgres URL (Supabase pooler, port 6543) so history survives restarts. Run the offline test with `python -m tests.test_flow`.
