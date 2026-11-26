#!/usr/bin/env python3
"""Sync posts from the Blogger feed into this static site.

Reads https://seasailandus.blogspot.com/feeds/posts/default (JSON), then:
  * writes a page in posts/ for every new post, and rewrites the page of any
    post whose Blogger "updated" date has changed;
  * downloads the images those posts use into images/ (reusing any that are
    already there);
  * regenerates index.html, all-posts.html and the Archive menu in every page.

Posts are matched by their Blogger post ID, which is stored in each page
(id="post-body-<ID>") and in data/posts.json. Nothing is ever deleted: posts
that vanish from Blogger are listed in REMOVED_ON_BLOGGER.md for a human to
decide on.

Usage:  python scripts/sync_blogger.py            # sync
        python scripts/sync_blogger.py --dry-run  # report only, write nothing
Needs:  pip install beautifulsoup4
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

from bs4 import BeautifulSoup

BLOG = "https://seasailandus.blogspot.com"
FEED = BLOG + "/feeds/posts/default?alt=json&max-results={n}&start-index={i}"
PAGE_SIZE = 25  # Blogger returns fewer entries than asked when posts are large
USER_AGENT = "seasailandus-sync/1.0"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
POSTS_DIR = os.path.join(ROOT, "posts")
IMAGES_DIR = os.path.join(ROOT, "images")
MANIFEST = os.path.join(ROOT, "data", "posts.json")
REMOVED_REPORT = os.path.join(ROOT, "REMOVED_ON_BLOGGER.md")
FALLBACK_HERO = "/images/IMG_2237.jpeg"  # used when a post has no picture

# Pages written by the March 2026 scrape. On the very first run (no manifest
# yet) a post whose Blogger "updated" date is older than this is assumed to be
# already correct on the site; anything newer is re-rendered.
SCRAPE_TIME = "2026-03-26T00:00:00+00:00"

# Refuse to run if this share of known posts suddenly disappears: that is a
# feed glitch, not your dad deleting half his blog.
MAX_REMOVED_SHARE = 0.2

FR_DAYS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
FR_MONTHS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet",
             "août", "septembre", "octobre", "novembre", "décembre"]
EN_MONTHS = ["January", "February", "March", "April", "May", "June", "July",
             "August", "September", "October", "November", "December"]

INDEX_RECENT = 3  # cards under "Recent Entries"
INDEX_MORE = 6    # rows under "More from the logbook"


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------

def http_get(url: str, tries: int = 4) -> tuple[bytes, str]:
    last = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read(), r.headers.get("Content-Type", "")
        except Exception as e:  # noqa: BLE001 - retried, then re-raised
            last = e
            time.sleep(2 ** attempt)
    raise last  # type: ignore[misc]


def fetch_feed() -> list[dict]:
    entries: list[dict] = []
    total = None
    start = 1
    while True:
        body, _ = http_get(FEED.format(n=PAGE_SIZE, i=start))
        feed = json.loads(body)["feed"]
        total = int(feed["openSearch$totalResults"]["$t"])
        page = feed.get("entry", [])
        if not page:
            break
        entries.extend(page)
        start += len(page)  # advance by what we actually got
        if start > total:
            break
    ids = {post_id(e) for e in entries}
    if total is None or len(ids) != total:
        sys.exit(f"Feed looks incomplete: got {len(ids)} posts, Blogger says {total}. "
                 "Nothing written.")
    return entries


def title_of(entry: dict) -> str:
    return entry["title"]["$t"].strip()


def post_id(entry: dict) -> str:
    return re.search(r"post-(\d+)$", entry["id"]["$t"]).group(1)


def parse_time(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s)


# --------------------------------------------------------------------------
# Formatting helpers (match the existing pages)
# --------------------------------------------------------------------------

def esc(s: str) -> str:
    return html.escape(s, quote=True)


def french_date(published: str) -> str:
    d = parse_time(published)  # keep Blogger's own time zone, like the blog does
    return f"{FR_DAYS[d.weekday()]} {d.day} {FR_MONTHS[d.month - 1]} {d.year}"


def excerpt(body_html: str, limit: int = 190) -> str:
    text = " ".join(BeautifulSoup(body_html, "html.parser").get_text(" ").split())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(" ,;:.-") + "…"


def slugify(title: str) -> str:
    s = re.sub(r"[^\w]+", "-", title.lower(), flags=re.UNICODE).strip("-_")
    return s[:60].rstrip("-") or "post"


def image_name(src: str, content_type: str) -> str:
    """<name>-<sha1(url)[:12]>.<ext>, the scheme the original scrape used."""
    last = urllib.parse.urlparse(src).path.rstrip("/").split("/")[-1].split("=")[0]
    stem, ext = os.path.splitext(last)
    ext = ext.lower().lstrip(".")
    if ext not in ("jpg", "jpeg", "png", "gif", "webp"):
        ext = {"image/png": "png", "image/gif": "gif",
               "image/webp": "webp"}.get(content_type.split(";")[0].strip(), "jpg")
    stem = re.sub(r"[^A-Za-z0-9._-]", "-", stem)[:80] or "image"
    return f"{stem}-{hashlib.sha1(src.encode()).hexdigest()[:12]}.{ext}"


class Images:
    """Finds or downloads the local copy of a Blogger image."""

    def __init__(self, dry_run: bool):
        self.dry_run = dry_run
        self.by_hash: dict[str, str] = {}
        for f in os.listdir(IMAGES_DIR):
            m = re.match(r".*-([0-9a-f]{12})\.\w+$", f)
            if m:
                self.by_hash[m.group(1)] = f
        self.downloaded: list[str] = []
        self.failed: list[str] = []

    def local(self, src: str) -> str | None:
        if not src.startswith(("http://", "https://")):
            return src if src.startswith("/images/") else None
        key = hashlib.sha1(src.encode()).hexdigest()[:12]
        if key in self.by_hash:
            return "/images/" + self.by_hash[key]
        if self.dry_run:
            self.downloaded.append(src)
            return "/images/" + image_name(src, "")
        try:
            data, ctype = http_get(src)
        except Exception as e:  # noqa: BLE001
            print(f"  ! could not download {src[:90]}…: {e}", file=sys.stderr)
            self.failed.append(src)
            return None  # keep linking to Blogger for this one
        name = image_name(src, ctype)
        with open(os.path.join(IMAGES_DIR, name), "wb") as fh:
            fh.write(data)
        self.by_hash[key] = name
        self.downloaded.append(name)
        return "/images/" + name


def first_image(body_html: str) -> str | None:
    img = BeautifulSoup(body_html, "html.parser").find("img", src=True)
    return img["src"] if img else None


def render_body(entry: dict, images: Images) -> tuple[str, str]:
    """Return (article inner html, hero image path)."""
    soup = BeautifulSoup(entry["content"]["$t"], "html.parser")
    hero = None
    for tag in soup.find_all(["style", "script"]):  # e.g. CSS pasted from Word
        tag.decompose()
    for img in soup.find_all("img", src=True):
        local = images.local(img["src"])
        if local:
            img["src"] = local
            hero = hero or local
        for attr in ("width", "height", "border"):  # let the stylesheet size images
            img.attrs.pop(attr, None)
        img["decoding"] = "async"
        img["loading"] = "lazy"
    for a in soup.find_all("a", href=True):
        if a["href"].startswith(("http://", "https://")):
            a["target"] = "_blank"
            a["rel"] = "noopener noreferrer"
    body = (f'<div class="post-body entry-content" id="post-body-{post_id(entry)}" '
            f'itemprop="description articleBody">\n{soup.decode().strip()}\n'
            f'<div style="clear: both;"></div>\n</div>')
    return body, hero or FALLBACK_HERO


# --------------------------------------------------------------------------
# Page templates
# --------------------------------------------------------------------------

HEAD = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>{title} | Sea Sail & Us</title>
  <meta name="description" content="Sea Sail & Us — sailing journal aboard Alcyone." />
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:wght@500;600;700&family=Source+Serif+4:opsz,wght@8..60,400;500;600&display=swap" rel="stylesheet">
  <link rel="stylesheet" href="/assets/css/style.css" />
</head>
<body class="{body_class}">
<header class="site-header">
  <div class="container header-inner">
    <a class="brand" href="/index.html">Sea Sail & Us</a>
    <nav class="main-nav">
      <a href="/index.html">Home</a>
      <a href="/all-posts.html">All Posts</a>
      <div class="archive-dropdown">
        <button type="button">Archive</button>
        <div class="archive-panel">{archive}</div>
      </div>
    </nav>
  </div>
</header>
"""

