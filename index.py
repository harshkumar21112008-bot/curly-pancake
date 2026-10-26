"""Competitor Monitor - single-file FastAPI app for Vercel (api/index.py).
Sections: storage, scraper, keyword enrichment, pricing, alerts, pipeline, API."""
import asyncio, datetime as dt, hashlib, html as htmlmod, ipaddress, json, os, re, smtplib, socket, tempfile, time
from concurrent.futures import ThreadPoolExecutor
from email.message import EmailMessage
from functools import lru_cache
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import httpx
from bs4 import BeautifulSoup
from dateutil import parser as dparse
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import (Boolean, Column, DateTime, Integer, MetaData, String, Table, Text,
                        UniqueConstraint, create_engine, desc, insert, select, update)
from sqlalchemy.pool import NullPool


# ===== storage / scraper / enrich / pricing / alerts / pipeline =====
"""Storage. SQLite locally, Postgres (e.g. Supabase) in production — same code via SQLAlchemy."""



def _url():
    u = os.getenv("DATABASE_URL") or ("sqlite:////tmp/monitor.db" if os.getenv("VERCEL") else "sqlite:///data/monitor.db")
    for p in ("postgres://", "postgresql://"):
        if u.startswith(p):
            u = "postgresql+psycopg2://" + u[len(p):]
    if u.startswith("sqlite:///") and "/" in u[10:]:
        os.makedirs(os.path.dirname(u[10:]), exist_ok=True)
    return u


engine = create_engine(_url(), poolclass=NullPool) if os.getenv('VERCEL') else create_engine(_url(), pool_pre_ping=True)
md = MetaData()

T_domains = Table("domains", md,
                Column("domain", String(255), primary_key=True),
                Column("last_checked", DateTime), Column("last_status", String(30)))

T_posts = Table("posts", md,
              Column("id", Integer, primary_key=True),
              Column("domain", String(255), index=True),
              Column("url", String(1000)), Column("title", Text),
              Column("published", String(40)), Column("description", Text),
              Column("keyword", String(300)), Column("volume", Integer),
              Column("source", String(20)),
              Column("hydrated", Boolean, default=False),
              Column("first_seen", DateTime),
              UniqueConstraint("domain", "url", name="uq_domain_url"))

T_pricing = Table("pricing", md,
                Column("id", Integer, primary_key=True),
                Column("domain", String(255), index=True),
                Column("url", Text), Column("fetched", DateTime),
                Column("text_hash", String(64)), Column("prices", Text))

md.create_all(engine)


def now():
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def known_urls(domain):
    with engine.connect() as c:
        return {r[0] for r in c.execute(select(T_posts.c.url).where(T_posts.c.domain == domain))}


def add_posts(domain, rows):
    if rows:
        with engine.begin() as c:
            c.execute(insert(T_posts), [{**r, "domain": domain, "first_seen": now()} for r in rows])


def recent_posts(domain, limit=50):
    q = (select(T_posts).where(T_posts.c.domain == domain, T_posts.c.hydrated.is_(True))
         .order_by(desc(T_posts.c.first_seen), desc(T_posts.c.published)).limit(limit))
    with engine.connect() as c:
        return [dict(r._mapping) for r in c.execute(q)]


def touch_domain(domain, status):
    with engine.begin() as c:
        n = c.execute(update(T_domains).where(T_domains.c.domain == domain)
                      .values(last_checked=now(), last_status=status)).rowcount
        if not n:
            c.execute(insert(T_domains).values(domain=domain, last_checked=now(), last_status=status))


def list_domains():
    with engine.connect() as c:
        return [dict(r._mapping) for r in c.execute(select(T_domains).order_by(T_domains.c.domain))]


def last_pricing(domain):
    q = select(T_pricing).where(T_pricing.c.domain == domain).order_by(desc(T_pricing.c.id)).limit(1)
    with engine.connect() as c:
        r = c.execute(q).first()
        return dict(r._mapping) if r else None


