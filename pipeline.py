"""One check = scrape -> diff against DB -> enrich new posts -> pricing -> alert."""
import os

from . import alerts, db, enrich, pricing, scraper

HYDRATE_FIRST = int(os.getenv("HYDRATE_FIRST", "12"))   # first run: only fully process the newest N
HYDRATE_MAX = int(os.getenv("HYDRATE_MAX", "40"))       # later runs: cap per check


def run_check(domain_raw, slack_url=None, email=None):
    domain = scraper.normalize_domain(domain_raw)
    known = db.known_urls(domain)
    first = not known
    s = scraper.scan(domain)
    res = {"domain": domain, "status": s["status"], "first_run": first, "new_posts": [],
           "pricing": None, "enrichment_warning": None, "alerts": [], "tracked_before": len(known)}

    new = {u: m for u, m in s["cands"].items() if u not in known}
    ordered = sorted(new.items(), key=lambda kv: kv[1].get("published") or "", reverse=True)
    batch = ordered[:HYDRATE_FIRST if first else HYDRATE_MAX]
    parsed = scraper.hydrate(s["f"], [u for u, _ in batch]) if batch else {}

    reported = []
    for u, m in batch:
        p = parsed.get(u) or {}
        reported.append({"url": u, "title": p.get("title") or m.get("title") or u,
                         "published": p.get("published") or m.get("published"),
                         "description": p.get("description") or m.get("description"),
                         "meta_keywords": p.get("meta_keywords"), "source": m.get("source")})
    reported.sort(key=lambda p: p["published"] or "", reverse=True)
    warn = enrich.enrich(reported) if reported else None

    rows = [{**{k: p.get(k) for k in ("url", "title", "published", "description", "keyword", "volume", "source")},
             "hydrated": True} for p in reported]
    rows += [{"url": u, "title": m.get("title"), "published": m.get("published"),
              "description": m.get("description"), "keyword": None, "volume": None,
              "source": m.get("source"), "hydrated": False} for u, m in ordered[len(batch):]]
    db.add_posts(domain, rows)

    try:
        res["pricing"] = pricing.check(s["f"], s["base"], s["soup"], domain)
    except Exception as e:  # pricing must never break the post check
        res["pricing"] = {"status": "error", "note": str(e)}
    price_changed = res["pricing"].get("status") == "changed"

    res["new_posts"], res["enrichment_warning"] = reported, warn
    res["indexed_total"] = len(known) + len(new)
    if s["status"] == "ok":
        if first:
            res["message"] = f"Initial scan: indexed {len(new)} posts. Showing the {len(reported)} most recent."
        elif reported:
            res["message"] = f"{len(reported)} new post{'s' if len(reported) != 1 else ''} since the last check."
        else:
            res["message"] = f"No new posts since the last check ({len(known)} tracked)."
    else:
        res["message"] = s["message"]

    if reported or price_changed:
        res["alerts"] = alerts.dispatch(domain, reported, res["pricing"] if price_changed else None,
                                        first, len(new), warn, slack_url, email)
    db.touch_domain(domain, s["status"])
    return res
