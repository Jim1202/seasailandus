"""Extras for the "For Sale" posts about ALCYONE.

* is_sale()        - which Blogger posts belong to the sale documents
* add_toc()        - finds the section titles inside a (very long) post, gives
                     them anchors and returns an outline for "On this page"
* group_of()/nice_title() - how the For Sale page sorts and labels the posts

The text of the posts is never changed; we only add id="" anchors.
"""
from __future__ import annotations

import re
import unicodedata

from bs4 import BeautifulSoup, Tag

SALE_RE = re.compile(r"\b(FOR SALE|PREVENTE|PRE-VENTE|À VENDRE|A VENDRE)\b", re.I)

# Display order of the groups on the For Sale page.
GROUPS = [
    ("start", "Start here", "The short version: what ALCYONE is, where she is, and the key specifications."),
    ("depth", "The boat in depth", "Why she was built and equipped this way, and her full history since 2004."),
    ("inventory", "Inventory & survey", "Everything on board, where it is stowed, and the latest haul-out and survey."),
    ("french", "En français", "Les documents de vente en français."),
]


def is_sale(title: str) -> bool:
    return bool(SALE_RE.search(title))


def group_of(title: str) -> str:
    t = title.upper()
    if re.search(r"\((FR|FRENCH)\b|PREVENTE|IN FRENCH|RAPIDE", t):
        return "french"
    if "INVENTORY" in t or "SURVEY" in t:
        return "inventory"
    if "PRESENTATION" in t or "SPECIFICATION" in t:
        return "start"
    return "depth"


SMALL_WORDS = {"a", "an", "and", "at", "by", "for", "from", "in", "of", "on", "or", "the", "to", "with", "n", "be"}


def nice_title(title: str) -> str:
    """'260623 ALCYONE FOR SALE: WHY WE LOVE THIS CATAMARAN (ENGLISH)' -> 'Why We Love This Catamaran'."""
    t = re.sub(r"^\s*\d{6}\s*", "", title)
    t = re.sub(r"^ALCYONE\s+(FOR SALE|PREVENTE)\s*:?\s*", "", t, flags=re.I)
    t = re.sub(r"\s*:?\s*\((ENGLISH|ENG)\)", "", t, flags=re.I)
    t = re.sub(r"\s+", " ", t).strip(" :-")
    if t.startswith("(") and t.endswith(")"):
        t = t[1:-1].strip()
    letters = [c for c in t if c.isalpha()]
    all_caps = bool(letters) and sum(c.isupper() for c in letters) / len(letters) > 0.7
    words = []
    for i, w in enumerate(t.split(" ")):
        core = re.sub(r"[^\w]", "", w)
        if all_caps or (core.isupper() and len(core) >= 4):  # shouting words -> Title Case
            lw = w.lower()
            if lw == "n":
                w = "&"
            elif i and lw in SMALL_WORDS:
                w = lw
            else:
                w = re.sub(r"[^\W\d_]", lambda m: m.group(0).upper(), lw, count=1)
        words.append(w)
    t = " ".join(words)
    t = re.sub(r"\b(Fr|Eng|Rcs|Nz|Us|Fp)\b", lambda m: m.group(0).upper(), t)
    return t or title


# ---------------------------------------------------------------------------
# Table of contents
# ---------------------------------------------------------------------------

BLOCKS = ["h1", "h2", "h3", "h4", "h5", "h6", "p", "div", "td", "li"]
BIG_RE = re.compile(r"font-size:\s*(large|x-large|xx-large|larger|[2-9]\d(\.\d+)?px|1\.[3-9]\d*em|[2-9](\.\d+)?em)", re.I)
BOLD_RE = re.compile(r"font-weight:\s*(bold|[6-9]00)", re.I)
NUMBERED_RE = re.compile(r"^(\d{1,2}\s?\)|APPENDIX\b|ANNEXE\b|PART\s+\d)", re.I)
SKIP_TEXT = {"french", "english", "français", "francais", "fr", "eng", "contact"}


def _text(el: Tag) -> str:
    return " ".join(el.get_text(" ").split())