def save_pricing(domain, url, text_hash, prices_json):
    with engine.begin() as c:
        c.execute(insert(T_pricing).values(domain=domain, url=url, fetched=now(),
                                         text_hash=text_hash, prices=prices_json))


"""Own scraper: homepage + sitemaps + RSS/Atom + blog listing pages -> candidate post URLs."""


UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0 Safari/537.36 CompetitorMonitor/1.0")
HEADERS = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9",
           "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}
BLOG_SEGS = {"blog", "blogs", "resources", "resource", "articles", "article", "news", "insights",
             "learn", "post", "posts", "stories", "guides", "library", "journal", "academy", "updates"}
SKIP_SEGS = {"tag", "tags", "category", "categories", "author", "authors", "page", "topic",
             "topics", "feed", "search", "wp-content", "wp-json", "amp"}
PROBES = ["/blog", "/resources", "/news", "/articles", "/insights", "/learn"]
BLOCK_MARKERS = ("just a moment...", "attention required! | cloudflare", "cf-browser-verification",
                 "px-captcha", "request unsuccessful. incapsula")


class InvalidDomain(ValueError): ...
class FetchError(Exception): ...
class BlockedError(Exception): ...


def normalize_domain(raw):
    d = (raw or "").strip().lower()
    d = re.sub(r"^[a-z]+://", "", d).split("/")[0].split("?")[0].split(":")[0]
    if d.startswith("www."):
        d = d[4:]
    if not re.fullmatch(r"([a-z0-9-]+\.)+[a-z]{2,}", d):
        raise InvalidDomain(f"'{raw}' is not a valid domain (try example.com)")
    return d


