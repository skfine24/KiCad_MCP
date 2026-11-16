"""BOM enrichment and MPN validation."""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path

from . import kicad_cli, paths

_ENRICH_FIELDS = (
    "Reference,Value,Footprint,${QUANTITY},${DNP},"
    "MPN,Manufacturer,Supplier,Supplier Part,Stock,Alternate"
)
_ENRICH_LABELS = "Refs,Value,Footprint,Qty,DNP,MPN,Manufacturer,Supplier,Supplier Part,Stock,Alternate"
_KEY_FIELDS = ("MPN", "Value")


def _read_csv(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    return list(reader)


def _write_csv(path: Path, rows: list[dict], fieldnames: list[str]) -> None:
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    paths.atomic_write_text(path, output.getvalue(), newline="")


def _load_database(path: str | Path) -> list[dict]:
    db_path = paths.normalize_path(path)
    if not db_path.is_file():
        raise FileNotFoundError(f"BOM database not found: {db_path}")
    if db_path.suffix.lower() == ".json":
        data = json.loads(db_path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data = data.get("parts", data.get("rows", []))
        return [dict(row) for row in data]
    return _read_csv(db_path)


def _db_index(rows: list[dict]) -> dict:
    index = {}
    for row in rows:
        for key in _KEY_FIELDS:
            value = str(row.get(key) or "").strip()
            if value:
                index.setdefault(value.lower(), row)
    return index


def bom_enrich(
    schematic_path: str | Path,
    output_path: str | Path | None = None,
    database_path: str | Path | None = None,
    exclude_power: bool = True,
    exclude_dnp: bool = False,
) -> dict:
    sch = paths.normalize_path(schematic_path)
    if not sch.is_file():
        raise FileNotFoundError(f"schematic not found: {sch}")
    out = paths.normalize_path(output_path) if output_path else sch.parent / f"{sch.stem}-bom-enriched.csv"
    export = kicad_cli.export_bom(
        sch,
        out,
        exclude_power=exclude_power,
        exclude_dnp=exclude_dnp,
        fields=_ENRICH_FIELDS,
        labels=_ENRICH_LABELS,
    )
    rows = _read_csv(out)
    fieldnames = list(rows[0].keys()) if rows else _ENRICH_LABELS.split(",")
    for column in ("MPN", "Manufacturer", "Supplier", "Supplier Part", "Stock", "Alternate"):
        if column not in fieldnames:
            fieldnames.append(column)
            for row in rows:
                row[column] = ""

    database_rows = _load_database(database_path) if database_path else []
    index = _db_index(database_rows)
    enriched = 0
    missing_mpn = []
    for row in rows:
        key = ""
        for field in _KEY_FIELDS:
            value = str(row.get(field) or "").strip()
            if value and value.lower() in index:
                key = value.lower()
                break
        if key:
            source = index[key]
            for column in ("MPN", "Manufacturer", "Supplier", "Supplier Part", "Stock", "Alternate"):
                value = str(source.get(column) or "").strip()
                if value and not str(row.get(column) or "").strip():
                    row[column] = value
                    enriched += 1
        if not str(row.get("MPN") or "").strip():
            missing_mpn.append(row.get("Refs") or row.get("Reference") or row.get("Value"))

    duplicate_mpns = _duplicate_mpns(rows)
    _write_csv(out, rows, fieldnames)
    return {
        "command": "bom_enrich",
        "output": str(out),
        "export": export,
        "row_count": len(rows),
        "enriched_field_count": enriched,
        "database_rows": len(database_rows),
        "missing_mpn": missing_mpn,
        "missing_mpn_count": len(missing_mpn),
        "duplicate_mpns": duplicate_mpns,
        "revision": paths.file_revision(out),
        "source_revision": paths.file_revision(sch),
    }


def _duplicate_mpns(rows: list[dict]) -> list[dict]:
    seen = {}
    for row in rows:
        mpn = str(row.get("MPN") or "").strip()
        if not mpn:
            continue
        seen.setdefault(mpn, []).append(row.get("Refs") or row.get("Reference") or row.get("Value"))
    return [
        {"mpn": mpn, "refs": refs}
        for mpn, refs in seen.items()
        if len(refs) > 1
    ]


def bom_validate_mpn(bom_path: str | Path) -> dict:
    path = paths.normalize_path(bom_path)
    if not path.is_file():
        raise FileNotFoundError(f"BOM not found: {path}")
    rows = _read_csv(path)
    if not rows:
        return {"ok": False, "reason": "empty BOM", "row_count": 0}
    if "MPN" not in rows[0]:
        return {
            "ok": False,
            "reason": "BOM has no MPN column; export with fields including ${MPN}",
            "row_count": len(rows),
            "columns": list(rows[0].keys()),
        }
    missing = []
    for row in rows:
        if not str(row.get("MPN") or "").strip():
            missing.append(row.get("Refs") or row.get("Reference") or row.get("Value"))
    duplicates = _duplicate_mpns(rows)
    return {
        "ok": not missing and not duplicates,
        "row_count": len(rows),
        "missing_mpn": missing,
        "missing_mpn_count": len(missing),
        "duplicate_mpns": duplicates,
        "duplicate_mpn_count": len(duplicates),
        "revision": paths.file_revision(path),
    }
