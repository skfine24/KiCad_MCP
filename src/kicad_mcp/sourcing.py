"""Schematic-to-orderable-BOM sourcing with LCSC.

Reads a KiCad schematic, groups components by value/footprint/MPN, searches
LCSC for each group, classifies availability against the BOM quantity, and can
write the selected LCSC part metadata back to the schematic symbols.

The goal is a PCB design flow where every placed part has an orderable LCSC
part number before layout starts.
"""

from __future__ import annotations

import csv
import io
import re
from pathlib import Path
from typing import Any

from . import board_parser, paths, pcb_edit, sch_edit, sexpr
from .parts.sites import lcsc

_LCSC_PROPERTIES = (
    "LCSC",
    "LCSC Part",
    "LCSC Part #",
    "LCSC Part Number",
    "Supplier Part",
    "SupplierPart",
)
_MPN_PROPERTIES = (
    "MPN",
    "Manufacturer Part Number",
    "Mfr Part",
    "Mfr. Part",
    "Part Number",
)
_PASSIVE_RE = re.compile(
    r"^\s*\d+(?:\.\d+)?\s*(?:pF|nF|uF|µF|mF|F|Ω|ohm|k|M|mH|uH|µH|H|V|A|W)\s*$",
    re.IGNORECASE,
)
_GENERIC_NAME_RE = re.compile(
    r"^(?:BTN\d*|SW\d*|LED(?:_\w+)?|BUZZER(?:_\w+)?|POWER(?:_\w+)?|BAT|OLED|BZ\d*)$",
    re.IGNORECASE,
)
_STANDARD_SIZES = ("0402", "0603", "0805", "1206", "1210", "1812", "2010", "2512")


def _flag(node: list, key: str, default: bool = False) -> bool:
    child = sexpr.get_first(node, key)
    if not child:
        return default
    if len(child) == 1:
        return True
    return str(child[1]).lower() in ("yes", "true", "1")