def _is_bold(el: Tag, text: str) -> bool:
    if el.name in ("b", "strong") or BOLD_RE.search(el.get("style", "")):
        return True
    bold = "".join(_text(b) for b in el.find_all(["b", "strong"]))
    bold += "".join(_text(s) for s in el.find_all(style=BOLD_RE))
    return len(bold) >= 0.85 * len(text.replace(" ", "")) if text else False


def _is_big(el: Tag) -> bool:
    if BIG_RE.search(el.get("style", "")):
        return True
    return any(BIG_RE.search(s.get("style", "")) for s in el.find_all(style=True))


def _rank(el: Tag, text: str) -> float | None:
    """Lower = more important. None = not a section title."""
    heading = re.match(r"h([1-6])$", el.name)
    bold, big = _is_bold(el, text), _is_big(el)
    upper = sum(c.isupper() for c in text) >= 0.7 * max(1, sum(c.isalpha() for c in text))
    if NUMBERED_RE.match(text) and (heading or bold or big):
        return 0
    if el.name in ("b", "strong"):
        return None  # inline bold only counts when numbered (e.g. "2) DRYDOCK")
    if el.name == "td" and (bold or big) and upper:
        row = el.find_parent("tr")
        if row is not None and len(row.find_all(["td", "th"])) == 1:
            return 2  # a one-cell row heading a table, e.g. inventory categories
    if heading:
        return int(heading.group(1))
    if big and bold:
        return 2
    if big and upper:
        return 3
    if bold and upper:
        return 4
    return None


def anchor_id(text: str, used: set[str]) -> str:
    s = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")[:50].strip("-") or "section"
    base, n = s, 2
    while s in used:
        s, n = f"{base}-{n}", n + 1
    used.add(s)
    return s


def add_toc(soup: BeautifulSoup, max_items: int = 200) -> list[dict]:
    """Give section titles an id and return [{id, text, level}] (level 1 or 2)."""
    found = []
    seen_text: set[str] = set()
    for el in soup.find_all(BLOCKS + ["b", "strong"]):
        if el.name in BLOCKS and el.find(BLOCKS):  # only innermost blocks
            continue
        if el.find_parent("td", class_="tr-caption") or "tr-caption" in (el.get("class") or []):
            continue  # photo captions
        text = _text(el)
        letters = sum(c.isalpha() for c in text)
        if letters < 3 or len(text) > 90 or text.lower().strip(" :") in SKIP_TEXT:
            continue
        rank = _rank(el, text)
        if rank is None:
            continue
        key = text.lower()
        if key in seen_text:
            continue  # same title repeated (e.g. nested spans)
        seen_text.add(key)
        found.append((rank, el, text))
    if not found:
        return []
    ranks = sorted({r for r, _, _ in found})
    top = ranks[0]
    if sum(1 for r, _, _ in found if r == top) < 2 and len(ranks) > 1:
        top = ranks[1]  # a single big title isn't an outline
    deeper = [r for r in ranks if r > top]
    second = deeper[0] if deeper else None
    if top == 0 and second is not None:
        # Numbered parts on top; show everything else that looks like a title below them.
        second = max(r for r in ranks if r > top and r <= 4) if any(top < r <= 4 for r in ranks) else second
    used: set[str] = set()
    toc = []
    for rank, el, text in found:
        if rank < top:
            continue  # a lone title above the outline (e.g. the post's own heading)
        level = 1 if rank == top else 2 if (second is not None and rank <= second) else None
        if level is None:
            continue
        if not el.get("id"):
            el["id"] = anchor_id(text, used)
        toc.append({"id": el["id"], "text": text.lstrip("=>●•- ").strip(), "level": level})
    if len(toc) > max_items:  # too detailed: keep the main sections only
        toc = [t for t in toc if t["level"] == 1]
    return toc


def order_docs(posts: list[dict]) -> list[dict]:
    """Sale posts in reading order: by group (Start here first), then oldest first."""
    rank = {key: i for i, (key, _, _) in enumerate(GROUPS)}
    return sorted(posts, key=lambda p: (rank[group_of(p["title"])], p["published"]))
