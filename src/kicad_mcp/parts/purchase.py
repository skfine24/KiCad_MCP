from __future__ import annotations

from typing import Any

from .http import RateLimitedSession
from .sites import devicemart, digikey, icmart


def _to_int(value: Any, default: int = 1) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _round_up(value: int, step: int) -> int:
    if step <= 0:
        return value
    return ((value + step - 1) // step) * step


def _price_for_qty(
    discounts: list[dict[str, Any]] | None,
    base_price: int | None,
    qty: int,
) -> int | None:
    price = base_price
    for tier in discounts or []:
        min_qty = tier.get("min_qty") or 1
        max_qty = tier.get("max_qty")
        tier_price = tier.get("price")
        if tier_price is None:
            continue
        if qty >= min_qty and (max_qty is None or qty < max_qty):
            price = tier_price
    return price


def _order_qty(qty: int, moq: int | None, multiple: int | None) -> int:
    order_qty = max(qty, moq or 1)
    return _round_up(order_qty, multiple or 1)


def _item_name(item: dict[str, Any]) -> str:
    for key in ("name", "value", "mpn", "part", "description"):
        value = item.get(key)
        if value:
            return str(value).strip()
    raise ValueError("BOM item has no name/value/mpn field")


def _is_module(item: dict[str, Any]) -> bool:
    text = " ".join(
        str(item.get(key, ""))
        for key in ("name", "value", "mpn", "part", "description", "package")
    ).lower()
    if "module" in text or "모듈" in text:
        return True
    if "pico" in text:
        return True
    if any(token in text for token in ("vl53l0x", "vl53l1x", "vl53l5cx")):
        return True
    if "ws2812b" in text and ("strip" in text or "led strip" in text):
        return True
    return False


def _icmart_options(
    session: RateLimitedSession,
    item: dict[str, Any],
    qty: int,
    limit: int,
) -> list[dict[str, Any]]:
    options: list[dict[str, Any]] = []
    product_id = item.get("icmart_id") or item.get("icmart_product_id")
    if product_id:
        detail = icmart.product(session, str(product_id))
        stock = detail.get("stock")
        order_qty = qty
        unit_price = _price_for_qty(
            detail.get("discounts"), detail.get("price"), order_qty
        )
        subtotal = unit_price * order_qty if unit_price is not None else None
        available = (
            bool(detail.get("available"))
            and stock is not None
            and stock >= order_qty
        )
        options.append(
            {
                "site": "icmart",
                "id": detail.get("id"),
                "name": detail.get("name"),
                "manufacturer": detail.get("manufacturer"),
                "price": unit_price,
                "currency": "KRW",
                "price_includes_vat": True,
                "stock": stock,
                "bom_qty": qty,
                "order_qty": order_qty,
                "qty": order_qty,
                "subtotal": subtotal,
                "available": available,
                "moq": None,
                "url": detail.get("url"),
                "inquiry_url": detail.get("inquiry_url"),
                "note": "Direct IC Mart product id",
            }
        )
        return options

    query = _item_name(item)
    search_result = icmart.search(session, query, limit=max(1, min(limit, 5)))
    candidates = search_result.get("items", [])[: max(1, min(limit, 5))]
    for candidate in candidates:
        detail = icmart.product(session, str(candidate["id"]))
        stock = detail.get("stock")
        order_qty = qty
        unit_price = _price_for_qty(
            detail.get("discounts"), detail.get("price"), order_qty
        )
        subtotal = unit_price * order_qty if unit_price is not None else None
        available = (
            bool(detail.get("available"))
            and stock is not None
            and stock >= order_qty
        )
        options.append(
            {
                "site": "icmart",
                "id": detail.get("id"),
                "name": detail.get("name"),
                "manufacturer": detail.get("manufacturer"),
                "price": unit_price,
                "currency": "KRW",
                "price_includes_vat": True,
                "stock": stock,
                "bom_qty": qty,
                "order_qty": order_qty,
                "qty": order_qty,
                "subtotal": subtotal,
                "available": available,
                "moq": None,
                "url": detail.get("url"),
                "inquiry_url": detail.get("inquiry_url"),
                "note": "IC Mart search match; verify package/MPN",
            }
        )
    return options


def _devicemart_options(
    session: RateLimitedSession,
    item: dict[str, Any],
    qty: int,
    limit: int,
) -> list[dict[str, Any]]:
    options: list[dict[str, Any]] = []
    product_no = item.get("devicemart_no") or item.get("devicemart_product_no")
    if product_no:
        detail = devicemart.product(session, str(product_no))
        stock = detail.get("stock")
        moq = detail.get("moq") or 1
        multiple = detail.get("order_multiple") or 1
        order_qty = _order_qty(qty, moq, multiple)
        unit_price = _price_for_qty(
            detail.get("discounts"), detail.get("price"), order_qty
        )
        subtotal = unit_price * order_qty if unit_price is not None else None
        available = (
            bool(detail.get("available"))
            and stock is not None
            and stock >= order_qty
        )
        note = "Direct DeviceMart product no"
        if order_qty != qty:
            note += (
                f"; order qty adjusted from {qty} to {order_qty} "
                f"(MOQ {moq}, multiple {multiple})"
            )
        options.append(
            {
                "site": "devicemart",
                "id": detail.get("id"),
                "name": detail.get("name"),
                "manufacturer": detail.get("manufacturer"),
                "price": unit_price,
                "currency": "KRW",
                "price_vat_included": detail.get("price_vat_included"),
                "stock": stock,
                "bom_qty": qty,
                "order_qty": order_qty,
                "qty": order_qty,
                "subtotal": subtotal,
                "available": available,
                "moq": moq,
                "order_multiple": multiple,
                "url": detail.get("url"),
                "note": note,
            }
        )
        return options

    query = _item_name(item)
    search_result = devicemart.search(session, query, limit=max(1, min(limit, 10)))
    for candidate in search_result.get("items", [])[: max(1, min(limit, 10))]:
        stock = candidate.get("stock")
        moq = candidate.get("moq") or 1
        multiple = candidate.get("order_multiple") or 1
        order_qty = _order_qty(qty, moq, multiple)
        unit_price = candidate.get("price")
        subtotal = unit_price * order_qty if unit_price is not None else None
        available = (
            bool(candidate.get("available"))
            and stock is not None
            and stock >= order_qty
        )
        note = "DeviceMart search match; verify package/MPN"
        if order_qty != qty:
            note += (
                f"; order qty adjusted from {qty} to {order_qty} "
                f"(MOQ {moq}, multiple {multiple})"
            )
        options.append(
            {
                "site": "devicemart",
                "id": candidate.get("id"),
                "name": candidate.get("name"),
                "manufacturer": candidate.get("manufacturer"),
                "price": unit_price,
                "currency": "KRW",
                "stock": stock,
                "bom_qty": qty,
                "order_qty": order_qty,
                "qty": order_qty,
                "subtotal": subtotal,
                "available": available,
                "moq": moq,
                "order_multiple": multiple,
                "url": candidate.get("url"),
                "note": note,
            }
        )
    return options


def _digikey_price_for_qty(candidate: dict[str, Any], qty: int) -> float | None:
    tiers = candidate.get("price_tiers") or []
    if not tiers:
        return candidate.get("price")
    price = tiers[0].get("unit_price")
    for tier in tiers:
        break_quantity = tier.get("break_quantity") or 1
        if qty >= break_quantity:
            price = tier.get("unit_price")
        else:
            break
    return price


def _digikey_candidates(
    session: digikey.DigiKeySession,
    item: dict[str, Any],
    limit: int,
) -> list[dict[str, Any]]:
    product_number = (
        item.get("digikey_product_number")
        or item.get("digikey_pn")
        or item.get("digikey_mpn")
    )
    if product_number:
        return [digikey.product(session, str(product_number))]

    mpn = item.get("mpn")
    if mpn:
        try:
            return [digikey.product(session, str(mpn))]
        except Exception:
            pass

    query = str(mpn or _item_name(item))
    search_result = digikey.search(session, query, limit=max(1, min(limit, 10)))
    candidates = (
        search_result.get("exact_matches")
        or search_result.get("items")
        or []
    )
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for candidate in candidates[: max(1, min(limit, 10))]:
        key = str(candidate.get("digikey_product_number") or candidate.get("id"))
        if key in seen:
            continue
        seen.add(key)
        result.append(candidate)
    return result


def _digikey_options(
    session: digikey.DigiKeySession | None,
    item: dict[str, Any],
    qty: int,
    limit: int,
) -> list[dict[str, Any]]:
    if session is None:
        raise ValueError("DigiKey session is required for site='digikey'")
    options: list[dict[str, Any]] = []
    for candidate in _digikey_candidates(session, item, limit):
        selected_variation = candidate.get("selected_variation") or {}
        stock = selected_variation.get("quantity_available")
        if stock is None:
            stock = candidate.get("stock")
        moq = candidate.get("moq") or 1
        multiple = candidate.get("order_multiple") or 1
        order_qty = _order_qty(qty, moq, multiple)
        unit_price = _digikey_price_for_qty(candidate, order_qty)
        subtotal = unit_price * order_qty if unit_price is not None else None
        available = (
            bool(candidate.get("available"))
            and stock is not None
            and stock >= order_qty
        )
        if item.get("digikey_product_number") or item.get("digikey_pn"):
            note = "DigiKey product number"
        elif item.get("mpn"):
            note = "DigiKey exact MPN"
        else:
            note = "DigiKey search match; verify package/MPN"
        if order_qty != qty:
            note += (
                f"; order qty adjusted from {qty} to {order_qty} "
                f"(MOQ {moq}, multiple {multiple})"
            )
        options.append(
            {
                "site": "digikey",
                "id": candidate.get("id"),
                "digikey_product_number": candidate.get("digikey_product_number"),
                "name": candidate.get("name"),
                "description": candidate.get("description"),
                "manufacturer": candidate.get("manufacturer"),
                "price": unit_price,
                "currency": "KRW",
                "price_includes_vat": candidate.get("price_includes_vat"),
                "stock": stock,
                "bom_qty": qty,
                "order_qty": order_qty,
                "qty": order_qty,
                "subtotal": subtotal,
                "available": available,
                "moq": moq,
                "order_multiple": multiple,
                "package_type": candidate.get("package_type"),
                "status": candidate.get("status"),
                "url": candidate.get("url"),
                "datasheet_url": candidate.get("datasheet_url"),
                "note": note,
            }
        )
    return options


def build_purchase_list(
    items: list[dict[str, Any]],
    ic_session: RateLimitedSession,
    dm_session: RateLimitedSession,
    site: str = "digikey",
    limit: int = 5,
    include_modules: bool = False,
    dk_session: digikey.DigiKeySession | None = None,
) -> dict[str, Any]:
    site = site.lower().strip()
    if site not in {"digikey", "icmart", "devicemart", "both", "all"}:
        raise ValueError(
            "site must be 'digikey', 'icmart', 'devicemart', 'both', or 'all'"
        )

    results: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    total_items = len(items)
    processed_items = 0
    available_items = 0
    estimated_total = 0

    for item in items:
        name = _item_name(item)
        qty = _to_int(item.get("qty") or item.get("quantity"), 1)
        if qty < 1:
            qty = 1
        if not include_modules and _is_module(item):
            excluded.append(
                {
                    "name": name,
                    "qty": qty,
                    "reason": "Module excluded from purchase BOM",
                }
            )
            continue
        processed_items += 1
        options: list[dict[str, Any]] = []
        errors: list[str] = []

        if site in {"digikey", "all"}:
            try:
                options.extend(_digikey_options(dk_session, item, qty, limit))
            except Exception as exc:  # noqa: BLE001
                errors.append(f"digikey: {exc}")

        if site in {"icmart", "both", "all"}:
            try:
                options.extend(_icmart_options(ic_session, item, qty, limit))
            except Exception as exc:  # noqa: BLE001
                errors.append(f"icmart: {exc}")

        if site in {"devicemart", "both", "all"}:
            try:
                options.extend(_devicemart_options(dm_session, item, qty, limit))
            except Exception as exc:  # noqa: BLE001
                errors.append(f"devicemart: {exc}")

        options.sort(
            key=lambda option: (
                0 if option.get("available") else 1,
                option.get("subtotal") if option.get("subtotal") is not None else 10**18,
            )
        )
        recommended = next(
            (option for option in options if option.get("available")),
            None,
        )
        if recommended:
            available_items += 1
            if recommended.get("subtotal"):
                estimated_total += int(recommended["subtotal"])

        results.append(
            {
                "name": name,
                "qty": qty,
                "mpn": item.get("mpn"),
                "package": item.get("package"),
                "options": options,
                "recommended": recommended,
                "available": recommended is not None,
                "errors": errors,
            }
        )

    return {
        "site": site,
        "items": results,
        "excluded": excluded,
        "summary": {
            "total_items": total_items,
            "excluded_items": len(excluded),
            "processed_items": processed_items,
            "available_items": available_items,
            "unavailable_items": processed_items - available_items,
            "estimated_total_krw": estimated_total,
        },
    }
