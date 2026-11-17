from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import requests

from ..http import RateLimitedSession

BASE = "https://api.digikey.com"
TOKEN_URL = f"{BASE}/v1/oauth2/token"
SEARCH_URL = f"{BASE}/products/v4/search/keyword"
PRODUCT_URL = f"{BASE}/products/v4/search/{{product_number}}/productdetails"
DEFAULT_CREDENTIALS_PATH = (
    Path.home() / ".config" / "opencode" / "parts-purchase" / "digikey.json"
)


def _credentials_path() -> Path:
    return Path(
        os.environ.get(
            "PARTS_MCP_DIGIKEY_CREDENTIALS",
            str(DEFAULT_CREDENTIALS_PATH),
        )
    )


def _load_credentials() -> dict[str, str]:
    path = _credentials_path()
    if not path.exists():
        raise RuntimeError(
            f"DigiKey credentials file not found: {path}. "
            "Create it with client_id, client_secret, and customer_id."
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"DigiKey credentials file could not be read: {path}") from exc
    client_id = str(data.get("client_id") or "").strip()
    client_secret = str(data.get("client_secret") or "").strip()
    customer_id = str(data.get("customer_id") or "").strip()
    if not client_id or not client_secret:
        raise RuntimeError(
            f"DigiKey credentials file is missing client_id or client_secret: {path}"
        )
    return {
        "client_id": client_id,
        "client_secret": client_secret,
        "customer_id": customer_id,
    }


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text or None


def _normalize_variation(variation: dict[str, Any]) -> dict[str, Any]:
    pricing: list[dict[str, Any]] = []
    for tier in variation.get("StandardPricing") or []:
        break_quantity = _to_int(tier.get("BreakQuantity"))
        unit_price = _to_float(tier.get("UnitPrice"))
        if break_quantity is None or unit_price is None:
            continue
        pricing.append(
            {
                "break_quantity": break_quantity,
                "unit_price": unit_price,
                "total_price": _to_float(tier.get("TotalPrice")),
            }
        )
    pricing.sort(key=lambda item: item["break_quantity"])
    return {
        "digikey_product_number": variation.get("DigiKeyProductNumber"),
        "package_type": (variation.get("PackageType") or {}).get("Name"),
        "minimum_order_quantity": _to_int(variation.get("MinimumOrderQuantity")) or 1,
        "standard_package": _to_int(variation.get("StandardPackage")) or 1,
        "quantity_available": _to_int(
            variation.get("QuantityAvailableforPackageType")
        )
        or 0,
        "pricing": pricing,
        "digi_reel_fee": _to_float(variation.get("DigiReelFee")) or 0.0,
        "marketplace": bool(variation.get("MarketPlace")),
        "tariff_active": bool(variation.get("TariffActive")),
    }


