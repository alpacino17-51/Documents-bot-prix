"""Parseurs communs à la plateforme Pepper (Dealabs, mydealz, hotukdeals, chollometro, pepper.it).

Ordre de préférence :
1. JSON embarqué dans les pages (`data-vue3` / `data-vue2`, composant ThreadMainListItemNormalizer) :
   données structurées complètes (prix, prix barré, température, marchand, domaine du lien, date).
2. Flux RSS (`/rss/...`) : prix + marchand (`<pepper:merchant>`), mais pas de température.
3. HTML brut (dernier recours).
"""
from __future__ import annotations

import json
import re
from email.utils import parsedate_to_datetime
from typing import Any, Iterator, Optional

from bs4 import BeautifulSoup

from models import Deal
from utils import extract_asin, find_prices, html_to_text, parse_price

THREAD_COMPONENTS = {"ThreadMainListItemNormalizer", "ThreadListItem", "ThreadItem"}


# ------------------------------------------------------------------ JSON embarqué

def extract_vue_payloads(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    payloads: list[dict] = []
    for attr in ("data-vue3", "data-vue2"):
        for el in soup.find_all(attrs={attr: True}):
            raw = el.get(attr)
            try:
                data = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(data, dict):
                payloads.append(data)
    return payloads


def _walk(obj: Any) -> Iterator[dict]:
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)


def threads_from_html(html: str) -> list[dict]:
    threads: dict[str, dict] = {}
    for payload in extract_vue_payloads(html):
        name = payload.get("name", "")
        props = payload.get("props") or {}
        candidates = []
        if name in THREAD_COMPONENTS and isinstance(props.get("thread"), dict):
            candidates.append(props["thread"])
        else:
            candidates.extend(d for d in _walk(props) if "threadId" in d and "title" in d and "temperature" in d)
        for t in candidates:
            tid = str(t.get("threadId", ""))
            if tid and tid not in threads:
                threads[tid] = t
    return list(threads.values())


