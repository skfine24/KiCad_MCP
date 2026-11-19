from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

from ..http import RateLimitedSession

API_BASE = "https://wmsc.lcsc.com"
SITE_BASE = "https://www.lcsc.com"
SEARCH_URL = f"{API_BASE}/ftps/wm/product/query/list"
PRODUCT_URL = f"{API_BASE}/ftps/wm/product/detail"
ALTERNATE_URL = f"{API_BASE}/ftps/wm/product/alternate/part"

# The public LCSC search API is used by the website itself and works without a
# login. These headers mirror the browser request (Origin/Referer are required).
API_HEADERS = {
    "accept": "application/json, text/plain, */*",
    "content-type": "application/json;charset=UTF-8",
    "origin": SITE_BASE,
    "referer": f"{SITE_BASE}/",
}


def _to_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(float(str(value).replace(",", "")))
    except (TypeError, ValueError):
        return None


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _price_tiers(item: dict[str, Any]) -> list[dict[str, Any]]:
    tiers: list[dict[str, Any]] = []
    for row in item.get("productPriceList") or []:
        price = _to_float(row.get("usdPrice"))
        if price is None:
            price = _to_float(row.get("currencyPrice"))
        if price is None:
            price = _to_float(row.get("productPrice"))
        if price is None:
            continue
        tiers.append(
            {
                "min_qty": _to_int(row.get("ladder")) or 1,
                "price": price,
                "currency": "USD",
            }
        )
    tiers.sort(key=lambda tier: tier["min_qty"])
    return tiers


def _summary(item: dict[str, Any]) -> dict[str, Any]:
    code = item.get("productCode")
    tiers = _price_tiers(item)
    stock = _to_int(item.get("stockNumber"))
    return {
        "site": "lcsc",
        "id": item.get("productId"),
        "code": code,
        "model": item.get("productModel"),
        "name": item.get("productNameEn") or item.get("productModel"),
        "brand": item.get("brandNameEn"),
        "package": item.get("encapStandard"),
        "description": item.get("productIntroEn"),
        "price": tiers[0]["price"] if tiers else None,
        "currency": "USD",
        "price_tiers": tiers,
        "stock": stock,
        "available": bool(stock and stock > 0),
        "pre_sale": item.get("isPreSale"),
        "forbid": item.get("isForbid"),
        "not_overstock": item.get("isNotOverstock"),
        "product_cycle": item.get("productCycle"),
        "min_qty": _to_int(item.get("minBuyNumber")),
        "pack_qty": _to_int(item.get("minPacketNumber")),
        "pack_unit": item.get("minPacketUnit"),
        "unit": item.get("productUnit"),
        "url": f"{SITE_BASE}/product-detail/{code}.html" if code else None,
        "pdf_url": item.get("pdfUrl"),
        "catalog": item.get("catalogName"),
        "rohs": item.get("isRohsCert"),
    }


def make_session() -> RateLimitedSession:
    return RateLimitedSession(min_interval=0.8)


def parse_product_code(value: str) -> str:
    text = (value or "").strip()
    match = re.search(r"C\d{4,}", text, re.IGNORECASE)
    if match:
        return match.group(0).upper()
    raise ValueError(f"Invalid LCSC part number or URL: {value!r}")