def _select_variation(variations: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not variations:
        return None
    return min(
        variations,
        key=lambda variation: (
            0 if variation["quantity_available"] > 0 else 1,
            variation["minimum_order_quantity"],
            variation["digi_reel_fee"],
        ),
    )


def _price_for_qty(variation: dict[str, Any] | None, qty: int) -> float | None:
    if not variation:
        return None
    pricing = variation.get("pricing") or []
    if not pricing:
        return None
    price = pricing[0]["unit_price"]
    for tier in pricing:
        if qty >= tier["break_quantity"]:
            price = tier["unit_price"]
        else:
            break
    return price


def _normalize_product(product: dict[str, Any], detail: bool = False) -> dict[str, Any]:
    variations = [
        _normalize_variation(variation)
        for variation in product.get("ProductVariations") or []
    ]
    selected = _select_variation(variations)
    quantity = _to_int(product.get("QuantityAvailable"))
    if quantity is None and variations:
        quantity = max(
            (variation["quantity_available"] for variation in variations),
            default=0,
        )
    discontinued = bool(product.get("Discontinued")) or bool(
        product.get("EndOfLife")
    )
    status = (product.get("ProductStatus") or {}).get("Status")
    available = bool(quantity and quantity > 0) and not discontinued
    description = (product.get("Description") or {}).get("ProductDescription")
    detailed_description = (product.get("Description") or {}).get(
        "DetailedDescription"
    )
    parameters: list[dict[str, Any]] = []
    if detail:
        for parameter in product.get("Parameters") or []:
            parameters.append(
                {
                    "name": parameter.get("ParameterText"),
                    "value": parameter.get("ValueText"),
                }
            )
    mpn = product.get("ManufacturerProductNumber")
    price = _to_float(product.get("UnitPrice"))
    if price is None and selected and selected["pricing"]:
        price = selected["pricing"][0]["unit_price"]
    selected_standard = selected.get("standard_package") if selected else None
    selected_moq = selected.get("minimum_order_quantity") if selected else None
    order_multiple = (
        selected_standard
        if selected_standard
        and selected_standard > 1
        and selected_standard == selected_moq
        else 1
    )
    return {
        "site": "digikey",
        "id": mpn,
        "mpn": mpn,
        "name": mpn or description,
        "description": description,
        "detailed_description": detailed_description,
        "manufacturer": (product.get("Manufacturer") or {}).get("Name"),
        "price": price,
        "currency": "KRW",
        "price_includes_vat": None,
        "stock": quantity,
        "available": available,
        "status": status,
        "discontinued": discontinued,
        "end_of_life": bool(product.get("EndOfLife")),
        "back_order_not_allowed": bool(product.get("BackOrderNotAllowed")),
        "normally_stocking": bool(product.get("NormallyStocking")),
        "url": product.get("ProductUrl"),
        "datasheet_url": product.get("DatasheetUrl"),
        "photo_url": product.get("PhotoUrl"),
        "variations": variations,
        "selected_variation": selected,
        "digikey_product_number": (
            selected.get("digikey_product_number") if selected else None
        ),
        "package_type": selected.get("package_type") if selected else None,
        "moq": selected.get("minimum_order_quantity") if selected else None,
        "order_multiple": order_multiple if selected else None,
        "price_tiers": selected.get("pricing") if selected else [],
        "parameters": parameters,
        "lead_weeks": product.get("ManufacturerLeadWeeks"),
        "rohs": (product.get("Classifications") or {}).get("RohsStatus"),
        "reach": (product.get("Classifications") or {}).get("ReachStatus"),
    }


def parse_product_number(value: str) -> str:
    text = (value or "").strip()
    if not text:
        raise ValueError("DigiKey product number is empty")
    if text.startswith("http://") or text.startswith("https://"):
        parsed = urlparse(text)
        parts = [unquote(part) for part in parsed.path.split("/") if part]
        if "detail" in parts:
            index = parts.index("detail")
            if len(parts) > index + 2:
                return parts[index + 2]
        if parts:
            return parts[-1]
    return text


class DigiKeySession:
    def __init__(self, min_interval: float = 0.6) -> None:
        self._http = RateLimitedSession(min_interval=min_interval)
        self._token: str | None = None
        self._token_expires_at = 0.0
        self._credentials: dict[str, str] | None = None

    def _get_credentials(self) -> dict[str, str]:
        if self._credentials is None:
            self._credentials = _load_credentials()
        return self._credentials

    def _ensure_token(self) -> str:
        if self._token and time.monotonic() < self._token_expires_at:
            return self._token
        credentials = self._get_credentials()
        response = self._http.post(
            TOKEN_URL,
            data={
                "client_id": credentials["client_id"],
                "client_secret": credentials["client_secret"],
                "grant_type": "client_credentials",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if response.status_code != 200:
            raise RuntimeError(
                f"DigiKey token request failed with HTTP {response.status_code}"
            )
        data = response.json()
        token = data.get("access_token")
        if not token:
            raise RuntimeError("DigiKey token response did not contain access_token")
        expires_in = _to_int(data.get("expires_in")) or 600
        self._token = token
        self._token_expires_at = time.monotonic() + max(60, expires_in - 60)
        return token

    def _headers(self) -> dict[str, str]:
        credentials = self._get_credentials()
        token = self._ensure_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "X-DIGIKEY-Client-Id": credentials["client_id"],
            "X-DIGIKEY-Locale-Site": "KR",
            "X-DIGIKEY-Locale-Language": "ko",
            "X-DIGIKEY-Locale-Currency": "KRW",
            "Content-Type": "application/json",
        }
        if credentials.get("customer_id"):
            headers["X-DIGIKEY-Customer-Id"] = credentials["customer_id"]
        return headers

    def search(self, query: str, limit: int = 10, offset: int = 0) -> dict[str, Any]:
        limit = max(1, min(int(limit), 50))
        offset = max(0, int(offset))
        response = self._http.post(
            SEARCH_URL,
            headers=self._headers(),
            json={"Keywords": query, "Limit": limit, "Offset": offset},
        )
        if response.status_code != 200:
            raise RuntimeError(
                f"DigiKey search failed with HTTP {response.status_code}"
            )
        data = response.json()
        items = [
            _normalize_product(product)
            for product in data.get("Products") or []
        ]
        exact_matches = [
            _normalize_product(product)
            for product in data.get("ExactMatches") or []
        ]
        return {
            "site": "digikey",
            "query": query,
            "total": data.get("ProductsCount"),
            "items": items,
            "exact_matches": exact_matches,
            "url": "https://www.digikey.kr/ko/products/results",
        }

    def product(self, product_number: str) -> dict[str, Any]:
        number = parse_product_number(product_number)
        url = PRODUCT_URL.format(
            product_number=requests.utils.quote(number, safe="")
        )
        response = self._http.get(url, headers=self._headers())
        if response.status_code != 200:
            try:
                search_result = self.search(number, limit=5)
            except Exception:
                search_result = {}
            candidates = (
                search_result.get("exact_matches")
                or search_result.get("items")
                or []
            )
            if candidates:
                return candidates[0]
            raise RuntimeError(
                f"DigiKey product {number} failed with HTTP {response.status_code}"
            )
        data = response.json()
        product = data.get("Product")
        if not product:
            raise RuntimeError(
                f"DigiKey product {number} response did not contain Product"
            )
        return _normalize_product(product, detail=True)

    def close(self) -> None:
        self._http.close()


def make_session() -> DigiKeySession:
    return DigiKeySession()


def search(
    session: DigiKeySession,
    query: str,
    limit: int = 10,
    offset: int = 0,
) -> dict[str, Any]:
    return session.search(query, limit, offset)


def product(session: DigiKeySession, product_number: str) -> dict[str, Any]:
    return session.product(product_number)
