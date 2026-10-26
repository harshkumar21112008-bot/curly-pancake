"""Pricing page tracking: snapshot visible prices, diff against the last snapshot."""
import hashlib
import json
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from . import db
from .scraper import BlockedError, FetchError

PRICE_RE = re.compile(
    r"(?:₹|\$|€|£|Rs\.?\s?|USD\s?|INR\s?)\s?\d[\d,]*(?:\.\d+)?"
    r"(?:\s?(?:/|per)\s?(?:mo|month|yr|year|user|seat|member)\w*)?", re.I)


def _find_url(f, base, soup):
    cands = []
    if soup:
        for a in soup.find_all("a", href=True):
            txt, path = a.get_text(" ", strip=True).lower(), urlparse(a["href"]).path.lower()
            if txt in ("pricing", "plans", "plans & pricing", "plans and pricing", "prices") \
                    or re.search(r"/(pricing|plans|prices)/?$", path):
                cands.append(urljoin(base, a["href"]))
    cands += [urljoin(base, p) for p in ("/pricing", "/plans")]
    for u in dict.fromkeys(cands):
        try:
            return u, f.get(u)
        except (FetchError, BlockedError):
            continue
    return None, None


def check(f, base, soup, domain):
    url, r = _find_url(f, base, soup)
    if not r:
        return {"status": "not_found", "note": "No public pricing page found."}
    s = BeautifulSoup(r.text, "lxml")
    for t in s(["script", "style", "nav", "footer", "header", "noscript"]):
        t.decompose()
    text = re.sub(r"\s+", " ", s.get_text(" ", strip=True))
    prices = sorted({re.sub(r"\s+", "", m.group(0)) for m in PRICE_RE.finditer(text)})
    h = hashlib.sha256(text.encode()).hexdigest()
    out = {"url": str(r.url), "prices": prices, "added": [], "removed": []}
    if not prices:
        out["note"] = "No prices found on the page (it may be rendered by JavaScript or 'contact sales')."
    prev = db.last_pricing(domain)
    if not prev:
        db.save_pricing(domain, out["url"], h, json.dumps(prices))
        return {**out, "status": "baseline"}
    old = json.loads(prev["prices"])
    out["added"], out["removed"] = sorted(set(prices) - set(old)), sorted(set(old) - set(prices))
    if out["added"] or out["removed"]:
        db.save_pricing(domain, out["url"], h, json.dumps(prices))
        return {**out, "status": "changed"}
    if h != prev["text_hash"]:
        db.save_pricing(domain, out["url"], h, json.dumps(prices))
        return {**out, "status": "content_changed"}
    return {**out, "status": "unchanged"}