def search(
    session: RateLimitedSession,
    query: str,
    limit: int = 20,
    page: int = 1,
    catalog_ids: list[int] | None = None,
) -> dict[str, Any]:
    limit = max(1, min(int(limit), 100))
    page = max(1, int(page))
    body: dict[str, Any] = {
        "keyword": query,
        "currentPage": page,
        "pageSize": limit,
    }
    if catalog_ids:
        body["catalogIdList"] = [int(catalog_id) for catalog_id in catalog_ids]
    response = session.post(SEARCH_URL, json=body, headers=API_HEADERS)
    if response.status_code != 200:
        raise RuntimeError(f"LCSC search failed with HTTP {response.status_code}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError("LCSC search returned a non-JSON response") from exc
    if payload.get("code") != 200:
        raise RuntimeError(f"LCSC search failed: {payload.get('msg') or payload.get('code')}")
    result = payload.get("result") or {}
    items = [_summary(item) for item in (result.get("dataList") or [])]
    return {
        "site": "lcsc",
        "query": query,
        "page": _to_int(result.get("currPage")) or page,
        "total": _to_int(result.get("totalRow")),
        "total_pages": _to_int(result.get("totalPage")),
        "items": items,
        "url": f"{SITE_BASE}/search?q={quote(query)}",
    }


def product(session: RateLimitedSession, product_code: str) -> dict[str, Any]:
    code = parse_product_code(product_code)
    response = session.get(PRODUCT_URL, params={"productCode": code}, headers=API_HEADERS)
    if response.status_code != 200:
        raise RuntimeError(f"LCSC product {code} failed with HTTP {response.status_code}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError("LCSC product returned a non-JSON response") from exc
    if payload.get("code") != 200:
        raise RuntimeError(
            f"LCSC product {code} failed: {payload.get('msg') or payload.get('code')}"
        )
    result = payload.get("result") or {}
    detail = _summary(result)
    detail.update(
        {
            "key_attributes": result.get("productKeyAttributes"),
            "catalog_path": [
                name
                for name in (result.get("parentCatalogName"), result.get("catalogName"))
                if name
            ],
            "parameters": [
                {
                    "name": param.get("paramNameEn") or param.get("paramName"),
                    "value": param.get("paramValueEn") or param.get("paramValue"),
                }
                for param in (result.get("paramVOList") or [])
                if (param.get("paramNameEn") or param.get("paramName"))
                and (param.get("paramValueEn") or param.get("paramValue"))
            ],
            "reel_price": _to_float(result.get("reelPrice")),
            "is_reel": result.get("isReel"),
            "min_packet_number": _to_int(result.get("minPacketNumber")),
            "min_packet_unit": result.get("minPacketUnit"),
            "images": result.get("productImages") or [],
            "datasheet": result.get("pdfUrl"),
            "weight_g": _to_float(result.get("productWeight")),
            "eccn": result.get("eccn"),
            "moisture_sensitivity_level": result.get("moistureSensitivityLevel"),
        }
    )
    return detail


def alternatives(
    session: RateLimitedSession,
    product_code: str,
    limit: int = 10,
) -> dict[str, Any]:
    """Return LCSC's alternative-part list for a product code."""
    code = parse_product_code(product_code)
    limit = max(1, min(int(limit), 50))
    response = session.get(
        ALTERNATE_URL,
        params={"productCode": code},
        headers=API_HEADERS,
    )
    if response.status_code != 200:
        raise RuntimeError(
            f"LCSC alternatives for {code} failed with HTTP {response.status_code}"
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError("LCSC alternatives returned a non-JSON response") from exc
    if payload.get("code") != 200:
        raise RuntimeError(
            f"LCSC alternatives for {code} failed: "
            f"{payload.get('msg') or payload.get('code')}"
        )
    result = payload.get("result") or {}
    items = [_summary(item) for item in (result.get("dataList") or [])][:limit]
    return {
        "site": "lcsc",
        "product_code": code,
        "total": _to_int(result.get("totalRow")),
        "items": items,
    }


def _item_query(item: dict[str, Any]) -> str:
    for key in ("mpn", "name", "value", "part", "description"):
        value = item.get(key)
        if value:
            return str(value).strip()
    raise ValueError("BOM item has no name/value/mpn field")


def _price_for_qty(tiers: list[dict[str, Any]] | None, qty: int) -> float | None:
    price = None
    for tier in tiers or []:
        if qty >= (tier.get("min_qty") or 1):
            price = tier.get("price")
    return price


def bom_search(
    session: RateLimitedSession,
    items: list[dict[str, Any]],
    limit: int = 3,
) -> dict[str, Any]:
    limit = max(1, min(int(limit), 10))
    results: list[dict[str, Any]] = []
    available_items = 0

    for item in items:
        qty = _to_int(item.get("qty") or item.get("quantity")) or 1
        if qty < 1:
            qty = 1
        code = (
            item.get("lcsc_code")
            or item.get("lcsc")
            or item.get("lcsc_part_number")
        )
        candidates: list[dict[str, Any]] = []
        errors: list[str] = []
        query: str | None = None

        if code:
            try:
                candidates = [product(session, str(code))]
            except Exception as exc:  # noqa: BLE001
                errors.append(f"lcsc: {exc}")
        else:
            try:
                query = _item_query(item)
                search_result = search(session, query, limit)
                candidates = search_result.get("items", [])[:limit]
            except Exception as exc:  # noqa: BLE001
                errors.append(f"lcsc: {exc}")

        for candidate in candidates:
            moq = candidate.get("min_qty") or 1
            order_qty = max(qty, moq)
            unit_price = _price_for_qty(candidate.get("price_tiers"), order_qty)
            subtotal = unit_price * order_qty if unit_price is not None else None
            candidate["bom_qty"] = qty
            candidate["moq"] = moq
            candidate["order_qty"] = order_qty
            candidate["pack_qty"] = candidate.get("pack_qty")
            candidate["pack_unit"] = candidate.get("pack_unit")
            candidate["subtotal"] = subtotal
            candidate["available"] = bool(
                candidate.get("stock") is not None
                and candidate["stock"] >= order_qty
            )

        recommended = next(
            (candidate for candidate in candidates if candidate.get("available")),
            None,
        )
        if recommended:
            available_items += 1
        results.append(
            {
                "name": query or code or item.get("mpn") or "?",
                "qty": qty,
                "mpn": item.get("mpn"),
                "package": item.get("package"),
                "candidates": candidates,
                "recommended": recommended,
                "available": recommended is not None,
                "errors": errors,
            }
        )

    return {
        "site": "lcsc",
        "items": results,
        "summary": {
            "total_items": len(results),
            "available_items": available_items,
            "unavailable_items": len(results) - available_items,
        },
    }