@lru_cache(maxsize=512)
def _public_host(host):
    """SSRF guard: refuse private / internal addresses."""
    if os.getenv("ALLOW_PRIVATE") == "1":
        return True
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        raise FetchError(f"DNS lookup failed for {host}")
    for i in infos:
        ip = ipaddress.ip_address(i[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise InvalidDomain("Private or internal addresses are not allowed")
    return True


class Fetcher:
    def __init__(self):
        self.c = httpx.Client(headers=HEADERS, timeout=12, follow_redirects=False)
        self.deadline = time.time() + float(os.getenv("RUN_BUDGET_SECONDS", "45"))
        self.blocked = False

    def get(self, url):
        if time.time() > self.deadline:
            raise FetchError("time budget exceeded")
        try:
            for _ in range(6):  # manual redirects so every hop is SSRF-checked
                _public_host(urlparse(url).hostname or "")
                r = self.c.get(url)
                loc = r.headers.get("location")
                if r.status_code in (301, 302, 303, 307, 308) and loc:
                    url = urljoin(str(r.url), loc)
                    continue
                break
        except httpx.HTTPError as e:
            raise FetchError(f"{type(e).__name__}: {e}") from e
        head = r.text[:4000].lower() if "html" in r.headers.get("content-type", "") else ""
        if r.status_code in (401, 403, 429) or any(m in head for m in BLOCK_MARKERS):
            self.blocked = True
            raise BlockedError(f"HTTP {r.status_code} - the site is blocking automated requests")
        if r.status_code >= 400:
            raise FetchError(f"HTTP {r.status_code}")
        return r


# ---------- helpers ----------
TRACK = re.compile(r"^(utm_|fbclid|gclid|mc_|ref$)", re.I)


def norm(u):
    p = urlparse(u.strip())
    q = [(k, v) for k, v in parse_qsl(p.query) if not TRACK.match(k)]
    return urlunparse(("https", p.netloc.lower(), p.path.rstrip("/") or "/", "", urlencode(q), ""))


def to_iso(s):
    try:
        return dparse.parse(s).date().isoformat()
    except Exception:
        return None


def same_site(u, domain):
    h = (urlparse(u).hostname or "").lower()
    return h == domain or h.endswith("." + domain)


def is_post_url(u, domain):
    if not same_site(u, domain):
        return False
    p = urlparse(u)
    segs = [s for s in p.path.split("/") if s]
    if not segs or segs[-1].isdigit() or "-" not in segs[-1]:
        return False
    if any(s.lower() in SKIP_SEGS for s in segs) or re.search(
            r"\.(jpe?g|png|gif|svg|pdf|webp|xml|css|js|zip)$", p.path, re.I):
        return False
    if (p.hostname or "").startswith(("blog.", "news.", "insights.")):
        return True
    return any(s.lower() in BLOG_SEGS and len(segs) - i >= 2 for i, s in enumerate(segs))


def anchors(soup, base):
    for a in soup.find_all("a", href=True):
        h = a["href"].strip()
        if not h.startswith(("#", "mailto:", "tel:", "javascript:")):
            yield norm(urljoin(base, h)), a.get_text(" ", strip=True)


# ---------- discovery sources ----------
def sitemap_urls(f, base):
    seeds = []
    try:
        seeds += re.findall(r"(?im)^sitemap:\s*(\S+)", f.get(urljoin(base, "/robots.txt")).text)
    except (FetchError, BlockedError):
        pass
    seeds += [urljoin(base, p) for p in ("/sitemap.xml", "/sitemap_index.xml", "/wp-sitemap.xml")]
    queue, seen, out, fetched = list(dict.fromkeys(seeds)), set(), {}, 0
    while queue and fetched < 25:
        sm = queue.pop(0)
        if sm in seen or sm.endswith(".gz"):
            continue
        seen.add(sm)
        try:
            xml = f.get(sm).text
        except (FetchError, BlockedError):
            continue
        fetched += 1
        locs = lambda s: [htmlmod.unescape(x) for x in re.findall(r"<loc>\s*(.*?)\s*</loc>", s, re.S)]
        if "<sitemapindex" in xml:
            kids = locs(xml)
            pref = [k for k in kids if re.search(r"post|blog|article|news|insight|resource", k, re.I)]
            queue = (pref or kids) + queue
        else:
            for blk in re.findall(r"<url>(.*?)</url>", xml, re.S):
                loc = locs(blk)
                if loc:
                    lm = re.search(r"<lastmod>\s*(.*?)\s*</lastmod>", blk, re.S)
                    out[loc[0]] = lm.group(1) if lm else None
    return out


def feed_items(f, base, soup):
    cands = []
    if soup:
        for l in soup.find_all("link", href=True):
            if any(t in (l.get("type") or "") for t in ("rss", "atom")):
                cands.append(urljoin(base, l["href"]))
    cands += [urljoin(base, p) for p in ("/feed", "/feed/", "/rss.xml", "/blog/feed", "/blog/feed/",
                                          "/blog/rss.xml", "/atom.xml", "/index.xml")]
    for fu in dict.fromkeys(cands):
        if "comments" in fu:
            continue
        try:
            x = f.get(fu).text
        except (FetchError, BlockedError):
            continue
        if not re.search(r"<(rss|feed|rdf)", x[:3000]):
            continue
        s = BeautifulSoup(x, "xml")
        items = {}
        for it in s.find_all(["item", "entry"]):
            l = it.find("link")
            link = (l.get("href") or l.get_text(strip=True)) if l else None
            if not link:
                continue
            t, d, ds = it.find("title"), it.find(["pubDate", "published", "updated", "date"]), it.find(["description", "summary"])
            items[norm(link)] = {
                "title": t.get_text(" ", strip=True) if t else None,
                "published": to_iso(d.get_text(strip=True)) if d else None,
                "description": BeautifulSoup(ds.get_text(), "html.parser").get_text(" ", strip=True)[:300] if ds else None}
        if items:
            return items
    return {}


def listing_pages(base, domain, soup):
    pages = []
    if soup:
        for u, _ in anchors(soup, base):
            p = urlparse(u)
            segs = [s for s in p.path.split("/") if s]
            if same_site(u, domain) and ((len(segs) == 1 and segs[0].lower() in BLOG_SEGS) or
                                          ((p.hostname or "").startswith("blog.") and not segs)):
                pages.append(u)
    pages += [norm(urljoin(base, p)) for p in PROBES]
    return list(dict.fromkeys(pages))[:8]


def crawl_listing(f, url, domain):
    """Return {post_url: anchor_text} from a listing page plus its next page."""
    out, nxt = {}, url
    for _ in range(2):
        try:
            r = f.get(nxt)
        except (FetchError, BlockedError):
            break
        if urlparse(str(r.url)).path.rstrip("/") == "":  # redirected to homepage -> no such listing
            break
        s = BeautifulSoup(r.text, "lxml")
        for u, txt in anchors(s, str(r.url)):
            if is_post_url(u, domain):
                out.setdefault(u, txt if len(txt) > 12 else None)
        n = s.find("a", rel="next") or s.find("link", rel="next")
        if not n or not n.get("href"):
            break
        nxt = urljoin(str(r.url), n["href"])
    return out


# ---------- post metadata ----------
def _ld_date(s):
    for tag in s.find_all("script", type="application/ld+json"):
        try:
            stack = [json.loads(tag.string or "")]
        except ValueError:
            continue
        while stack:
            d = stack.pop()
            if isinstance(d, list):
                stack += d
            elif isinstance(d, dict):
                if d.get("datePublished"):
                    return str(d["datePublished"])
                stack += list(d.values())
    return None


def parse_post(text):
    s = BeautifulSoup(text, "lxml")

    def meta(*names):
        for n in names:
            t = (s.find("meta", attrs={"property": n}) or s.find("meta", attrs={"name": n})
                 or s.find("meta", attrs={"itemprop": n}))
            if t and t.get("content", "").strip():
                return t["content"].strip()

    title = meta("og:title", "twitter:title") or (s.h1.get_text(" ", strip=True) if s.h1 else None) \
        or (s.title.get_text(strip=True) if s.title else None)
    date = meta("article:published_time", "og:article:published_time", "datePublished", "date",
                "publish_date", "pubdate", "DC.date.issued") or _ld_date(s)
    if not date:
        t = s.find("time", attrs={"datetime": True})
        date = t["datetime"] if t else None
    return {"title": title, "published": to_iso(date) if date else None,
            "description": meta("description", "og:description", "twitter:description"),
            "meta_keywords": meta("keywords", "news_keywords")}


def hydrate(f, urls):
    def one(u):
        try:
            return u, parse_post(f.get(u).text)
        except (FetchError, BlockedError):
            return u, None
    with ThreadPoolExecutor(6) as ex:
        return dict(ex.map(one, urls))


# ---------- main entry ----------
def scan(domain):
    f, home, base = Fetcher(), None, f"https://{domain}/"
    err = None
    for host in (domain, "www." + domain):
        try:
            home = f.get(f"https://{host}/")
            base = str(home.url)
            break
        except BlockedError as e:
            err = e
            break
        except FetchError as e:
            err = e
    if home is None and not f.blocked:
        return {"status": "unreachable", "message": f"Could not reach {domain}: {err}",
                "cands": {}, "f": f, "soup": None, "base": base}

    soup = BeautifulSoup(home.text, "lxml") if home is not None else None
    cands = {}

    def merge(u, **m):
        cur = cands.setdefault(norm(u), {})
        for k, v in m.items():
            if v and not cur.get(k):
                cur[k] = v

    for u, m in feed_items(f, base, soup).items():
        if same_site(u, domain):
            merge(u, **m, source="feed")
    for u, lm in sitemap_urls(f, base).items():
        if is_post_url(u, domain):
            merge(u, published=to_iso(lm) if lm else None, source="sitemap")
    if soup:
        for u, txt in anchors(soup, base):
            if is_post_url(u, domain):
                merge(u, title=txt if len(txt) > 12 else None, source="html")
    for page in listing_pages(base, domain, soup):
        for u, txt in crawl_listing(f, page, domain).items():
            merge(u, title=txt, source="html")

    if cands:
        status, msg = "ok", f"Found {len(cands)} post URLs"
    elif f.blocked:
        status, msg = "blocked", ("This site blocks automated requests (bot protection). "
                                  "Nothing could be scraped without a headless browser or proxy.")
    else:
        status, msg = "no_blog", "No blog, feed or article sitemap found on this domain."
    return {"status": status, "message": msg, "cands": cands, "f": f, "soup": soup, "base": base}


"""Target keyword + search volume. Providers: DataForSEO (trial/sandbox) or Keywords Everywhere."""


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


def enrich_posts(posts):
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


"""Pricing page tracking: snapshot visible prices, diff against the last snapshot."""



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


def price_check(f, base, soup, domain):
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
    prev = last_pricing(domain)
    if not prev:
        save_pricing(domain, out["url"], h, json.dumps(prices))
        return {**out, "status": "baseline"}
    old = json.loads(prev["prices"])
    out["added"], out["removed"] = sorted(set(prices) - set(old)), sorted(set(old) - set(prices))
    if out["added"] or out["removed"]:
        save_pricing(domain, out["url"], h, json.dumps(prices))
        return {**out, "status": "changed"}
    if h != prev["text_hash"]:
        save_pricing(domain, out["url"], h, json.dumps(prices))
        return {**out, "status": "content_changed"}
    return {**out, "status": "unchanged"}


"""Slack webhook + email alerts."""



def build_alert(domain, posts, pricing, first, total, warn, slack=True):
    link = (lambda u, t: f"<{u}|{t.replace('|', '/').replace('<', '').replace('>', '')}>") if slack \
        else (lambda u, t: f"{t}\n    {u}")
    bold = (lambda s: f"*{s}*") if slack else (lambda s: s)
    L = []
    if posts:
        if first:
            L.append(f"{bold(domain)} - initial scan: {total} posts indexed. Latest {len(posts)}:")
        else:
            L.append(f"{bold(domain)} published {len(posts)} new post{'s' if len(posts) != 1 else ''} "
                     f"since the last check:")
        for p in posts[:15]:
            vol = f"{p['volume']:,}/mo" if p.get("volume") is not None else "n/a"
            L.append(f"• {link(p['url'], p['title'])} ({p.get('published') or 'date n/a'})\n"
                     f"    keyword: {p.get('keyword') or 'n/a'} | search volume: {vol}")
        if len(posts) > 15:
            L.append(f"...and {len(posts) - 15} more")
    if pricing:
        L.append(f"\n{bold('Pricing change detected')}: {pricing['url']}")
        if pricing["added"]:
            L.append("  added: " + ", ".join(pricing["added"]))
        if pricing["removed"]:
            L.append("  removed: " + ", ".join(pricing["removed"]))
    if warn:
        L.append(f"\nNote: {warn}")
    return "\n".join(L)


def _slack(url, text):
    r = httpx.post(url, json={"text": text}, timeout=10)
    if r.status_code != 200:
        raise RuntimeError(f"Slack returned {r.status_code}: {r.text[:80]}")


def _email(to, subject, body):
    host = os.getenv("SMTP_HOST")
    if not host:
        raise RuntimeError("SMTP is not configured on the server")
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = os.getenv("SMTP_FROM") or os.getenv("SMTP_USER"), to, subject
    m.set_content(body)
    port = int(os.getenv("SMTP_PORT", "587"))
    with (smtplib.SMTP_SSL(host, port, timeout=15) if port == 465 else smtplib.SMTP(host, port, timeout=15)) as s:
        if port != 465:
            s.starttls()
        if os.getenv("SMTP_USER"):
            s.login(os.getenv("SMTP_USER"), os.getenv("SMTP_PASS", ""))
        s.send_message(m)


def dispatch_alert(domain, posts, pricing, first, total, warn, slack_url=None, email=None):
    results = []
    slack_url = slack_url or os.getenv("SLACK_WEBHOOK_URL")
    if slack_url:
        try:
            if not slack_url.startswith("https://hooks.slack.com/"):
                raise RuntimeError("Not a Slack incoming-webhook URL")
            _slack(slack_url, build_alert(domain, posts, pricing, first, total, warn, True))
            results.append({"channel": "slack", "ok": True})
        except Exception as e:
            results.append({"channel": "slack", "ok": False, "error": str(e)})
    if email:
        try:
            n = len(posts)
            _email(email, f"{domain}: {n} new post{'s' if n != 1 else ''}" if posts else f"{domain}: pricing changed",
                   build_alert(domain, posts, pricing, first, total, warn, False))
            results.append({"channel": "email", "ok": True})
        except Exception as e:
            results.append({"channel": "email", "ok": False, "error": str(e)})
    if not results:
        results.append({"channel": "none", "ok": False, "error": "No Slack webhook or email configured"})
    return results


"""One check = scrape -> diff against DB -> enrich new posts -> pricing -> alert."""


HYDRATE_FIRST = int(os.getenv("HYDRATE_FIRST", "12"))   # first run: only fully process the newest N
HYDRATE_MAX = int(os.getenv("HYDRATE_MAX", "40"))       # later runs: cap per check


def run_check(domain_raw, slack_url=None, email=None):
    domain = normalize_domain(domain_raw)
    known = known_urls(domain)
    first = not known
    s = scan(domain)
    res = {"domain": domain, "status": s["status"], "first_run": first, "new_posts": [],
           "pricing": None, "enrichment_warning": None, "alerts": [], "tracked_before": len(known)}

    new = {u: m for u, m in s["cands"].items() if u not in known}
    ordered = sorted(new.items(), key=lambda kv: kv[1].get("published") or "", reverse=True)
    batch = ordered[:HYDRATE_FIRST if first else HYDRATE_MAX]
    parsed = hydrate(s["f"], [u for u, _ in batch]) if batch else {}

    reported = []
    for u, m in batch:
        p = parsed.get(u) or {}
        reported.append({"url": u, "title": p.get("title") or m.get("title") or u,
                         "published": p.get("published") or m.get("published"),
                         "description": p.get("description") or m.get("description"),
                         "meta_keywords": p.get("meta_keywords"), "source": m.get("source")})
    reported.sort(key=lambda p: p["published"] or "", reverse=True)
    warn = enrich_posts(reported) if reported else None

    rows = [{**{k: p.get(k) for k in ("url", "title", "published", "description", "keyword", "volume", "source")},
             "hydrated": True} for p in reported]
    rows += [{"url": u, "title": m.get("title"), "published": m.get("published"),
              "description": m.get("description"), "keyword": None, "volume": None,
              "source": m.get("source"), "hydrated": False} for u, m in ordered[len(batch):]]
    add_posts(domain, rows)

    try:
        res["pricing"] = price_check(s["f"], s["base"], s["soup"], domain)
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
        res["alerts"] = dispatch_alert(domain, reported, res["pricing"] if price_changed else None,
                                        first, len(new), warn, slack_url, email)
    touch_domain(domain, s["status"])
    return res


# ===== API =====





app = FastAPI(title="Competitor Monitor")

@app.exception_handler(Exception)
async def on_error(request, exc):
    return JSONResponse({"detail": f"{type(exc).__name__}: {exc}"}, status_code=500)



class CheckReq(BaseModel):
    domain: str
    slack_webhook: str | None = None
    email: str | None = None


@app.post("/api/check")
def api_check(req: CheckReq):
    try:
        return run_check(req.domain, (req.slack_webhook or "").strip() or None,
                                  (req.email or "").strip() or None)
    except InvalidDomain as e:
        raise HTTPException(400, str(e))


@app.get("/api/posts")
def api_posts(domain: str):
    return recent_posts(normalize_domain(domain))


@app.get("/api/domains")
def api_domains():
    return list_domains()


@app.get("/api/health")
def health():
    return {"ok": True}



@app.get("/api/cron")
def api_cron(authorization: str | None = Header(None)):
    """Called daily by Vercel Cron (set CRON_SECRET). Re-checks tracked domains."""
    secret = os.getenv("CRON_SECRET")
    if not secret or authorization != f"Bearer {secret}":
        raise HTTPException(401, "Unauthorized")
    out = {}
    for d in list_domains():
        try:
            r = run_check(d["domain"])
            out[d["domain"]] = r["message"]
        except Exception as e:
            out[d["domain"]] = f"failed: {e}"
    return out
