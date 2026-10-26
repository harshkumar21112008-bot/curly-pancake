"""Storage. SQLite locally, Postgres (e.g. Supabase) in production — same code via SQLAlchemy."""
import datetime as dt
import os

from sqlalchemy import (Boolean, Column, DateTime, Integer, MetaData, String, Table, Text,
                        UniqueConstraint, create_engine, desc, insert, select, update)


def _url():
    u = os.getenv("DATABASE_URL") or "sqlite:///data/monitor.db"
    for p in ("postgres://", "postgresql://"):
        if u.startswith(p):
            u = "postgresql+psycopg2://" + u[len(p):]
    if u.startswith("sqlite:///") and "/" in u[10:]:
        os.makedirs(os.path.dirname(u[10:]), exist_ok=True)
    return u


engine = create_engine(_url(), pool_pre_ping=True)
md = MetaData()

domains = Table("domains", md,
                Column("domain", String(255), primary_key=True),
                Column("last_checked", DateTime), Column("last_status", String(30)))

posts = Table("posts", md,
              Column("id", Integer, primary_key=True),
              Column("domain", String(255), index=True),
              Column("url", String(1000)), Column("title", Text),
              Column("published", String(40)), Column("description", Text),
              Column("keyword", String(300)), Column("volume", Integer),
              Column("source", String(20)),
              Column("hydrated", Boolean, default=False),
              Column("first_seen", DateTime),
              UniqueConstraint("domain", "url", name="uq_domain_url"))

pricing = Table("pricing", md,
                Column("id", Integer, primary_key=True),
                Column("domain", String(255), index=True),
                Column("url", Text), Column("fetched", DateTime),
                Column("text_hash", String(64)), Column("prices", Text))

md.create_all(engine)


def now():
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def known_urls(domain):
    with engine.connect() as c:
        return {r[0] for r in c.execute(select(posts.c.url).where(posts.c.domain == domain))}


def add_posts(domain, rows):
    if rows:
        with engine.begin() as c:
            c.execute(insert(posts), [{**r, "domain": domain, "first_seen": now()} for r in rows])


def recent_posts(domain, limit=50):
    q = (select(posts).where(posts.c.domain == domain, posts.c.hydrated.is_(True))
         .order_by(desc(posts.c.first_seen), desc(posts.c.published)).limit(limit))
    with engine.connect() as c:
        return [dict(r._mapping) for r in c.execute(q)]


def touch_domain(domain, status):
    with engine.begin() as c:
        n = c.execute(update(domains).where(domains.c.domain == domain)
                      .values(last_checked=now(), last_status=status)).rowcount
        if not n:
            c.execute(insert(domains).values(domain=domain, last_checked=now(), last_status=status))


def list_domains():
    with engine.connect() as c:
        return [dict(r._mapping) for r in c.execute(select(domains).order_by(domains.c.domain))]


def last_pricing(domain):
    q = select(pricing).where(pricing.c.domain == domain).order_by(desc(pricing.c.id)).limit(1)
    with engine.connect() as c:
        r = c.execute(q).first()
        return dict(r._mapping) if r else None


def save_pricing(domain, url, text_hash, prices_json):
    with engine.begin() as c:
        c.execute(insert(pricing).values(domain=domain, url=url, fetched=now(),
                                         text_hash=text_hash, prices=prices_json))