FOOT = """
<footer class="site-footer">
  <div class="container footer-inner">
    <p>Sea Sail & Us — journal de bord d'Alcyone.</p>
    <a href="/all-posts.html">Browse the full archive</a>
  </div>
</footer>
</body>
</html>"""


def archive_panel(posts: list[dict]) -> str:
    years: dict[int, list[int]] = {}
    for p in posts:
        d = parse_time(p["published"])
        months = years.setdefault(d.year, [])
        if d.month not in months:
            months.append(d.month)
    out = []
    for y in sorted(years, reverse=True):
        items = "".join(
            f'<li><a href="/all-posts.html#{y}-{EN_MONTHS[m - 1].lower()}">{EN_MONTHS[m - 1]}</a></li>'
            for m in sorted(years[y], reverse=True))
        out.append(f"<details><summary>{y}</summary><ul>{items}</ul></details>")
    return "".join(out)


def render_post_page(p: dict, body: str, archive: str) -> str:
    t = esc(p["title"])
    return (HEAD.format(title=t, body_class="post-page", archive=archive) + f"""
<main>
  <section class="post-hero compact">
    <div class="post-hero-image" style="background-image:url('{p['hero']}')"></div>
    <div class="post-hero-overlay"></div>
    <div class="container post-hero-content">
      <a class="eyebrow-link" href="/all-posts.html">← Back to archive</a>
      <p class="post-date">{french_date(p['published'])}</p>
      <h1>{t}</h1>
    </div>
  </section>
  <section class="post-shell container">
    <article class="post-content">{body}</article>
  </section>
</main>
""" + FOOT)


