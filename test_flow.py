"""Offline end-to-end test with a mocked site: run twice, then add a post and change a price."""
import os, tempfile
os.environ["ALLOW_PRIVATE"] = "1"
os.environ["DATABASE_URL"] = f"sqlite:///{tempfile.mkdtemp()}/t.db"
import httpx
from app import pipeline, scraper, enrich

POSTS = {"/blog/how-to-track-competitors-seo": "2026-10-01", "/blog/pricing-strategy-for-saas": "2026-10-10"}
PRICE = {"v": "$29/mo"}

def page(title, date=None, desc="desc"):
    d = f'<meta property="article:published_time" content="{date}">' if date else ""
    return f'<html><head><title>{title} | Acme</title><meta name="description" content="{desc}">{d}</head><body><h1>{title}</h1></body></html>'

def handler(req):
    p = req.url.path
    if p == "/": return httpx.Response(200, text='<a href="/blog">Blog</a><a href="/pricing">Pricing</a>', headers={"content-type": "text/html"})
    if p == "/sitemap.xml":
        urls = "".join(f"<url><loc>https://acme-test.com{k}</loc><lastmod>{v}</lastmod></url>" for k, v in POSTS.items())
        return httpx.Response(200, text=f"<urlset>{urls}</urlset>")
    if p == "/blog": return httpx.Response(200, text="".join(f'<a href="{k}">Read: {k}</a>' for k in POSTS), headers={"content-type": "text/html"})
    if p in POSTS: return httpx.Response(200, text=page(p.split("/")[-1].replace("-", " ").title(), POSTS[p]), headers={"content-type": "text/html"})
    if p == "/pricing": return httpx.Response(200, text=f"<main>Pro {PRICE['v']}</main>", headers={"content-type": "text/html"})
    return httpx.Response(404)

_init = scraper.Fetcher.__init__
def patched(self):
    _init(self); self.c = httpx.Client(transport=httpx.MockTransport(handler), headers=scraper.HEADERS)
scraper.Fetcher.__init__ = patched

r1 = pipeline.run_check("https://www.acme-test.com/", None, None)
assert r1["first_run"] and len(r1["new_posts"]) == 2, r1
assert "invalid" in r1["enrichment_warning"] or "No keyword API" in r1["enrichment_warning"]
r2 = pipeline.run_check("acme-test.com")
assert not r2["first_run"] and r2["new_posts"] == [], r2           # second run: nothing repeated
POSTS["/blog/new-seo-playbook-2026"] = "2026-10-22"; PRICE["v"] = "$39/mo"
r3 = pipeline.run_check("acme-test.com")
assert [p["url"] for p in r3["new_posts"]] == ["https://acme-test.com/blog/new-seo-playbook-2026"], r3
assert r3["pricing"]["status"] == "changed" and r3["pricing"]["added"] == ["$39/mo"], r3["pricing"]
print("keyword:", r3["new_posts"][0]["keyword"], "| alerts:", r3["alerts"])
# no blog / blocked
POSTS.clear()
print("OK", pipeline.run_check("other-test.com")["status"])
