"""Target keyword + search volume. Providers: DataForSEO (trial/sandbox) or Keywords Everywhere."""
import os
import re
from urllib.parse import urlparse

import httpx

STOP = set("a an the and or of to in on for with from by at as is are be your you how what why when "
           "which who this that it its our we vs do does can i my".split())


class AuthError(Exception): ...
class NoProvider(Exception): ...


def _clean_title(t):
    parts = re.split(r"\s+[|–—-]\s+", t or "")
    if len(parts) > 1 and len(parts[-1].split()) <= 3:
        parts = parts[:-1]  # drop trailing "| Brand"
    return " ".join(parts)


def candidates(p):
    """Candidate target keywords, best guess first."""
    out = [k.strip().lower() for k in (p.get("meta_keywords") or "").split(",")
           if 1 < len(k.split()) <= 5][:2]
    slug = urlparse(p["url"]).path.rstrip("/").split("/")[-1]
    slug = re.sub(r"[-_]+", " ", re.sub(r"\b(19|20)\d{2}\b", "", slug)).strip()
    if slug and len(slug.split()) <= 6:
        out.append(slug.lower())
    words = re.sub(r"[^\w\s'-]", " ", _clean_title(p.get("title")).lower()).split()
    if 2 <= len(words) <= 6:
        out.append(" ".join(words))
    core = [w for w in words if w not in STOP]
    for n in (3, 2):
        out += [" ".join(core[i:i + n]) for i in range(len(core) - n + 1)]
    seen = []
    for k in out:
        if 3 < len(k) <= 80 and len(k.split()) <= 10 and k not in seen:
            seen.append(k)
    return seen[:8]


def _dataforseo(kws):
    login, pw = os.getenv("DATAFORSEO_LOGIN"), os.getenv("DATAFORSEO_PASSWORD")
    base = os.getenv("DATAFORSEO_BASE", "https://api.dataforseo.com")
    r = httpx.post(f"{base}/v3/keywords_data/google_ads/search_volume/live", auth=(login, pw), timeout=60,
                   json=[{"keywords": kws, "location_code": int(os.getenv("KW_LOCATION_CODE", "2840")),
                          "language_code": os.getenv("KW_LANG", "en")}])
    if r.status_code in (401, 403):
        raise AuthError()
    j = r.json()
    if j.get("status_code") in (40100, 40101, 40102):
        raise AuthError()
    task = (j.get("tasks") or [{}])[0]
    if task.get("status_code") != 20000:
        raise RuntimeError(task.get("status_message") or j.get("status_message") or "DataForSEO error")
    return {x["keyword"].lower(): x.get("search_volume") for x in task.get("result") or []}


def _keywords_everywhere(kws):
    r = httpx.post("https://api.keywordseverywhere.com/v1/get_keyword_data", timeout=60,
                   headers={"Authorization": f"Bearer {os.getenv('KEYWORDS_EVERYWHERE_KEY')}",
                            "Accept": "application/json"},
                   data={"country": os.getenv("KW_COUNTRY", "us"), "currency": "USD",
                         "dataSource": "gkp", "kw[]": kws[:100]})
    if r.status_code in (401, 403):
        raise AuthError()
    if r.status_code != 200:
        raise RuntimeError(f"Keywords Everywhere HTTP {r.status_code}")
    return {x["keyword"].lower(): x.get("vol") for x in r.json().get("data", [])}


def _lookup(kws):
    if os.getenv("DATAFORSEO_LOGIN") and os.getenv("DATAFORSEO_PASSWORD"):
        return _dataforseo(kws)
    if os.getenv("KEYWORDS_EVERYWHERE_KEY"):
        return _keywords_everywhere(kws)
    raise NoProvider()


def enrich(posts):
    """Set post['keyword'] and post['volume'] in place. Never raises; returns a warning or None."""
    for p in posts:
        p["_c"] = candidates(p)
    allk = list(dict.fromkeys(k for p in posts for k in p["_c"]))[:100]
    vols, warn = {}, None
    if allk:
        try:
            vols = _lookup(allk)
        except AuthError:
            warn = "Keyword API rejected the credentials (invalid key) - search volume unavailable."
        except NoProvider:
            warn = "No keyword API configured - search volume unavailable."
        except Exception as e:
            warn = f"Keyword API error ({type(e).__name__}: {e}) - search volume unavailable."
    for p in posts:
        c = p.pop("_c")
        best = max(c, key=lambda k: vols.get(k) if vols.get(k) is not None else -1, default=None)
        p["keyword"], p["volume"] = best, (vols.get(best) if best else None)
    return warn
