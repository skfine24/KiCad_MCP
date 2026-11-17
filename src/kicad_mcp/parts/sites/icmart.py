from __future__ import annotations

import re
from typing import Any

from bs4 import BeautifulSoup

from ..http import RateLimitedSession

BASE = "https://icmart.co.kr"
SEARCH_URL = f"{BASE}/products"


def _clean(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _to_int(value: Any) -> int | None:
    if value is None:
        return None
    match = re.search(r"[\d,]+", str(value))
    if not match:
        return None
    try:
        return int(match.group(0).replace(",", ""))
    except ValueError:
        return None


def _parse_discounts(value_node: Any) -> list[dict[str, Any]]:
    if value_node is None:
        return []
    lines = [
        line.strip()
        for line in value_node.get_text("\n", strip=True).splitlines()
        if line.strip()
    ]
    discounts: list[dict[str, Any]] = []
    pending_min_qty: int | None = None
    for line in lines:
        base = re.fullmatch(r"([\d,]+)\s*원", line)
        if base:
            price = _to_int(base.group(1))
            if price is not None:
                discounts.append({"min_qty": 1, "price": price, "discount_percent": 0})
            continue

        tier_min = re.fullmatch(r"([\d,]+)\s*개\s*이상", line)
        if tier_min:
            pending_min_qty = _to_int(tier_min.group(1))
            continue

        tier = re.search(
            r"([\d,]+)\s*개\s*이상\s*([\d,]+)\s*원(?:\s*\((\d+)%\s*DC\))?",
            line,
        )
        if tier:
            min_qty = _to_int(tier.group(1))
            price = _to_int(tier.group(2))
            percent = int(tier.group(3)) if tier.group(3) else None
            if min_qty is not None and price is not None:
                discounts.append(
                    {
                        "min_qty": min_qty,
                        "price": price,
                        "discount_percent": percent,
                    }
                )
            pending_min_qty = None
            continue

        tier_price = re.fullmatch(r"([\d,]+)\s*원(?:\s*\((\d+)%\s*DC\))?", line)
        if tier_price and pending_min_qty is not None:
            price = _to_int(tier_price.group(1))
            percent = int(tier_price.group(2)) if tier_price.group(2) else None
            if price is not None:
                discounts.append(
                    {
                        "min_qty": pending_min_qty,
                        "price": price,
                        "discount_percent": percent,
                    }
                )
            pending_min_qty = None

    discounts.sort(key=lambda item: item["min_qty"])
    return discounts


def _parse_search_anchor(anchor: Any) -> dict[str, Any] | None:
    href = anchor.get("href", "")
    if not re.fullmatch(r"/products/\d+", href):
        return None
    name_node = anchor.select_one("h4")
    code_node = anchor.select_one("p.code")
    price_node = anchor.select_one("p.price")
    name = None
    if name_node:
        name = _clean(name_node.get("title") or name_node.get_text(" ", strip=True))
    code = _clean(code_node.get_text(" ", strip=True)) if code_node else None
    price = _to_int(price_node.get_text(" ", strip=True)) if price_node else None
    if price == 0:
        price = None
    if not name:
        lines = [line.strip() for line in anchor.stripped_strings if line.strip()]
        name = lines[0] if lines else None
    if not name:
        return None
    return {
        "site": "icmart",
        "id": int(href.rsplit("/", 1)[-1]),
        "name": name,
        "code": code,
        "price": price,
        "currency": "KRW",
        "stock": None,
        "available": None,
        "url": f"{BASE}{href}",
    }


def make_session() -> RateLimitedSession:
    return RateLimitedSession(min_interval=0.8)


def parse_product_id(value: str) -> str:
    text = (value or "").strip()
    match = re.search(r"/products/(\d+)", text)
    if match:
        return match.group(1)
    if text.isdigit():
        return text
    raise ValueError(f"Invalid IC Mart product id or URL: {value!r}")


def search(session: RateLimitedSession, query: str, limit: int = 20) -> dict[str, Any]:
    limit = max(1, min(int(limit), 100))
    response = session.get(SEARCH_URL, params={"search": query})
    if response.status_code != 200:
        raise RuntimeError(f"IC Mart search failed with HTTP {response.status_code}")

    soup = BeautifulSoup(response.text, "html.parser")
    container = soup.select_one(".products-container")
    total = None
    if container:
        match = re.search(r"총\s*([\d,]+)\s*건", container.get_text(" ", strip=True))
        if match:
            total = _to_int(match.group(1))

    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for anchor in soup.select('section.product-grid a[href^="/products/"]'):
        href = anchor.get("href", "")
        if href in seen:
            continue
        item = _parse_search_anchor(anchor)
        if item is None:
            continue
        seen.add(href)
        items.append(item)
        if len(items) >= limit:
            break

    return {
        "site": "icmart",
        "query": query,
        "total": total,
        "items": items,
        "url": response.url,
    }


def product(session: RateLimitedSession, product_id: str) -> dict[str, Any]:
    pid = parse_product_id(product_id)
    response = session.get(f"{BASE}/products/{pid}")
    if response.status_code != 200:
        raise RuntimeError(
            f"IC Mart product {pid} failed with HTTP {response.status_code}"
        )

    soup = BeautifulSoup(response.text, "html.parser")
    name_node = soup.select_one(".detail-info h2")
    name = _clean(name_node.get_text(" ", strip=True)) if name_node else None

    info_text: dict[str, str] = {}
    info_nodes: dict[str, Any] = {}
    for row in soup.select(".info-row"):
        label_node = row.select_one(".label")
        value_node = row.select_one(".value")
        if not label_node or not value_node:
            continue
        label = _clean(label_node.get_text(" ", strip=True))
        info_text[label] = _clean(value_node.get_text(" ", strip=True))
        info_nodes[label] = value_node

    discounts = _parse_discounts(info_nodes.get("판매가"))
    base_price = discounts[0]["price"] if discounts else None
    stock = _to_int(info_text.get("재고수량"))

    inquiry = soup.select_one('a[href*="/inquiry?product_idx="]')
    inquiry_url = f"{BASE}{inquiry.get('href')}" if inquiry else None
    description_node = soup.select_one(".detail-description")
    description = (
        _clean(description_node.get_text(" ", strip=True)) if description_node else None
    )

    return {
        "site": "icmart",
        "id": int(pid),
        "name": name,
        "code": info_text.get("상품코드"),
        "manufacturer": info_text.get("제조사"),
        "description": description,
        "price": base_price,
        "currency": "KRW",
        "price_includes_vat": True,
        "discounts": discounts,
        "stock": stock,
        "available": stock is not None and stock > 0,
        "inquiry_url": inquiry_url,
        "url": f"{BASE}/products/{pid}",
    }