def _normalize(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _value_key(value: Any) -> str:
    """Keep digits and dots so 4.7k and 47k stay distinct."""
    return re.sub(r"[^0-9a-z.]", "", str(value or "").lower())


def _package_from_footprint(footprint: str | None) -> str | None:
    if not footprint:
        return None
    text = str(footprint).split(":")[-1]
    for size in _STANDARD_SIZES:
        if re.search(rf"(?<!\d){size}(?!\d)", text):
            return size
    low = text.lower()
    for token, label in (
        ("sot-23-6", "SOT-23-6"),
        ("sot-23-5", "SOT-23-5"),
        ("sot-23-3", "SOT-23-3"),
        ("sot-23", "SOT-23"),
        ("sot-363", "SOT-363"),
        ("sc-70-6", "SC-70-6"),
        ("sc-70", "SC-70"),
        ("tsot-23-6", "TSOT-23-6"),
        ("tsot-23", "TSOT-23"),
        ("qfn", "QFN"),
        ("qfp", "QFP"),
        ("dip", "DIP"),
        ("soic", "SOIC"),
        ("usb_c", "USB-C"),
        ("usb-c", "USB-C"),
        ("jst_xh", "JST XH 2.5mm"),
        ("pinheader_2.54", "2.54mm"),
        ("led_d3.0", "3mm"),
        ("sw_push_6mm", "6mm"),
        ("buzzer_12x9.5", "12x9.5"),
        ("push_lock_5.8x5.8", "5.8x5.8 6P"),
        ("yk16s", "14P"),
    ):
        if token in low:
            return label
    return None


def _existing_lcsc(properties: dict[str, str]) -> str | None:
    for name in _LCSC_PROPERTIES:
        value = properties.get(name)
        if value:
            match = re.search(r"C\d{4,}", value, re.IGNORECASE)
            if match:
                return match.group(0).upper()
    return None


def _existing_mpn(properties: dict[str, str]) -> str | None:
    for name in _MPN_PROPERTIES:
        value = properties.get(name)
        if value:
            return value.strip()
    return None


def _looks_like_mpn(value: str) -> bool:
    return (
        bool(re.search(r"[A-Za-z]", value))
        and bool(re.search(r"\d", value))
        and len(value) >= 4
        and not _PASSIVE_RE.match(value)
    )


def _value_as_mpn(value: str) -> str | None:
    if not value:
        return None
    if re.fullmatch(r"C\d{4,}", value, re.IGNORECASE):
        return None
    if _PASSIVE_RE.match(value) or _GENERIC_NAME_RE.match(value):
        return None
    return value if _looks_like_mpn(value) else None


def _base_mpn(mpn: str) -> str:
    if re.fullmatch(r"YK16S_[LR]", mpn, re.IGNORECASE):
        return "YK16S"
    return mpn


def _mpn_matches(candidate_model: str | None, mpn: str) -> bool:
    candidate = _normalize(candidate_model)
    target = _normalize(_base_mpn(mpn))
    if not candidate or not target:
        return False
    return candidate == target or candidate.startswith(target) or target.startswith(candidate)


def _is_passive_footprint(footprint: str) -> bool:
    low = footprint.lower()
    return any(token in low for token in ("resistor", "capacitor", "inductor"))


def _candidate_matches_group(candidate: dict[str, Any], group: dict[str, Any]) -> bool:
    text = " ".join(
        str(candidate.get(key) or "")
        for key in ("catalog", "description", "model")
    ).lower()
    value_key = _value_key(group["value"])
    if value_key and value_key not in _value_key(text):
        return False
    package = group["package"]
    if package and package.lower() in _STANDARD_SIZES:
        if package.lower() not in text:
            return False
    footprint = group["footprint"].lower()
    if "resistor" in footprint:
        if "resistor" not in text and " res " not in text:
            return False
    elif "capacitor" in footprint:
        if "capacitor" not in text and " cap " not in text:
            return False
    elif "inductor" in footprint:
        if "inductor" not in text and " ind " not in text:
            return False
    return True


def _read_components(schematic_path: str | Path) -> tuple[Path, list[dict[str, Any]]]:
    sch = paths.normalize_path(schematic_path)
    if not sch.is_file():
        raise FileNotFoundError(f"schematic not found: {sch}")
    if sch.suffix.lower() != ".kicad_sch":
        raise ValueError(f"expected .kicad_sch file, got: {sch.name}")
    tree = sexpr.load_file(sch)
    if not tree or tree[0] != "kicad_sch":
        raise ValueError(f"not a kicad_sch file: {sch.name}")

    components: list[dict[str, Any]] = []
    for node in sexpr.get_all(tree, "symbol"):
        lib_id = sexpr.get_value(node, "lib_id")
        if not lib_id:
            continue
        properties: dict[str, str] = {}
        for prop in sexpr.get_all(node, "property"):
            if len(prop) > 2:
                properties[str(prop[1])] = str(prop[2])
        ref = (properties.get("Reference") or "").strip()
        if not ref or ref.startswith("#"):
            continue
        if (
            _flag(node, "dnp")
            or not _flag(node, "in_bom", default=True)
            or not _flag(node, "on_board", default=True)
        ):
            continue
        components.append(
            {
                "reference": ref,
                "value": (properties.get("Value") or "").strip(),
                "footprint": (properties.get("Footprint") or "").strip(),
                "lib_id": lib_id,
                "properties": properties,
            }
        )
    return sch, components


def _group_components(components: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for component in components:
        value = component["value"]
        lcsc_code = _existing_lcsc(component["properties"])
        if not lcsc_code and re.fullmatch(r"C\d{4,}", value, re.IGNORECASE):
            lcsc_code = value.upper()
        mpn = _existing_mpn(component["properties"]) or _value_as_mpn(value)
        key = (
            value.lower(),
            component["footprint"].lower(),
            lcsc_code or "",
            mpn.lower() if mpn else "",
        )
        group = groups.get(key)
        if group is None:
            group = {
                "value": value,
                "footprint": component["footprint"],
                "package": _package_from_footprint(component["footprint"]),
                "lcsc_code": lcsc_code,
                "mpn": mpn,
                "refs": [],
                "lib_ids": [],
            }
            groups[key] = group
        group["refs"].append(component["reference"])
        if component["lib_id"] not in group["lib_ids"]:
            group["lib_ids"].append(component["lib_id"])
    return list(groups.values())


def _query_for_group(group: dict[str, Any]) -> str:
    if group["lcsc_code"]:
        return group["lcsc_code"]
    if group["mpn"]:
        return _base_mpn(group["mpn"])

    value = group["value"]
    package = group["package"]
    if re.fullmatch(r"YK16S_[LR]", value, re.IGNORECASE):
        value = "YK16S"
    if not value:
        if package:
            return package
        return group["lib_ids"][0].split(":")[-1] if group["lib_ids"] else ""

    if _PASSIVE_RE.match(value):
        return f"{value} {package}" if package else value
    if re.fullmatch(r"C\d{4,}", value, re.IGNORECASE):
        return value
    if _GENERIC_NAME_RE.match(value):
        return package or value
    if value.lower() == "usb-c":
        return "USB-C"
    if package and not _looks_like_mpn(value):
        return f"{value} {package}"
    return value


def _catalog_ids_for_group(group: dict[str, Any]) -> list[int] | None:
    footprint = group["footprint"].lower()
    if "resistor" in footprint:
        return [1199]  # Chip Resistor - Surface Mount
    if "capacitor" in footprint:
        return [1142]  # Ceramic Capacitors
    if "inductor" in footprint:
        return [1181]  # Fixed Inductors
    return None


def _search_group(
    session: lcsc.RateLimitedSession,
    group: dict[str, Any],
    limit: int,
) -> tuple[list[dict[str, Any]], str | None]:
    query = _query_for_group(group)
    if not query:
        return [], "no search query (empty value/footprint)"

    if group["lcsc_code"]:
        try:
            return [lcsc.product(session, group["lcsc_code"])], None
        except Exception as exc:  # noqa: BLE001
            try:
                result = lcsc.search(
                    session,
                    query,
                    limit,
                    catalog_ids=_catalog_ids_for_group(group),
                )
                return result.get("items", [])[:limit], f"product lookup failed: {exc}"
            except Exception as fallback_exc:  # noqa: BLE001
                return [], f"{exc}; fallback failed: {fallback_exc}"

    try:
        result = lcsc.search(
            session,
            query,
            limit,
            catalog_ids=_catalog_ids_for_group(group),
        )
    except Exception as exc:  # noqa: BLE001
        return [], str(exc)
    return result.get("items", [])[:limit], None


def _select_candidate(
    candidates: list[dict[str, Any]],
    group: dict[str, Any],
) -> tuple[dict[str, Any] | None, str, str]:
    qty = len(group["refs"])
    selected: dict[str, Any] | None = None
    status = "not_found"
    note = ""

    if group["lcsc_code"]:
        exact = [
            candidate
            for candidate in candidates
            if _normalize(candidate.get("code")) == _normalize(group["lcsc_code"])
        ]
        if exact:
            selected = exact[0]
            status = "orderable"
            note = "exact LCSC code"
        elif candidates:
            selected = candidates[0]
            status = "needs_review"
            note = f"LCSC code {group['lcsc_code']} not returned"
        else:
            note = f"LCSC code {group['lcsc_code']} not found"
    elif group["mpn"]:
        exact = [
            candidate
            for candidate in candidates
            if _mpn_matches(candidate.get("model"), group["mpn"])
        ]
        if exact:
            selected = exact[0]
            status = "orderable"
            note = "exact/prefix MPN"
        elif candidates:
            selected = candidates[0]
            status = "needs_review"
            note = "MPN not exact; review candidate"
        else:
            note = f"MPN {group['mpn']} not found"
    elif not _is_passive_footprint(group["footprint"]):
        selected = candidates[0] if candidates else None
        status = "needs_review" if selected else "not_found"
        note = "non-passive generic value; add MPN/LCSC" if selected else "no candidates"
    else:
        matches = [
            candidate
            for candidate in candidates
            if _candidate_matches_group(candidate, group)
        ]
        if matches:
            available = [
                candidate
                for candidate in matches
                if candidate.get("stock") is not None and candidate["stock"] >= qty
            ]
            selected = available[0] if available else matches[0]
            status = "orderable" if selected["stock"] >= qty else "low_stock"
            note = "value/package/type match"
        else:
            selected = candidates[0] if candidates else None
            status = "needs_review" if selected else "not_found"
            note = "no value/package/type match"

    if selected is not None:
        if selected.get("pre_sale") or selected.get("forbid"):
            status = "not_orderable"
            note = "pre-sale or forbidden part"
        elif selected.get("stock") is None or selected["stock"] <= 0:
            status = "not_orderable"
            note = "no stock"
        elif selected["stock"] < qty:
            status = "low_stock"
            note = f"stock {selected['stock']} < BOM qty {qty}"

    return selected, status, note


def _properties_for_candidate(candidate: dict[str, Any]) -> dict[str, str]:
    properties = {
        "LCSC": str(candidate.get("code") or ""),
        "MPN": str(candidate.get("model") or ""),
        "Manufacturer": str(candidate.get("brand") or ""),
        "Supplier": "LCSC",
        "Supplier Part": str(candidate.get("code") or ""),
        "Stock": str(candidate.get("stock") if candidate.get("stock") is not None else ""),
    }
    if candidate.get("pdf_url"):
        properties["Datasheet"] = str(candidate["pdf_url"])
    return properties


def _read_pcb_components(pcb_path: str | Path) -> tuple[Path, list[dict[str, Any]]]:
    pcb = paths.normalize_path(pcb_path)
    if not pcb.is_file():
        raise FileNotFoundError(f"PCB not found: {pcb}")
    if pcb.suffix.lower() != ".kicad_pcb":
        raise ValueError(f"expected .kicad_pcb file, got: {pcb.name}")
    data = board_parser.read_board(pcb)
    components: list[dict[str, Any]] = []
    for footprint in data.get("footprints") or []:
        ref = (footprint.get("reference") or "").strip()
        if not ref or ref.startswith("H"):
            continue
        if str(footprint.get("exclude_from_pos_files") or "").lower() in ("yes", "true"):
            continue
        lib_id = footprint.get("lib_id") or ""
        components.append(
            {
                "reference": ref,
                "value": (footprint.get("value") or "").strip(),
                "footprint": lib_id,
                "lib_id": lib_id,
                "properties": footprint.get("properties") or {},
            }
        )
    return pcb, components


def _price_for_qty(tiers: list[dict[str, Any]] | None, qty: int) -> float | None:
    price = None
    for tier in tiers or []:
        if qty >= (tier.get("min_qty") or 1):
            price = tier.get("price")
    return price


def _analyze_groups(
    groups: list[dict[str, Any]],
    limit: int,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    session = lcsc.make_session()
    search_limit = max(1, min(int(limit), 100))
    items: list[dict[str, Any]] = []
    counts = {
        "orderable": 0,
        "needs_review": 0,
        "low_stock": 0,
        "not_found": 0,
        "not_orderable": 0,
    }
    for group in groups:
        qty = len(group["refs"])
        candidates, error = _search_group(session, group, search_limit)
        selected, status, note = _select_candidate(candidates, group)
        moq = selected.get("min_qty") if selected else None
        order_qty = max(qty, moq or 1)
        unit_price = _price_for_qty(
            selected.get("price_tiers") if selected else None,
            order_qty,
        )
        subtotal = unit_price * order_qty if unit_price is not None else None
        counts[status] = counts.get(status, 0) + 1
        items.append(
            {
                "refs": group["refs"],
                "qty": qty,
                "value": group["value"],
                "footprint": group["footprint"],
                "package": group["package"],
                "lib_ids": group["lib_ids"],
                "query": _query_for_group(group),
                "status": status,
                "selected": selected,
                "alternatives": candidates[:5],
                "moq": moq,
                "order_qty": order_qty,
                "subtotal": subtotal,
                "currency": "USD" if selected else None,
                "applied": False,
                "note": note,
                "error": error,
            }
        )
    return items, counts


def prepare_orderable_bom(
    schematic_path: str | Path,
    limit: int = 100,
    apply: bool = False,
    strict: bool = False,
    allow_generic: bool = False,
    backup: bool = True,
    dry_run: bool = False,
    expected_revision: str | None = None,
) -> dict[str, Any]:
    """Search LCSC for every schematic component group and optionally apply part metadata.

    ``strict=True`` returns a blocked report without writing when any group is
    not exactly orderable. ``apply=True`` writes LCSC/MPN/Manufacturer/Supplier/
    Supplier Part/Stock properties to the schematic in one save. Generic-value
    matches stay ``needs_review`` and are only applied with ``allow_generic=True``.
    """
    sch, components = _read_components(schematic_path)
    groups = _group_components(components)
    items, counts = _analyze_groups(groups, limit)
    updates: list[dict[str, Any]] = []

    if apply:
        for item in items:
            selected = item["selected"]
            if selected is None:
                continue
            qty = item["qty"]
            can_apply = item["status"] == "orderable" or (
                allow_generic
                and item["status"] == "needs_review"
                and selected.get("stock") is not None
                and selected["stock"] >= qty
            )
            if can_apply:
                properties = _properties_for_candidate(selected)
                updates.extend(
                    {"ref": ref, "properties": properties}
                    for ref in item["refs"]
                )
                item["applied"] = True

    all_orderable = counts["orderable"] == len(groups)
    blocked = None
    if strict and not all_orderable:
        blocked = (
            f"strict mode: {len(groups) - counts['orderable']} of {len(groups)} "
            "component groups are not exactly orderable; schematic was not modified"
        )

    apply_result: dict[str, Any] | None = None
    if apply and updates and not blocked:
        apply_result = sch_edit.sch_set_properties(
            sch,
            updates,
            backup=backup,
            dry_run=dry_run,
            expected_revision=expected_revision,
        )

    return {
        "schematic": str(sch),
        "applied": bool(apply_result and not blocked),
        "strict": strict,
        "allow_generic": allow_generic,
        "blocked": blocked,
        "summary": {
            "components": len(components),
            "groups": len(groups),
            "orderable": counts["orderable"],
            "needs_review": counts["needs_review"],
            "low_stock": counts["low_stock"],
            "not_found": counts["not_found"],
            "not_orderable": counts["not_orderable"],
            "all_orderable": all_orderable,
            "planned_updates": len(updates),
            "applied_updates": apply_result.get("updated", 0) if apply_result else 0,
        },
        "apply_result": apply_result,
        "items": items,
    }


def check_orderable_pcb_bom(
    pcb_path: str | Path,
    limit: int = 100,
) -> dict[str, Any]:
    """Read-only LCSC sourcing report for a PCB (no PCB writes).

    Uses the footprint Value/Footprint and optional LCSC/MPN fields. Mounting
    holes and footprints excluded from POS files are skipped.
    """
    pcb, components = _read_pcb_components(pcb_path)
    groups = _group_components(components)
    items, counts = _analyze_groups(groups, limit)
    all_orderable = counts["orderable"] == len(groups)
    return {
        "pcb": str(pcb),
        "summary": {
            "components": len(components),
            "groups": len(groups),
            "orderable": counts["orderable"],
            "needs_review": counts["needs_review"],
            "low_stock": counts["low_stock"],
            "not_found": counts["not_found"],
            "not_orderable": counts["not_orderable"],
            "all_orderable": all_orderable,
        },
        "items": items,
    }


def _bom_field(row: dict[str, str], names: tuple[str, ...]) -> str | None:
    for name in names:
        for key, value in row.items():
            if key is None:
                continue
            if key.strip().lower() == name.lower() and str(value or "").strip():
                return str(value).strip()
    return None


def apply_lcsc_bom_to_schematic(
    schematic_path: str | Path,
    bom_path: str | Path,
    backup: bool = True,
    dry_run: bool = False,
    expected_revision: str | None = None,
) -> dict[str, Any]:
    """Copy LCSC/MPN/Manufacturer metadata from a BOM file onto schematic symbols.

    Accepts JLCPCB/LCSC BOM columns such as ``Designator``/``Refs`` and
    ``LCSC Part #``/``LCSC Part Number``. The template format with
    ``Customer Part Number(optional)`` as the designator column is also
    supported. All matched symbols are updated in one save.
    """
    sch, components = _read_components(schematic_path)
    known_refs = {component["reference"] for component in components}
    bom = paths.normalize_path(bom_path)
    if not bom.is_file():
        raise FileNotFoundError(f"BOM not found: {bom}")
    rows = list(csv.DictReader(io.StringIO(bom.read_text(encoding="utf-8-sig"))))
    updates: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    seen: set[str] = set()

    for index, row in enumerate(rows, start=1):
        designators = _bom_field(
            row,
            (
                "Designator",
                "Refs",
                "Reference",
                "Ref",
                "Customer Part Number(optional)",
                "Customer Part Number",
            ),
        )
        code_text = _bom_field(
            row,
            (
                "LCSC Part #",
                "LCSC Part Number",
                "LCSC Part Number(optional)",
                "LCSC",
                "Supplier Part",
            ),
        )
        if not designators or not code_text:
            skipped.append(
                {
                    "row": index,
                    "reason": "missing designator or LCSC part number",
                }
            )
            continue
        match = re.search(r"C\d{4,}", code_text, re.IGNORECASE)
        if not match:
            skipped.append(
                {
                    "row": index,
                    "refs": designators,
                    "reason": f"invalid LCSC part number: {code_text}",
                }
            )
            continue
        code = match.group(0).upper()
        mpn = _bom_field(
            row,
            (
                "MPN",
                "Manufacture Part Number",
                "Manufacturer Part Number",
                "Mfr Part",
                "Part Number",
            ),
        )
        manufacturer = _bom_field(
            row,
            ("Manufacturer", "Manufacturer(optional)", "Brand"),
        )

        for ref in re.split(r"[,;/\\\s]+", designators):
            ref = ref.strip()
            if not ref or ref in seen:
                continue
            if ref not in known_refs:
                skipped.append(
                    {"row": index, "ref": ref, "reason": "not in schematic"}
                )
                continue
            properties = {
                "LCSC": code,
                "Supplier": "LCSC",
                "Supplier Part": code,
            }
            if mpn:
                properties["MPN"] = mpn
            if manufacturer:
                properties["Manufacturer"] = manufacturer
            updates.append({"ref": ref, "properties": properties})
            seen.add(ref)

    result: dict[str, Any] | None = None
    if updates:
        result = sch_edit.sch_set_properties(
            sch,
            updates,
            backup=backup,
            dry_run=dry_run,
            expected_revision=expected_revision,
        )
    return {
        "schematic": str(sch),
        "bom": str(bom),
        "rows": len(rows),
        "planned_updates": len(updates),
        "updated": result.get("updated", 0) if result else 0,
        "skipped": skipped,
        "result": result,
    }


def apply_lcsc_bom_to_pcb(
    pcb_path: str | Path,
    bom_path: str | Path,
    backup: bool = True,
    dry_run: bool = False,
    expected_revision: str | None = None,
) -> dict[str, Any]:
    """Copy LCSC/MPN/Manufacturer metadata from a BOM file onto PCB footprints.

    Accepts the same BOM columns as ``apply_lcsc_bom_to_schematic``. Each
    matched footprint gets LCSC, Supplier, Supplier Part and, when present,
    MPN, Manufacturer and Stock fields. All footprints are updated in one
    pcbnew batch save.
    """
    pcb, components = _read_pcb_components(pcb_path)
    known_refs = {component["reference"] for component in components}
    bom = paths.normalize_path(bom_path)
    if not bom.is_file():
        raise FileNotFoundError(f"BOM not found: {bom}")
    rows = list(csv.DictReader(io.StringIO(bom.read_text(encoding="utf-8-sig"))))
    operations: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    seen: set[str] = set()

    for index, row in enumerate(rows, start=1):
        designators = _bom_field(
            row,
            (
                "Designator",
                "Refs",
                "Reference",
                "Ref",
                "Customer Part Number(optional)",
                "Customer Part Number",
            ),
        )
        code_text = _bom_field(
            row,
            (
                "LCSC Part #",
                "LCSC Part Number",
                "LCSC Part Number(optional)",
                "LCSC",
                "Supplier Part",
            ),
        )
        if not designators or not code_text:
            skipped.append(
                {
                    "row": index,
                    "reason": "missing designator or LCSC part number",
                }
            )
            continue
        match = re.search(r"C\d{4,}", code_text, re.IGNORECASE)
        if not match:
            skipped.append(
                {
                    "row": index,
                    "refs": designators,
                    "reason": f"invalid LCSC part number: {code_text}",
                }
            )
            continue
        code = match.group(0).upper()
        mpn = _bom_field(
            row,
            (
                "MPN",
                "Manufacture Part Number",
                "Manufacturer Part Number",
                "Mfr Part",
                "Part Number",
            ),
        )
        manufacturer = _bom_field(
            row,
            ("Manufacturer", "Manufacturer(optional)", "Brand"),
        )
        stock = _bom_field(row, ("Stock", "Stock(optional)"))

        for ref in re.split(r"[,;/\\\s]+", designators):
            ref = ref.strip()
            if not ref or ref in seen:
                continue
            if ref not in known_refs:
                skipped.append({"row": index, "ref": ref, "reason": "not in PCB"})
                continue
            fields = {
                "LCSC": code,
                "Supplier": "LCSC",
                "Supplier Part": code,
            }
            if mpn:
                fields["MPN"] = mpn
            if manufacturer:
                fields["Manufacturer"] = manufacturer
            if stock:
                fields["Stock"] = stock
            operations.append(
                {"kind": "set_footprint_fields", "ref": ref, "fields": fields}
            )
            seen.add(ref)

    result: dict[str, Any] | None = None
    if operations:
        result = pcb_edit.pcb_batch(
            pcb,
            operations,
            backup=backup,
            dry_run=dry_run,
            expected_revision=expected_revision,
        )
    updated = (
        len(operations)
        if result and result.get("ok") and not result.get("dry_run")
        else 0
    )
    return {
        "pcb": str(pcb),
        "bom": str(bom),
        "rows": len(rows),
        "planned_updates": len(operations),
        "updated": updated,
        "skipped": skipped,
        "result": result,
    }
