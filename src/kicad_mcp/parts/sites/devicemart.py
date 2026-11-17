from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup

from ..http import RateLimitedSession

BASE = "https://www.devicemart.co.kr"
SEARCH_URL = f"{BASE}/goods/search"
SSE_URL = f"{BASE}/goods/search_sse_stream"
CART_URL = f"{BASE}/order/cart"
ADD_URL = f"{BASE}/order/add"
DEL_URL = f"{BASE}/order/del"


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


def _all_ints(value: str) -> list[int]:
    result: list[int] = []
    for match in re.finditer(r"[\d,]+", value or ""):
        parsed = _to_int(match.group(0))
        if parsed is not None:
            result.append(parsed)
    return result


def _lookup_queries(
    name: str | None,
    title: str | None,
    product_no: str,
) -> list[str]:
    queries: list[str] = []
    for raw in (name, title, product_no):
        if not raw:
            continue
        cleaned = re.sub(r"\[[^\]]*\]", " ", raw)
        cleaned = re.sub(r"\([^)]*\)", " ", cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        if cleaned:
            queries.append(cleaned)
        tokens = cleaned.split()
        if tokens:
            queries.append(tokens[0])
    result: list[str] = []
    seen: set[str] = set()
    for query in queries:
        if query and query not in seen:
            seen.add(query)
            result.append(query)
    return result[:4]


def _parse_quantity_discounts(value: str) -> list[dict[str, Any]]:
    discounts: list[dict[str, Any]] = []
    pattern = re.compile(
        r"([\d,]+)\s*개\s*이상(?:\s*~\s*([\d,]+)\s*개\s*미만)?\s*:\s*"
        r"1개당\s*([\d,]+)\s*원"
    )
    for match in pattern.finditer(value or ""):
        min_qty = _to_int(match.group(1))
        max_qty = _to_int(match.group(2)) if match.group(2) else None
        price = _to_int(match.group(3))
        if min_qty is not None and price is not None:
            discounts.append(
                {"min_qty": min_qty, "max_qty": max_qty, "price": price}
            )
    discounts.sort(key=lambda item: item["min_qty"])
    return discounts


def make_session(state_dir: Path) -> RateLimitedSession:
    return RateLimitedSession(
        state_dir=state_dir,
        cookie_file="devicemart_cookies.json",
        min_interval=0.8,
    )


def parse_product_id(value: str) -> str:
    text = (value or "").strip()
    match = re.search(r"[?&]no=(\d+)", text)
    if match:
        return match.group(1)
    if text.isdigit():
        return text
    raise ValueError(f"Invalid DeviceMart product no or URL: {value!r}")


def _normalize_record(record: dict[str, Any]) -> dict[str, Any]:
    stock = _to_int(record.get("stock"))
    status = record.get("goods_status") or ""
    available = status not in {"runout", "soldout"} and stock is not None and stock > 0
    return {
        "site": "devicemart",
        "id": str(record.get("goods_seq")),
        "name": record.get("goods_name"),
        "code": record.get("goods_code"),
        "manufacturer": record.get("manufacture"),
        "summary": record.get("summary"),
        "price": _to_int(record.get("sale_price") or record.get("price")),
        "currency": "KRW",
        "stock": stock,
        "status": status,
        "moq": _to_int(record.get("min_purchase_ea")) or 1,
        "order_multiple": _to_int(record.get("min_purchase_ea_multiple")) or 1,
        "available": available,
        "url": f"{BASE}/goods/view?no={record.get('goods_seq')}",
    }


def _normalize_external(record: dict[str, Any], api_name: str | None) -> dict[str, Any]:
    prices = record.get("prices") or record.get("price") or []
    first_price = None
    if isinstance(prices, list) and prices:
        first_price = prices[0].get("price") if isinstance(prices[0], dict) else prices[0]
    stock = _to_int(record.get("stock"))
    return {
        "site": "devicemart_external",
        "source": api_name or "external",
        "id": record.get("sku"),
        "name": record.get("mpn") or record.get("sku"),
        "manufacturer": record.get("manufacture"),
        "summary": record.get("summary"),
        "price": _to_int(first_price),
        "currency": record.get("currency"),
        "stock": stock,
        "moq": _to_int(record.get("moq")),
        "available": stock is not None and stock > 0,
        "url": record.get("site") or None,
    }


def _get_csrf(session: RateLimitedSession, query: str) -> str:
    response = session.get(
        SEARCH_URL,
        params={
            "keyword_log_flag": "Y",
            "search_text": query,
            "q": query,
            "x": "0",
            "y": "0",
        },
    )
    if response.status_code != 200:
        raise RuntimeError(f"DeviceMart search page failed with HTTP {response.status_code}")
    soup = BeautifulSoup(response.text, "html.parser")
    token = soup.select_one("#csrf_token")
    return token.get("value") if token else ""


def search(
    session: RateLimitedSession,
    query: str,
    limit: int = 10,
    include_external: bool = False,
) -> dict[str, Any]:
    limit = max(1, min(int(limit), 50))
    csrf = _get_csrf(session, query)
    params = {
        "search_text": query,
        "old_search_text": "",
        "start_price": "0",
        "end_price": "0",
        "insearch": "0",
        "perpage": str(limit),
        "page": "1",
        "stock": "N",
        "if_count_chk": "",
        "keyword_log_flag": "Y",
        "category_code": "",
        "sort": "popular_sales",
        "tab": "all",
        "csrf_token": csrf,
    }
    response = session.get(
        SSE_URL,
        params=params,
        stream=True,
        headers={"Accept": "text/event-stream"},
        timeout=(10, 30),
    )
    if response.status_code != 200:
        raise RuntimeError(f"DeviceMart SSE search failed with HTTP {response.status_code}")

    items: list[dict[str, Any]] = []
    external: list[dict[str, Any]] = []
    total = None
    event = None
    deadline = time.monotonic() + 30
    try:
        for line in response.iter_lines(decode_unicode=True):
            if time.monotonic() > deadline:
                break
            if line is None:
                continue
            if line.startswith("event:"):
                event = line[6:].strip()
                continue
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload:
                continue
            try:
                data = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if event == "sphinx_result":
                for record in data.get("list", {}).get("record", []):
                    items.append(_normalize_record(record))
            elif event == "api_result" and include_external:
                api_name = data.get("api_name")
                for record in data.get("data", []):
                    external.append(_normalize_external(record, api_name))
            elif event == "complete":
                total = data.get("total_results")
            elif event == "error":
                raise RuntimeError(f"DeviceMart SSE error: {data.get('message', payload)}")
            elif event == "end":
                break
    finally:
        response.close()

    return {
        "site": "devicemart",
        "query": query,
        "total": total,
        "items": items[:limit],
        "external": external[:limit] if include_external else [],
        "url": response.url,
    }


def product(session: RateLimitedSession, product_no: str) -> dict[str, Any]:
    no = parse_product_id(product_no)
    response = session.get(f"{BASE}/goods/view?no={no}")
    if response.status_code != 200:
        raise RuntimeError(
            f"DeviceMart product {no} failed with HTTP {response.status_code}"
        )

    soup = BeautifulSoup(response.text, "html.parser")
    name_text = _clean(
        soup.select_one(".goods_name").get_text(" ", strip=True)
        if soup.select_one(".goods_name")
        else ""
    )
    manufacturer = None
    name = name_text
    bracket = re.match(r"^\[([^\]]+)\]\s*(.*)$", name_text)
    if bracket:
        manufacturer = bracket.group(1)
        name = bracket.group(2).strip()

    title_node = soup.select_one(".goods_title")
    title = _clean(title_node.get_text(" ", strip=True)) if title_node else None
    sell_price_text = _clean(
        soup.select_one(".sell_price").get_text(" ", strip=True)
        if soup.select_one(".sell_price")
        else ""
    )
    price_wrap_text = _clean(
        soup.select_one(".price_wrap").get_text(" ", strip=True)
        if soup.select_one(".price_wrap")
        else ""
    )
    spec_text = _clean(
        soup.select_one(".goods_spec_table").get_text(" ", strip=True)
        if soup.select_one(".goods_spec_table")
        else ""
    )
    option_text = _clean(
        soup.select_one(".goods_option_table").get_text(" ", strip=True)
        if soup.select_one(".goods_option_table")
        else ""
    )

    price = _to_int(sell_price_text) or _to_int(price_wrap_text)
    wrap_prices = _all_ints(price_wrap_text)
    price_vat_included = wrap_prices[1] if len(wrap_prices) > 1 else None

    stock_match = re.search(r"재고\s*([\d,]+)\s*개", spec_text)
    stock = _to_int(stock_match.group(1)) if stock_match else None
    stock_source = "product_page" if stock is not None else "unknown"

    form = soup.find("form", {"name": "goodsForm"})
    fields: dict[str, str] = {}
    if form:
        for inp in form.find_all("input"):
            field_name = inp.get("name")
            if field_name:
                fields[field_name] = inp.get("value") or ""

    moq = _to_int(fields.get("optionEa[0]")) or 1
    multiple_input = soup.find("input", {"name": "min_purchase_ea_multiple"})
    order_multiple = _to_int(multiple_input.get("value")) if multiple_input else moq
    if not order_multiple:
        order_multiple = moq

    option_fields = [
        value
        for key, value in fields.items()
        if key.startswith("option[") and value
    ]
    requires_options = bool(fields.get("option_use") or option_fields)

    status = "available" if stock is not None and stock > 0 else "unknown"
    available = stock is not None and stock > 0
    if stock is None:
        for lookup_query in _lookup_queries(name, title, no):
            try:
                lookup = search(session, lookup_query, limit=10)
                match = next(
                    (
                        item
                        for item in lookup.get("items", [])
                        if str(item.get("id")) == str(no)
                    ),
                    None,
                )
                if match:
                    stock = match.get("stock")
                    status = match.get("status") or status
                    available = bool(match.get("available"))
                    stock_source = "search"
                    break
            except Exception:
                continue

    return {
        "site": "devicemart",
        "id": no,
        "name": name,
        "manufacturer": manufacturer,
        "title": title,
        "description": title,
        "price": price,
        "currency": "KRW",
        "price_vat_included": price_vat_included,
        "discounts": _parse_quantity_discounts(option_text),
        "stock": stock,
        "stock_source": stock_source,
        "status": status,
        "available": available,
        "moq": moq,
        "order_multiple": order_multiple,
        "requires_options": requires_options,
        "cart_form_action": form.get("action") if form else ADD_URL,
        "cart_form_fields": fields,
        "cart_url": CART_URL,
        "url": f"{BASE}/goods/view?no={no}",
        "option_text": option_text,
    }


def add_to_cart(
    session: RateLimitedSession,
    product_no: str,
    qty: int,
    confirm: bool = False,
) -> dict[str, Any]:
    if not confirm:
        raise ValueError("add_to_cart_devicemart requires confirm=True")
    qty = int(qty)
    product_data = product(session, product_no)
    if not product_data["available"]:
        raise RuntimeError(f"DeviceMart product {product_data['id']} is not available")
    if qty < product_data["moq"]:
        raise ValueError(
            f"Quantity {qty} is below MOQ {product_data['moq']} for product {product_data['id']}"
        )
    if product_data["order_multiple"] and qty % product_data["order_multiple"] != 0:
        raise ValueError(
            f"Quantity {qty} must be a multiple of {product_data['order_multiple']}"
        )
    if product_data["requires_options"]:
        raise ValueError(
            f"Product {product_data['id']} requires option selection; use the website"
        )

    fields = dict(product_data["cart_form_fields"])
    fields["optionEa[0]"] = str(qty)
    response = session.post(f"{BASE}/order/add", data=fields)
    if response.status_code != 200:
        raise RuntimeError(f"DeviceMart cart add failed with HTTP {response.status_code}")
    text = response.text
    if "장바구니에 담겼습니다" not in text and "cart_add" not in text:
        raise RuntimeError(f"DeviceMart cart add failed: {text[:400]}")

    cart_data = cart(session)
    return {
        "ok": True,
        "product": product_data,
        "qty": qty,
        "message": "Product added to DeviceMart guest cart",
        "cart": cart_data,
    }


def cart(session: RateLimitedSession) -> dict[str, Any]:
    response = session.get(CART_URL)
    if response.status_code != 200:
        raise RuntimeError(f"DeviceMart cart failed with HTTP {response.status_code}")

    soup = BeautifulSoup(response.text, "html.parser")
    body_text = soup.get_text(" ", strip=True)
    count_match = re.search(r"장바구니\s*(\d+)", body_text)
    count = _to_int(count_match.group(1)) if count_match else None

    items: list[dict[str, Any]] = []
    for checkbox in soup.select('input[name="cart_option_seq[]"]'):
        seq = checkbox.get("value")
        name = checkbox.get("gname")
        stat = checkbox.get("stat")
        row = checkbox.find_parent("tr")
        row_text = _clean(row.get_text(" ", strip=True)) if row else ""
        goods_seq = None
        qty = None
        unit_price = None
        subtotal = None
        if row:
            goods_input = row.find("input", {"name": "opt_goods_seq[]"})
            if goods_input:
                goods_seq = goods_input.get("value")
            qty_input = row.find("input", {"name": "optionEa[0]"})
            if qty_input:
                qty = _to_int(qty_input.get("value"))
            price_pair = re.search(r"([\d,]+)\s+([\d,]+)\s*원", row_text)
            if price_pair:
                unit_price = _to_int(price_pair.group(1))
                subtotal = _to_int(price_pair.group(2))
            else:
                price_match = re.search(r"([\d,]+)\s*원", row_text)
                if price_match:
                    subtotal = _to_int(price_match.group(1))
        items.append(
            {
                "cart_option_seq": seq,
                "goods_seq": goods_seq,
                "name": name,
                "quantity": qty,
                "price": subtotal,
                "unit_price": unit_price,
                "subtotal": subtotal,
                "available": stat == "Y",
                "row_text": row_text[:500],
            }
        )

    return {
        "site": "devicemart",
        "count": count if count is not None else len(items),
        "items": items,
        "url": response.url,
    }


def remove_from_cart(
    session: RateLimitedSession,
    cart_option_seq: str,
    confirm: bool = False,
) -> dict[str, Any]:
    if not confirm:
        raise ValueError("remove_from_cart_devicemart requires confirm=True")
    data = {
        "cart_version": "2",
        "nation": "KOREA",
        "kr_nation": "대한민국",
        "cart_input_json": "",
        "currency": "KRW",
        "currency_symbol": "원",
        "currency_symbol_position": "after",
        "cart_option_seq": str(cart_option_seq),
        "select_del": "selDel",
    }
    response = session.post(DEL_URL, data=data)
    if response.status_code != 200:
        raise RuntimeError(f"DeviceMart cart remove failed with HTTP {response.status_code}")
    return {
        "ok": True,
        "cart": cart(session),
    }