def url(p: dict) -> str:
    return "/posts/" + p["file"]


def render_index(posts: list[dict], archive: str) -> str:
    top = posts[0]
    t = esc(top["title"])
    cards = "".join(f"""
    <article class="post-card">
      <a class="post-card-image" href="{url(p)}"><img src="{p['hero']}" alt="{esc(p['title'])}" loading="lazy"></a>
      <div class="post-card-body">
        <p class="card-date">{french_date(p['published'])}</p>
        <h3><a href="{url(p)}">{esc(p['title'])}</a></h3>
        <p>{esc(p['excerpt'])}</p>
        <a class="read-more" href="{url(p)}">Read more →</a>
      </div>
    </article>
    """ for p in posts[1:1 + INDEX_RECENT])
    rows = "".join(f"""
    <article class="list-post">
      <a class="list-post-image" href="{url(p)}"><img src="{p['hero']}" alt="{esc(p['title'])}" loading="lazy"></a>
      <div>
        <p class="card-date">{french_date(p['published'])}</p>
        <h3><a href="{url(p)}">{esc(p['title'])}</a></h3>
        <p>{esc(p['excerpt'])}</p>
      </div>
    </article>
    """ for p in posts[1 + INDEX_RECENT:1 + INDEX_RECENT + INDEX_MORE])
    return HEAD.format(title="Home", body_class="home-page", archive=archive) + f"""
<main>
  <section class="home-hero">
    <div class="hero-bg" style="background-image:url('/images/IMG_2237.jpeg')"></div>
    <div class="hero-shade"></div>
    <div class="container hero-content">
      <p class="hero-kicker">Sea Sail &amp; Us</p>
      <h1>A sailing journal aboard Alcyone</h1>
      <p class="hero-text">Stories, passages, anchorages and life at sea — from the Mediterranean to the South Pacific.</p>
      <div class="hero-actions">
        <a class="button solid" href="{url(top)}">Read the latest post</a>
        <a class="button ghost" href="/all-posts.html">Browse the archive</a>
      </div>
    </div>
  </section>

  <section class="featured-post container">
    <div class="section-head">
      <h2>Latest Entry</h2>
      <a href="/all-posts.html">View all {len(posts)} posts →</a>
    </div>
    <article class="featured-grid">
      <a class="featured-image" href="{url(top)}"><img src="{top['hero']}" alt="{t}"></a>
      <div class="featured-copy">
        <p class="card-date">{french_date(top['published'])}</p>
        <h3><a href="{url(top)}">{t}</a></h3>
        <p>{esc(top['excerpt'])}</p>
        <a class="read-more" href="{url(top)}">Open journal entry →</a>
      </div>
    </article>
  </section>

  <section class="recent-posts container">
    <div class="section-head">
      <h2>Recent Entries</h2>
      <a href="/all-posts.html">View full archive →</a>
    </div>
    <div class="cards-grid">
      {cards}</div>
  </section>

  <section class="more-posts container">
    <div class="section-head"><h2>More from the logbook</h2></div>
    <div class="list-posts">{rows}</div>
  </section>
</main>
""" + FOOT