def _num(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return parse_price(str(value))[0]


def thread_to_deal(t: dict, site: dict, source_name: str, price_error_context: bool = False) -> Optional[Deal]:
    if t.get("type") and t.get("type") not in ("Deal", "deal"):
        return None
    tid = str(t.get("threadId", "")).strip()
    title = (t.get("title") or "").strip()
    if not tid or not title:
        return None
    base = site["base_url"].rstrip("/")
    slug = t.get("titleSlug") or ""
    url = f"{base}/{site.get('deal_path', 'deals')}/{slug}-{tid}" if slug else (t.get("shareableLink") or base)
    price = _num(t.get("price"))
    if price is not None and price <= 0:
        price = None               # gratuit / prix non renseigné
    original = _num(t.get("nextBestPrice"))
    if original is None or price is None or original <= price:
        original = None
    merchant = (t.get("merchant") or {}).get("merchantName", "") if isinstance(t.get("merchant"), dict) else ""
    group = t.get("mainGroup") or {}
    link_host = (t.get("linkHost") or "").lower()
    pct = _num(t.get("percentage"))
    description = t.get("listCardDescription") or t.get("description") or ""
    deal = Deal(
        source=source_name,
        source_id=tid,
        title=title,
        url=url,
        site_country=site.get("country", ""),
        merchant=merchant,
        merchant_url=f"{base}/{site.get('visit_path', 'visit/homenew')}/{tid}",
        link_host=link_host,
        price=price,
        original_price=original,
        currency=site.get("currency", "EUR"),
        temperature=_num(t.get("temperature")),
        category=group.get("threadGroupName", "") if isinstance(group, dict) else "",
        description=html_to_text(description) if "<" in description else description,
        published_at=_num(t.get("publishedAt")),
        expired=bool(t.get("isExpired")) or bool(t.get("isGone")),
        price_error_context=price_error_context,
        displayed_discount_override=pct if pct and pct > 0 else None,
    )
    deal.asin = extract_asin(t.get("link") or "", deal.description)
    return deal


def parse_listing_json(html: str, site: dict, source_name: str, price_error_context: bool = False) -> list[Deal]:
    deals = []
    for t in threads_from_html(html):
        d = thread_to_deal(t, site, source_name, price_error_context)
        if d:
            deals.append(d)
    return deals


# ------------------------------------------------------------------ RSS

_ID_FROM_URL = re.compile(r"-(\d{4,})/?(?:[?#].*)?$")


def parse_rss(xml: str, site: dict, source_name: str, price_error_context: bool = False) -> list[Deal]:
    soup = BeautifulSoup(xml, "xml")
    deals: list[Deal] = []
    for item in soup.find_all("item"):
        title = item.find("title").get_text(strip=True) if item.find("title") else ""
        link = item.find("link").get_text(strip=True) if item.find("link") else ""
        if not title or not link:
            continue
        m = _ID_FROM_URL.search(link)
        tid = m.group(1) if m else link
        merchant_tag = item.find("pepper:merchant") or item.find("merchant")
        merchant = merchant_tag.get("name", "") if merchant_tag else ""
        price_raw = merchant_tag.get("price", "") if merchant_tag else ""
        price, cur = parse_price(price_raw, site.get("currency", "EUR")) if price_raw else (None, None)
        desc_html = item.find("description").get_text() if item.find("description") else ""
        description = html_to_text(desc_html)
        published = None
        if item.find("pubDate"):
            try:
                published = parsedate_to_datetime(item.find("pubDate").get_text(strip=True)).timestamp()
            except (TypeError, ValueError):
                published = None
        category = item.find("category").get_text(strip=True) if item.find("category") else ""
        deal = Deal(
            source=source_name,
            source_id=tid,
            title=title,
            url=link,
            site_country=site.get("country", ""),
            merchant=merchant,
            merchant_url=f"{site['base_url'].rstrip('/')}/{site.get('visit_path', 'visit/homenew')}/{tid}" if m else "",
            price=price if price and price > 0 else None,
            currency=cur or site.get("currency", "EUR"),
            category=category,
            description=description,
            published_at=published,
            price_error_context=price_error_context,
        )
        deal.asin = extract_asin(desc_html)
        deals.append(deal)
    return deals


# ------------------------------------------------------------------ HTML brut (secours)

def parse_listing_html(html: str, site: dict, source_name: str, price_error_context: bool = False) -> list[Deal]:
    soup = BeautifulSoup(html, "lxml")
    deals: list[Deal] = []
    for art in soup.select("article[id^=thread_]"):
        tid = art.get("id", "").replace("thread_", "")
        a = art.select_one("a.thread-link, .thread-title a, a.cept-tt")
        if not tid or not a:
            continue
        title = a.get("title") or a.get_text(" ", strip=True)
        href = a.get("href", "")
        price_el = art.select_one(".thread-price, .threadItemCard-price, [class*=thread-price]")
        orig_el = art.select_one(".text--lineThrough, .mute--text.text--lineThrough, [class*=lineThrough]")
        temp_el = art.select_one(".vote-temp, .cept-vote-temp, [class*=vote-temp]")
        merchant_el = art.select_one(".cept-merchant-name, [data-t=merchantLink], .threadListCard-merchant")
        price, cur = parse_price(price_el.get_text(" ", strip=True), site.get("currency")) if price_el else (None, None)
        orig, _ = parse_price(orig_el.get_text(" ", strip=True), site.get("currency")) if orig_el else (None, None)
        temp = None
        if temp_el:
            m = re.search(r"-?\d+(?:[.,]\d+)?", temp_el.get_text())
            temp = float(m.group(0).replace(",", ".")) if m else None
        classes = " ".join(art.get("class", []))
        deals.append(Deal(
            source=source_name,
            source_id=tid,
            title=title,
            url=href if href.startswith("http") else site["base_url"].rstrip("/") + href,
            site_country=site.get("country", ""),
            merchant=merchant_el.get_text(" ", strip=True) if merchant_el else "",
            merchant_url=f"{site['base_url'].rstrip('/')}/{site.get('visit_path', 'visit/homenew')}/{tid}",
            price=price if price and price > 0 else None,
            original_price=orig if orig and price and orig > price else None,
            currency=cur or site.get("currency", "EUR"),
            temperature=temp,
            expired="expired" in classes,
            price_error_context=price_error_context,
        ))
    return deals


# ------------------------------------------------------------------ commentaires de topics

def parse_topic_comments(html: str, topic: dict, site: dict, source_name: str) -> list[Deal]:
    """Extrait les commentaires d'un topic (JSON embarqué, sinon HTML) qui contiennent un lien et/ou un prix."""
    comments: dict[str, dict] = {}
    for payload in extract_vue_payloads(html):
        for d in _walk(payload):
            cid = d.get("commentId")
            content = d.get("preparedHtmlContent") or d.get("htmlContent") or d.get("content") or d.get("body")
            if cid and isinstance(content, str) and str(cid) not in comments:
                comments[str(cid)] = {"html": content, "ts": _num(d.get("createdAtTs") or d.get("createdAt"))}
    if not comments:
        soup = BeautifulSoup(html, "lxml")
        for el in soup.select("[id^=comment-], .commentList-item"):
            m = re.search(r"(\d{5,})", el.get("id", "") or el.get("data-id", ""))
            body = el.select_one(".comment-body, .userHtml, .commentList-comment")
            if m and body and m.group(1) not in comments:
                comments[m.group(1)] = {"html": str(body), "ts": None}

    base_host = re.sub(r"^https?://(www\.)?", "", site["base_url"]).split("/")[0]
    deals: list[Deal] = []
    for cid, c in comments.items():
        frag = BeautifulSoup(c["html"], "lxml")
        text = frag.get_text(" ", strip=True)
        links = [a.get("href", "") for a in frag.find_all("a") if a.get("href", "").startswith("http")]
        external = [u for u in links if base_host not in u] or links
        prices = find_prices(text)
        if not external and not prices:
            continue
        price, cur = prices[0] if prices else (None, None)
        original = next((v for v, _ in prices[1:] if price is not None and v > price), None)
        link = external[0] if external else topic["url"]
        deals.append(Deal(
            source=source_name,
            source_id=cid,
            title=(text[:140] + "…") if len(text) > 140 else text,
            url=link,
            site_country=site.get("country", ""),
            merchant=re.sub(r"^https?://(www\.)?", "", link).split("/")[0] if external else "",
            merchant_url=link,
            link_host=re.sub(r"^https?://", "", link).split("/")[0].lower() if external else "",
            price=price,
            original_price=original,
            currency=cur or "EUR",
            description=f"{topic.get('name', '')} : {text}",
            published_at=c["ts"],
            price_error_context=True,
            asin=extract_asin(*external),
        ))
    return deals
