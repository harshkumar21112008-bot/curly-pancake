"""Own scraper: homepage + sitemaps + RSS/Atom + blog listing pages -> candidate post URLs."""
import html as htmlmod
import ipaddress
import json
import os
import re
import socket
import time
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import httpx
from bs4 import BeautifulSoup
from dateutil import parser as dparse

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
        self.deadline = time.time() + float(os.getenv("RUN_BUDGET_SECONDS", "75"))
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