def render_all_posts(posts: list[dict], archive: str) -> str:
    out, cur_year, cur_month = [], None, None
    for p in posts:
        d = parse_time(p["published"])
        if d.year != cur_year:
            if cur_year is not None:
                out.append("</div></div></section>")
            out.append(f'<section class="archive-year container"><h2>{d.year}</h2>')
            cur_year, cur_month = d.year, None
        if d.month != cur_month:
            if cur_month is not None:
                out.append("</div></div>")
            m = EN_MONTHS[d.month - 1]
            out.append(f'<div class="archive-month" id="{d.year}-{m.lower()}"><h3>{m}</h3><div class="archive-posts">')
            cur_month = d.month
        out.append(f"""<article class="archive-item">
              <a class="archive-thumb" href="{url(p)}"><img src="{p['hero']}" alt="{esc(p['title'])}" loading="lazy"></a>
              <div>
                <p class="card-date">{french_date(p['published'])}</p>
                <h4><a href="{url(p)}">{esc(p['title'])}</a></h4>
                <p>{esc(p['excerpt'])}</p>
              </div>
            </article>""")
    if posts:
        out.append("</div></div></section>")
    return HEAD.format(title="All Posts", body_class="", archive=archive) + """
<main class="archive-page">
  <section class="archive-hero container">
    <p class="hero-kicker dark">Archive</p>
    <h1>Every voyage, stopover and crossing</h1>
    <p>Browse the journal by year and month, or use the archive menu above.</p>
  </section>
  """ + "".join(out) + """
</main>
""" + FOOT


# --------------------------------------------------------------------------
# Manifest
# --------------------------------------------------------------------------

def load_manifest(feed_by_id: dict[str, dict]) -> dict[str, dict]:
    if os.path.exists(MANIFEST):
        with open(MANIFEST, encoding="utf-8") as fh:
            return json.load(fh)
    # First run: discover existing pages by the Blogger ID inside them.
    manifest: dict[str, dict] = {}
    for path in sorted(glob.glob(os.path.join(POSTS_DIR, "*.html"))):
        with open(path, encoding="utf-8") as fh:
            m = re.search(r'id="post-body-(\d+)"', fh.read())
        if not m:
            continue
        pid = m.group(1)
        e = feed_by_id.get(pid)
        current = e is not None and parse_time(e["updated"]["$t"]) < parse_time(SCRAPE_TIME)
        manifest[pid] = {"file": os.path.basename(path),
                         "updated": e["updated"]["$t"] if current else None}
    return manifest


def new_filename(entry: dict, taken: set[str]) -> str:
    d = parse_time(entry["published"]["$t"])
    base = f"{d:%Y-%m-%d}-{slugify(title_of(entry))}"
    name = base + ".html"
    if name in taken:
        name = f"{base}-{post_id(entry)[-6:]}.html"
    return name


def write_if_changed(path: str, text: str, dry_run: bool) -> bool:
    old = None
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            old = fh.read()
    if old == text:
        return False
    if not dry_run:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
    return True


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true", help="report what would change, write nothing")
    args = ap.parse_args()

    entries = fetch_feed()
    feed_by_id = {post_id(e): e for e in entries}
    manifest = load_manifest(feed_by_id)
    images = Images(args.dry_run)

    removed = [pid for pid in manifest if pid not in feed_by_id]
    if manifest and len(removed) > MAX_REMOVED_SHARE * len(manifest):
        sys.exit(f"{len(removed)} of {len(manifest)} posts missing from the feed; "
                 "that looks like a Blogger glitch. Nothing written.")

    taken = {m["file"] for m in manifest.values()}
    added, updated = [], []
    for pid, e in feed_by_id.items():
        known = manifest.get(pid)
        if known and known.get("updated") == e["updated"]["$t"] and "title" in known:
            continue  # unchanged and fully described
        if known and known.get("updated") == e["updated"]["$t"]:
            # Unchanged page from the original scrape: only fill in list metadata.
            src = first_image(e["content"]["$t"])
            known.update(title=title_of(e), published=e["published"]["$t"],
                         excerpt=excerpt(e["content"]["$t"]),
                         hero=(images.local(src) if src else None) or FALLBACK_HERO,
                         blogger_url=next((l["href"] for l in e["link"] if l["rel"] == "alternate"), ""))
            continue
        body, hero = render_body(e, images)
        file = known["file"] if known else new_filename(e, taken)
        taken.add(file)
        manifest[pid] = {
            "file": file,
            "updated": e["updated"]["$t"],
            "title": title_of(e),
            "published": e["published"]["$t"],
            "excerpt": excerpt(e["content"]["$t"]),
            "hero": hero,
            "blogger_url": next((l["href"] for l in e["link"] if l["rel"] == "alternate"), ""),
            "_body": body,
        }
        (updated if known else added).append(pid)

    posts = sorted(({"id": k, **v} for k, v in manifest.items()),
                   key=lambda p: p["published"], reverse=True)
    archive = archive_panel(posts)

    changed_files = []
    for p in posts:
        if "_body" in p:
            path = os.path.join(POSTS_DIR, p["file"])
            if write_if_changed(path, render_post_page(p, p["_body"], archive), args.dry_run):
                changed_files.append(path)
    # Refresh the Archive menu on every other page.
    panel_re = re.compile(r'<div class="archive-panel">.*?</div>\n', re.S)
    for path in sorted(glob.glob(os.path.join(POSTS_DIR, "*.html"))) + [os.path.join(ROOT, "about.html")]:
        if path in changed_files:
            continue
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        new = panel_re.sub(lambda _m: f'<div class="archive-panel">{archive}</div>\n', text, count=1)
        if write_if_changed(path, new, args.dry_run):
            changed_files.append(path)
    for name, text in (("index.html", render_index(posts, archive)),
                       ("all-posts.html", render_all_posts(posts, archive))):
        if write_if_changed(os.path.join(ROOT, name), text, args.dry_run):
            changed_files.append(os.path.join(ROOT, name))

    for v in manifest.values():
        v.pop("_body", None)
    ordered = {p["id"]: manifest[p["id"]] for p in posts}
    write_if_changed(MANIFEST, json.dumps(ordered, ensure_ascii=False, indent=1) + "\n", args.dry_run)

    if removed:
        lines = ["# Posts no longer on Blogger", "",
                 "These posts are still on seasailandus.com but have disappeared from the Blogger feed.",
                 "Nothing was deleted. Remove them by hand (page in posts/ + entry in data/posts.json),",
                 "then run the sync again, or ignore this if they were removed by mistake on Blogger.", ""]
        lines += [f"- {manifest[pid].get('title', pid)} — posts/{manifest[pid]['file']} (Blogger ID {pid})"
                  for pid in removed]
        write_if_changed(REMOVED_REPORT, "\n".join(lines) + "\n", args.dry_run)
    elif os.path.exists(REMOVED_REPORT) and not args.dry_run:
        os.remove(REMOVED_REPORT)

    def label(pid: str) -> str:
        m = manifest[pid]
        return f"{m['published'][:10]}  {m['title'].strip()}  -> posts/{m['file']}"

    prefix = "[dry run] " if args.dry_run else ""
    print(f"{prefix}Feed: {len(entries)} posts. Site now: {len(posts)} posts.")
    print(f"{prefix}Added {len(added)}:")
    for pid in sorted(added, key=lambda k: manifest[k]['published']):
        print("  +", label(pid))
    print(f"{prefix}Updated {len(updated)}:")
    for pid in updated:
        print("  ~", label(pid))
    print(f"{prefix}Images downloaded: {len(images.downloaded)}; failed: {len(images.failed)}")
    print(f"{prefix}Files changed: {len(changed_files)}")
    if removed:
        print(f"{prefix}WARNING: {len(removed)} post(s) no longer on Blogger, see REMOVED_ON_BLOGGER.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
