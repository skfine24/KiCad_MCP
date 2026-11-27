"""JLCPCB-ready CPL (component placement list) generation and verification.

A CPL row is correct only when it agrees with the board on four things:

* designator and side;
* mid X/Y in the assembly house's coordinate frame;
* rotation after the bottom-side mirror and the vendor part-library correction;
* for polarized parts, the rotation that puts pin 1 (cathode / positive) on
  the pad that the schematic assigned to pin 1.

This module generates the JLCPCB CSV used by the manufacturing profile and
verifies an existing CPL (ours or a supplier's corrected file) against the
PCB, the BOM and, when available, the schematic netlist.  It also emits a
polarity audit that reports the pin-1 net and the pad-1 position in the same
bottom-view frame as the assembly house's 2D preview.
"""

from __future__ import annotations

import csv
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path

from . import kicad_cli, paths, sch_parser, sexpr

# JLCPCB rotation corrections, in the order they are tried (first match wins).
# Values are degrees added to the KiCad rotation (after the bottom-side mirror).
# Source: Fabrication Toolkit plugins/transformations.csv (the plugin JLCPCB
# recommends), plus the USB-C entry validated by JLC-KiCad-Tools and KiBot.
ROTATION_DB: list[tuple[str, float]] = [
    (r"^USB_C_Receptacle_HRO_TYPE-C-31-M-12", 180.0),
    (r"^CP_EIA-", 180.0),
    (r"^CP_Elec_", 180.0),
    (r"^C_Elec_", 180.0),
    (r"^DFN-", 270.0),
    (r"^D_SOT-23", 180.0),
    (r"^HTSSOP-", 270.0),
    (r"^JST_GH_SM", 180.0),
    (r"^JST_PH_S", 180.0),
    (r"^LQFP-", 270.0),
    (r"^MSOP-", 270.0),
    (r"^PowerPAK_SO-8_Single", 270.0),
    (r"^QFN-", 90.0),
    (r"^qfn-", 90.0),
    (r"^R_Array_Concave_", 90.0),
    (r"^R_Array_Convex_", 90.0),
    (r"^SC-74-6", 180.0),
    (r"^SOIC-", 270.0),
    (r"^SOIC127P798X216-8N", -90.0),
    (r"^SOP-(?!18_)", 270.0),
    (r"^SOP-18_", 0.0),
    (r"^SOP-4_", 0.0),
    (r"^SOT-143", 180.0),
    (r"^SOT-223", 180.0),
    (r"^SOT-23", 180.0),
    (r"^SOT-353", 180.0),
    (r"^SOT-363", 180.0),
    (r"^SOT-89", 180.0),
    (r"^SSOP-", 270.0),
    (r"^SW_SPST_B3", 90.0),
    (r"^TDSON-8-1", 270.0),
    (r"^TO-277", 90.0),
    (r"^TQFP-", 270.0),
    (r"^TSOT-23", 180.0),
    (r"^TSSOP-", 270.0),
    (r"^UDFN-10", 270.0),
    (r"^USON-10", 270.0),
    (r"^VSON-8_", 270.0),
    (r"^VSSOP-10_", 270.0),
    (r"^VSSOP-8_3\.0x3\.0mm_P0\.65mm", 270.0),
    (r"^VSSOP-8_", 180.0),
]

LCSC_FIELDS = [
    "LCSC Part #", "LCSC Part", "LCSC PN", "LCSC P/N", "LCSC Part No.",
    "LCSC Part Number", "JLCPCB Part #", "JLCPCB Part", "JLCPCB PN",
    "JLCPCB P/N", "JLCPCB Part No.", "JLCPCB Part Number",
    "LCSC", "JLC",
]
MPN_FIELDS = ["MPN", "Mpn", "mpn"]
ROTATION_FIELDS = ["FT Rotation Offset", "Rotation Offset", "RotOffset"]
POSITION_FIELDS = ["FT Position Offset", "Position Offset", "PosOffset"]

LCSC_RE = re.compile(r"^C\d+$", re.IGNORECASE)

# Footprints that carry a pin-1 / polarity orientation.  The audit reports
# every match; polarized parts (diode/LED/capacitor) are the ones that must be
# checked in the assembly preview when the rotation DB has no entry.
POLARITY_PATTERNS: list[tuple[str, str, str]] = [
    (r"^D_", "diode", "cathode (pin 1)"),
    (r"^LED_", "led", "cathode (pin 1)"),
    (r"^CP_", "capacitor", "positive (pin 1)"),
    (r"^C_Elec_", "capacitor", "positive (pin 1)"),
    (r"^C_Tantalum", "capacitor", "positive (pin 1)"),
    (r"^SOT-23", "transistor", "pin 1"),
    (r"^SOT-353", "transistor", "pin 1"),
    (r"^SOT-363", "transistor", "pin 1"),
    (r"^SOT-563", "transistor", "pin 1"),
    (r"^SOT-89", "transistor", "pin 1"),
    (r"^SOT-223", "transistor", "pin 1"),
    (r"^TO-", "transistor", "pin 1"),
    (r"^QFN-", "ic", "pin 1"),
    (r"^DFN-", "ic", "pin 1"),
    (r"^USON-", "ic", "pin 1"),
    (r"^VSON-", "ic", "pin 1"),
    (r"^SOIC-", "ic", "pin 1"),
    (r"^TSSOP-", "ic", "pin 1"),
    (r"^MSOP-", "ic", "pin 1"),
    (r"^LQFP-", "ic", "pin 1"),
    (r"^TQFP-", "ic", "pin 1"),
    (r"^SSOP-", "ic", "pin 1"),
    (r"^SOP-", "ic", "pin 1"),
    (r"^ESP32-", "module", "pin 1"),
    (r"^TPS", "ic", "pin 1"),
    (r"^ICM-", "ic", "pin 1"),
    (r"^BMP", "ic", "pin 1"),
    (r"^USB_C_", "connector", "pin 1"),
    (r"^SW_", "switch", "pin 1"),
    (r"^JST_", "connector", "pin 1"),
    (r"^Molex_", "connector", "pin 1"),
    (r"^Connector_", "connector", "pin 1"),
]

POLARIZED_KINDS = {"diode", "led", "capacitor"}


@dataclass
class Footprint:
    lib_id: str
    reference: str | None
    value: str | None
    side: str
    x: float
    y: float
    rot: float
    attrs: set[str]
    fields: dict[str, str]
    pads: list[dict]
    dnp: bool

    @property
    def short_name(self) -> str:
        return self.lib_id.split(":", 1)[-1]

    @property
    def excluded_from_bom(self) -> bool:
        return "exclude_from_bom" in self.attrs

    @property
    def excluded_from_pos(self) -> bool:
        return "exclude_from_pos_files" in self.attrs

    @property
    def through_hole(self) -> bool:
        return "through_hole" in self.attrs

    @property
    def smd(self) -> bool:
        return "smd" in self.attrs

    def pad(self, number: str) -> dict | None:
        for pad in self.pads:
            if pad["number"] == number:
                return pad
        return None


def _net_name(node: list) -> str | None:
    """KiCad 7-9 wrote ``(net 1 "GND")``; KiCad 10 writes ``(net "GND")``."""
    if len(node) == 2:
        name = str(node[1])
    elif len(node) > 2:
        name = str(node[2])
    else:
        return None
    return name or None


def _footprint_reference(fp: list) -> str | None:
    for prop in sexpr.get_all(fp, "property"):
        if len(prop) > 2 and str(prop[1]) == "Reference":
            return str(prop[2])
    for prop in sexpr.get_all(fp, "fp_text"):
        if len(prop) > 2 and str(prop[1]) == "reference":
            return str(prop[2])
    return None


def _footprint_value(fp: list) -> str | None:
    for prop in sexpr.get_all(fp, "property"):
        if len(prop) > 2 and str(prop[1]) == "Value":
            return str(prop[2])
    for prop in sexpr.get_all(fp, "fp_text"):
        if len(prop) > 2 and str(prop[1]) == "value":
            return str(prop[2])
    return None


def _edge_points(root: list) -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = []

    def add_xy(node: list | None) -> None:
        if node and len(node) > 2:
            try:
                points.append((float(node[1]), float(node[2])))
            except (TypeError, ValueError):
                pass

    for kind in ("gr_line", "gr_rect"):
        for node in sexpr.get_all(root, kind):
            if sexpr.get_value(node, "layer") != "Edge.Cuts":
                continue
            add_xy(sexpr.get_first(node, "start"))
            add_xy(sexpr.get_first(node, "end"))
    for node in sexpr.get_all(root, "gr_arc"):
        if sexpr.get_value(node, "layer") != "Edge.Cuts":
            continue
        for key in ("start", "mid", "end"):
            add_xy(sexpr.get_first(node, key))
    for node in sexpr.get_all(root, "gr_circle"):
        if sexpr.get_value(node, "layer") != "Edge.Cuts":
            continue
        center = sexpr.get_first(node, "center")
        end = sexpr.get_first(node, "end")
        if center and end and len(center) > 2 and len(end) > 2:
            cx, cy = float(center[1]), float(center[2])
            r = math.hypot(float(end[1]) - cx, float(end[2]) - cy)
            points.extend([(cx - r, cy), (cx + r, cy), (cx, cy - r), (cx, cy + r)])
    for node in sexpr.get_all(root, "gr_poly"):
        if sexpr.get_value(node, "layer") != "Edge.Cuts":
            continue
        pts = sexpr.get_first(node, "pts")
        if pts:
            for xy in sexpr.get_all(pts, "xy"):
                add_xy(xy)
    return points


def _aux_origin(root: list) -> tuple[float, float]:
    setup = sexpr.get_first(root, "setup")
    if setup:
        aux = sexpr.get_first(setup, "aux_axis_origin")
        if aux and len(aux) > 2:
            try:
                return float(aux[1]), float(aux[2])
            except (TypeError, ValueError):
                pass
    return 0.0, 0.0


def _board_box(points: list[tuple[float, float]]) -> dict | None:
    if not points:
        return None
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return {
        "min_x": min(xs),
        "max_x": max(xs),
        "min_y": min(ys),
        "max_y": max(ys),
        "width": max(xs) - min(xs),
        "height": max(ys) - min(ys),
    }


def _parse_pcb(pcb_path: str | Path) -> dict:
    pcb = paths.normalize_path(pcb_path)
    if not pcb.is_file():
        raise FileNotFoundError(f"pcb not found: {pcb}")
    if pcb.suffix.lower() != ".kicad_pcb":
        raise ValueError(f"expected .kicad_pcb file, got: {pcb.name}")
    root = sexpr.load_file(pcb)
    if not root or root[0] != "kicad_pcb":
        raise ValueError(f"not a kicad_pcb file: {pcb.name}")

    board = _board_box(_edge_points(root))
    aux = _aux_origin(root)
    footprints: list[Footprint] = []

    for node in sexpr.get_all(root, "footprint"):
        lib_id = str(node[1]) if len(node) > 1 and not isinstance(node[1], list) else ""
        layer = sexpr.get_value(node, "layer") or "F.Cu"
        side = "bottom" if layer == "B.Cu" else "top"
        at = sexpr.get_first(node, "at")
        x = float(at[1]) if at and len(at) > 1 else 0.0
        y = float(at[2]) if at and len(at) > 2 else 0.0
        rot = float(at[3]) if at and len(at) > 3 else 0.0

        attrs = {str(a) for a in (sexpr.get_first(node, "attr") or [])[1:] if not isinstance(a, list)}
        fields: dict[str, str] = {}
        for prop in sexpr.get_all(node, "property"):
            if len(prop) > 2:
                fields[str(prop[1])] = str(prop[2])
        dnp_node = sexpr.get_first(node, "dnp")
        dnp = bool(dnp_node and len(dnp_node) > 1 and str(dnp_node[1]).lower() in ("yes", "true"))
        if not dnp:
            dnp = fields.get("DNP", "").upper() in ("1", "Y", "YES", "TRUE")

        pads = []
        for pad_node in sexpr.get_all(node, "pad"):
            number = str(pad_node[1]) if len(pad_node) > 1 else ""
            pad_at = sexpr.get_first(pad_node, "at")
            px = float(pad_at[1]) if pad_at and len(pad_at) > 1 else 0.0
            py = float(pad_at[2]) if pad_at and len(pad_at) > 2 else 0.0
            net_node = sexpr.get_first(pad_node, "net")
            net = _net_name(net_node) if net_node else None
            layers_node = sexpr.get_first(pad_node, "layers")
            pad_layers = [str(v) for v in layers_node[1:]] if layers_node else []
            pads.append(
                {
                    "number": number,
                    "x": px,
                    "y": py,
                    "net": net,
                    "layers": pad_layers,
                }
            )

        footprints.append(
            Footprint(
                lib_id=lib_id,
                reference=_footprint_reference(node),
                value=_footprint_value(node),
                side=side,
                x=x,
                y=y,
                rot=rot,
                attrs=attrs,
                fields=fields,
                pads=pads,
                dnp=dnp,
            )
        )

    return {"file": str(pcb), "board": board, "aux_origin": aux, "footprints": footprints}


def _rotation_offset(fp: Footprint, db: list[tuple[str, float]]) -> tuple[float, str | None]:
    for pattern, offset in db:
        if re.search(pattern, fp.short_name):
            return offset, pattern
    return 0.0, None


def _manual_rotation(fields: dict[str, str]) -> float:
    for key in ROTATION_FIELDS:
        raw = fields.get(key, "").strip()
        if raw:
            try:
                return float(raw)
            except ValueError:
                return 0.0
    return 0.0


def _manual_position(fp: Footprint) -> tuple[float, float]:
    for key in POSITION_FIELDS:
        raw = fp.fields.get(key, "").strip()
        if raw:
            parts = raw.split(",")
            if len(parts) == 2:
                try:
                    ox, oy = float(parts[0]), float(parts[1])
                except ValueError:
                    return 0.0, 0.0
                rad = math.radians(fp.rot)
                c, s = math.cos(rad), math.sin(rad)
                if fp.side == "bottom":
                    ox, oy = ox * c + oy * s, ox * s - oy * c
                else:
                    ox, oy = ox * c - oy * s, ox * s + oy * c
                return ox, oy
    return 0.0, 0.0


def _rotation(fp: Footprint, db: list[tuple[str, float]]) -> tuple[float, float, str | None]:
    rotation = fp.rot
    if fp.side == "bottom":
        rotation = 180.0 - rotation
    offset, pattern = _rotation_offset(fp, db)
    manual = _manual_rotation(fp.fields)
    rotation = (rotation + offset + manual) % 360.0
    return rotation, offset, pattern


def _position(fp: Footprint, origin: str, board: dict | None, aux: tuple[float, float]) -> tuple[float, float]:
    if origin == "lower-left":
        if not board:
            raise ValueError("board outline is required for the lower-left origin")
        ox, oy = board["min_x"], board["max_y"]
    else:
        ox, oy = aux
    return fp.x - ox, -(fp.y - oy)


def _pad_abs(fp: Footprint, pad: dict | None) -> tuple[float, float] | None:
    """Board-frame pad centre.

    KiCad stores a footprint's local pad coordinates and a board rotation.  In
    the Y-down board frame the stored rotation is clockwise, so the transform is
    R(-rot): x' = x*cos + y*sin, y' = -x*sin + y*cos.
    """
    if pad is None:
        return None
    rad = math.radians(fp.rot)
    c, s = math.cos(rad), math.sin(rad)
    x = fp.x + pad["x"] * c + pad["y"] * s
    y = fp.y - pad["x"] * s + pad["y"] * c
    return round(x, 4), round(y, 4)


def _view_xy(fp: Footprint, xy: tuple[float, float] | None, board: dict | None) -> tuple[float, float] | None:
    """Pad position in the assembly house's 2D preview frame.

    The bottom-side preview is mirrored left/right around the board centre and
    keeps Y; the top-side preview is the board frame itself.
    """
    if xy is None:
        return None
    x, y = xy
    if fp.side == "bottom" and board:
        x = board["min_x"] + board["max_x"] - x
    return round(x, 4), round(y, 4)


def _polarity_kind(short_name: str) -> tuple[str | None, str | None]:
    for pattern, kind, pin1_name in POLARITY_PATTERNS:
        if re.search(pattern, short_name):
            return kind, pin1_name
    return None, None


def _expected_row(
    fp: Footprint,
    db: list[tuple[str, float]],
    origin: str,
    board: dict | None,
    aux: tuple[float, float],
) -> dict:
    rotation, offset, pattern = _rotation(fp, db)
    manual_rotation = _manual_rotation(fp.fields)
    ox, oy = _manual_position(fp)
    mx, my = _position(fp, origin, board, aux)
    return {
        "ref": fp.reference,
        "value": fp.value,
        "lib_id": fp.lib_id,
        "side": fp.side,
        "mid_x": round(mx + ox, 4),
        "mid_y": round(my + oy, 4),
        "rotation": round(rotation, 4),
        "rotation_correction": round(offset, 4),
        "correction_pattern": pattern,
        "manual_rotation": round(manual_rotation, 4),
        "manual_position": [round(ox, 4), round(oy, 4)],
    }


def _is_placed(fp: Footprint, include_tht: bool) -> bool:
    return (
        bool(fp.reference)
        and not fp.dnp
        and not fp.excluded_from_pos
        and (include_tht or not fp.through_hole)
    )


def _ref_sort_key(ref: str):
    match = re.match(r"([A-Za-z_]+)(\d+)", ref)
    if match:
        return (match.group(1).upper(), int(match.group(2)), ref)
    return (ref.upper(), 0, ref)


def _format_number(value: float, digits: int = 4) -> str:
    text = f"{value:.{digits}f}".rstrip("0").rstrip(".")
    return text or "0"


def load_rotation_map(path: str | Path | None) -> list[tuple[str, float]]:
    if not path:
        return []
    data = json.loads(paths.normalize_path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict):
        return [(str(k), float(v)) for k, v in data.items()]
    return [(str(item[0]), float(item[1])) for item in data]


def _read_bom_refs(bom_path: str | Path) -> set[str]:
    bom = paths.normalize_path(bom_path)
    if not bom.is_file():
        raise FileNotFoundError(f"bom not found: {bom}")
    with bom.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        keys = {str(k).strip().lower(): k for k in (reader.fieldnames or [])}
        ref_key = None
        for candidate in ("refs", "references", "designator", "designators", "reference", "ref"):
            if candidate in keys:
                ref_key = keys[candidate]
                break
        if ref_key is None:
            raise ValueError("BOM has no Refs/References/Designator column")
        refs: set[str] = set()
        for row in reader:
            text = (row.get(ref_key) or "").strip()
            for ref in re.split(r"[,\s]+", text):
                if ref:
                    refs.add(ref.upper())
    return refs


def _read_cpl(cpl_path: str | Path, units: str = "mm") -> dict:
    cpl = paths.normalize_path(cpl_path)
    if not cpl.is_file():
        raise FileNotFoundError(f"cpl not found: {cpl}")
    with cpl.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        keys = {str(k).strip().lower(): k for k in (reader.fieldnames or [])}
        if "mid x" in keys or "midx" in keys:
            file_format = "jlc"
        elif "posx" in keys:
            file_format = "kicad_pos"
        else:
            raise ValueError("unrecognised CPL header; expected Mid X/Mid Y or PosX/PosY")

        def value(row: dict, *names: str) -> str:
            for name in names:
                key = keys.get(name)
                if key and row.get(key) not in (None, ""):
                    return str(row[key]).strip()
            return ""

        rows = []
        for row in reader:
            ref = value(row, "designator", "ref", "reference")
            if not ref:
                continue
            try:
                if file_format == "jlc":
                    x = float(value(row, "mid x", "midx"))
                    y = float(value(row, "mid y", "midy"))
                    rot = float(value(row, "rotation", "rot"))
                else:
                    x = float(value(row, "posx"))
                    y = float(value(row, "posy"))
                    rot = float(value(row, "rot"))
                    if units.lower() in ("in", "inch", "inches"):
                        x *= 25.4
                        y *= 25.4
                layer = value(row, "layer", "side").strip().lower()
            except ValueError:
                raise ValueError(f"invalid numeric value in CPL row for {ref}")
            rows.append(
                {
                    "ref": ref,
                    "x": round(x, 4),
                    "y": round(y, 4),
                    "rotation": round(rot % 360.0, 4),
                    "layer": layer,
                }
            )
    return {"format": file_format, "rows": rows}


def _netlist_pin_map(schematic_path: str | Path | None) -> dict[str, dict[str, dict]]:
    if not schematic_path:
        return {}
    sch = paths.normalize_path(schematic_path)
    if not sch.is_file():
        raise FileNotFoundError(f"schematic not found: {sch}")
    netlist_path = sch.with_name(f"{sch.stem}-netlist.net")
    if not netlist_path.is_file() or netlist_path.stat().st_mtime_ns < sch.stat().st_mtime_ns:
        kicad_cli.export_netlist(sch)
    data = sch_parser.read_netlist(netlist_path)
    pin_map: dict[str, dict[str, dict]] = {}
    for net in data["nets"]:
        for node in net["nodes"]:
            ref = node.get("ref")
            pin = node.get("pin")
            if not ref or pin is None:
                continue
            pin_map.setdefault(ref, {})[str(pin)] = {
                "net": net.get("name"),
                "pinfunction": node.get("pinfunction"),
            }
    return pin_map


def _audit_polarity(
    footprints: list[Footprint],
    expected_by_ref: dict[str, dict],
    netlist_pin_map: dict[str, dict[str, dict]],
    board: dict | None,
    include_tht: bool,
) -> list[dict]:
    audit = []
    for fp in footprints:
        if not fp.reference:
            continue
        kind, pin1_name = _polarity_kind(fp.short_name)
        if not kind:
            continue
        pad1 = fp.pad("1")
        pad2 = fp.pad("2")
        pad1_net = pad1["net"] if pad1 else None
        pad2_net = pad2["net"] if pad2 else None
        pad1_xy = _pad_abs(fp, pad1)
        pad2_xy = _pad_abs(fp, pad2)
        expected = expected_by_ref.get(fp.reference)
        schematic = netlist_pin_map.get(fp.reference, {})
        sch_pin1 = schematic.get("1", {})
        sch_pin1_net = sch_pin1.get("net")
        sch_pin1_function = sch_pin1.get("pinfunction")
        pin1_match = sch_pin1_net is None or pad1_net == sch_pin1_net
        audit.append(
            {
                "ref": fp.reference,
                "lib_id": fp.lib_id,
                "value": fp.value,
                "side": fp.side,
                "kind": kind,
                "pin1_name": pin1_name,
                "in_cpl": expected is not None,
                "dnp": fp.dnp,
                "through_hole": fp.through_hole,
                "kicad_rotation": round(fp.rot, 4),
                "cpl_rotation": expected["rotation"] if expected else None,
                "rotation_correction": expected["rotation_correction"] if expected else None,
                "correction_pattern": expected["correction_pattern"] if expected else None,
                "manual_rotation": expected["manual_rotation"] if expected else None,
                "pad1_net": pad1_net,
                "pad2_net": pad2_net,
                "pad1_xy_mm": list(pad1_xy) if pad1_xy else None,
                "pad2_xy_mm": list(pad2_xy) if pad2_xy else None,
                "pad1_view_xy_mm": list(_view_xy(fp, pad1_xy, board)) if pad1_xy else None,
                "pad2_view_xy_mm": list(_view_xy(fp, pad2_xy, board)) if pad2_xy else None,
                "schematic_pin1_net": sch_pin1_net,
                "schematic_pin1_function": sch_pin1_function,
                "pin1_net_match": pin1_match,
                "polarity_ok": pin1_match,
                "needs_review": bool(expected and expected["correction_pattern"] is None),
            }
        )
    return audit


def _bom_cpl_compare(bom_refs: set[str], cpl_refs: set[str]) -> dict:
    return {
        "bom_count": len(bom_refs),
        "cpl_count": len(cpl_refs),
        "missing_in_cpl": sorted(bom_refs - cpl_refs, key=_ref_sort_key),
        "missing_in_bom": sorted(cpl_refs - bom_refs, key=_ref_sort_key),
        "matched": len(bom_refs & cpl_refs),
    }


def _duplicate_refs(rows: list[dict]) -> list[str]:
    seen: dict[str, int] = {}
    for row in rows:
        ref = row["ref"].upper()
        seen[ref] = seen.get(ref, 0) + 1
    return sorted((ref for ref, count in seen.items() if count > 1), key=_ref_sort_key)


def export_cpl(
    pcb_path: str | Path,
    output_path: str | Path | None = None,
    bom_path: str | Path | None = None,
    schematic_path: str | Path | None = None,
    origin: str = "aux",
    include_tht: bool = False,
    rotation_map_path: str | Path | None = None,
    strict: bool = False,
) -> dict:
    """Generate a JLCPCB-ready CPL and a polarity / rotation audit.

    ``origin`` is ``aux`` (KiCad auxiliary/drill origin, Y-up; matches the
    Gerbers) or ``lower-left`` (board lower-left, Y-up).  ``strict`` raises
    when the BOM/CPL designators do not match or a designator is duplicated;
    otherwise the result carries ``ok:false`` and the error list.
    """
    data = _parse_pcb(pcb_path)
    pcb = paths.normalize_path(pcb_path)
    board = data["board"]
    aux = data["aux_origin"]
    db = load_rotation_map(rotation_map_path) + ROTATION_DB

    expected_by_ref: dict[str, dict] = {}
    excluded = []
    for fp in data["footprints"]:
        if not fp.reference:
            continue
        if not _is_placed(fp, include_tht):
            reason = (
                "dnp" if fp.dnp
                else "exclude_from_pos" if fp.excluded_from_pos
                else "through_hole" if fp.through_hole
                else "no_reference"
            )
            excluded.append({"ref": fp.reference, "lib_id": fp.lib_id, "reason": reason})
            continue
        expected_by_ref[fp.reference] = _expected_row(fp, db, origin, board, aux)

    rows = [expected_by_ref[ref] for ref in sorted(expected_by_ref, key=_ref_sort_key)]
    out = paths.normalize_path(output_path) if output_path else pcb.parent / f"{pcb.stem}-cpl.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    lines = ["Designator,Mid X,Mid Y,Rotation,Layer"]
    for row in rows:
        lines.append(
            ",".join(
                [
                    row["ref"],
                    _format_number(row["mid_x"]),
                    _format_number(row["mid_y"]),
                    _format_number(row["rotation"]),
                    row["side"],
                ]
            )
        )
    paths.atomic_write_text(out, "\n".join(lines) + "\n")

    errors: list[dict] = []
    warnings: list[dict] = []

    duplicates = _duplicate_refs(rows)
    for ref in duplicates:
        errors.append({"type": "duplicate_ref", "ref": ref})

    bom_cpl = None
    if bom_path:
        bom_refs = _read_bom_refs(bom_path)
        cpl_refs = {row["ref"].upper() for row in rows}
        bom_cpl = _bom_cpl_compare(bom_refs, cpl_refs)
        for ref in bom_cpl["missing_in_cpl"]:
            errors.append({"type": "missing_in_cpl", "ref": ref})
        for ref in bom_cpl["missing_in_bom"]:
            errors.append({"type": "missing_in_bom", "ref": ref})

    netlist_pin_map: dict[str, dict[str, dict]] = {}
    if schematic_path:
        try:
            netlist_pin_map = _netlist_pin_map(schematic_path)
        except Exception as exc:  # noqa: BLE001
            warnings.append({"type": "netlist_unavailable", "detail": str(exc)})

    audit = _audit_polarity(
        data["footprints"], expected_by_ref, netlist_pin_map, board, include_tht
    )
    for item in audit:
        if not item["in_cpl"]:
            continue
        if item["needs_review"]:
            warnings.append(
                {
                    "type": "no_rotation_correction",
                    "ref": item["ref"],
                    "lib_id": item["lib_id"],
                    "kind": item["kind"],
                    "pin1_name": item["pin1_name"],
                    "note": "No vendor rotation correction is known for this footprint; "
                            "verify its orientation in the assembly preview.",
                }
            )
        if item["pin1_net_match"] is False:
            warnings.append(
                {
                    "type": "pin1_net_mismatch",
                    "ref": item["ref"],
                    "pcb_pad1_net": item["pad1_net"],
                    "schematic_pin1_net": item["schematic_pin1_net"],
                    "schematic_pin1_function": item["schematic_pin1_function"],
                }
            )

    report = {
        "origin": origin,
        "board_size_mm": (
            [round(board["width"], 4), round(board["height"], 4)] if board else None
        ),
        "rotation_corrections": _rotation_summary(rows),
        "no_rotation_correction": sorted(
            {item["lib_id"] for item in audit if item["in_cpl"] and item["needs_review"]}
        ),
        "polarity_audit": audit,
        "bom_cpl": bom_cpl,
        "excluded": excluded,
    }
    result = {
        "ok": not errors,
        "output": str(out),
        "count": len(rows),
        "rows": rows,
        "errors": errors,
        "warnings": warnings,
        "ready": not errors,
        "report": report,
        "revision": paths.file_revision(out),
        "source_revision": paths.file_revision(pcb),
    }
    if strict and errors:
        raise RuntimeError(f"CPL has {len(errors)} blocking error(s): {errors}")
    return result


def _rotation_summary(rows: list[dict]) -> dict[str, list[str]]:
    summary: dict[str, list[str]] = {}
    for row in rows:
        pattern = row.get("correction_pattern")
        if pattern:
            summary.setdefault(pattern, []).append(row["ref"])
    for pattern in summary:
        summary[pattern] = sorted(summary[pattern], key=_ref_sort_key)
    return summary


def _rotation_delta(a: float, b: float) -> float:
    return abs(((a - b + 180.0) % 360.0) - 180.0)


def _origin_error(expected: dict[str, dict], cpl_by_ref: dict[str, dict]) -> float:
    total = 0.0
    count = 0
    for ref, exp in expected.items():
        row = cpl_by_ref.get(ref.upper())
        if not row:
            continue
        total += (row["x"] - exp["mid_x"]) ** 2 + (row["y"] - exp["mid_y"]) ** 2
        count += 1
    return (total / count) ** 0.5 if count else float("inf")


def verify_cpl(
    pcb_path: str | Path,
    cpl_path: str | Path,
    bom_path: str | Path | None = None,
    schematic_path: str | Path | None = None,
    origin: str = "auto",
    include_tht: bool = False,
    position_tolerance_mm: float = 0.01,
    rotation_tolerance_deg: float = 0.1,
    cpl_format: str = "auto",
    units: str = "mm",
    strict: bool = False,
) -> dict:
    """Verify a CPL file against the PCB, BOM and (optionally) the schematic.

    Checks designator coverage, duplicates, mid X/Y, rotation (with the
    bottom-side mirror and the vendor correction) and layer.  The polarity
    audit reports pin-1 nets and pad-1 positions for diodes, LEDs, polarized
    capacitors, ICs and connectors; ``no_rotation_correction`` items must be
    checked in the assembly preview.
    """
    data = _parse_pcb(pcb_path)
    pcb = paths.normalize_path(pcb_path)
    board = data["board"]
    aux = data["aux_origin"]
    db = ROTATION_DB

    cpl = _read_cpl(cpl_path, units=units)
    if cpl_format != "auto" and cpl_format != cpl["format"]:
        raise ValueError(f"CPL format is {cpl['format']}, not {cpl_format}")

    cpl_by_ref: dict[str, dict] = {}
    duplicates = []
    for row in cpl["rows"]:
        key = row["ref"].upper()
        if key in cpl_by_ref:
            duplicates.append(row["ref"])
        cpl_by_ref[key] = row

    if cpl["format"] == "kicad_pos":
        origin_mode = "aux"
    elif origin == "auto":
        aux_expected = {
            fp.reference: _expected_row(fp, db, "aux", board, aux)
            for fp in data["footprints"]
            if fp.reference and _is_placed(fp, include_tht)
        }
        ll_expected = {
            fp.reference: _expected_row(fp, db, "lower-left", board, aux)
            for fp in data["footprints"]
            if fp.reference and _is_placed(fp, include_tht)
        }
        aux_error = _origin_error(aux_expected, cpl_by_ref)
        ll_error = _origin_error(ll_expected, cpl_by_ref)
        origin_mode = "lower-left" if ll_error < aux_error else "aux"
    else:
        origin_mode = origin

    if cpl["format"] == "kicad_pos":
        expected_by_ref = {
            fp.reference: {
                **_expected_row(fp, db, "aux", board, aux),
                "rotation": round(fp.rot % 360.0, 4),
                "rotation_correction": 0.0,
                "correction_pattern": None,
            }
            for fp in data["footprints"]
            if fp.reference and _is_placed(fp, include_tht)
        }
    else:
        expected_by_ref = {
            fp.reference: _expected_row(fp, db, origin_mode, board, aux)
            for fp in data["footprints"]
            if fp.reference and _is_placed(fp, include_tht)
        }

    errors: list[dict] = []
    rows = []
    for ref in sorted(expected_by_ref, key=_ref_sort_key):
        exp = expected_by_ref[ref]
        row = cpl_by_ref.get(ref.upper())
        if row is None:
            errors.append({"type": "missing_in_cpl", "ref": ref})
            rows.append({"ref": ref, "ok": False, "error": "missing_in_cpl"})
            continue
        dx = row["x"] - exp["mid_x"]
        dy = row["y"] - exp["mid_y"]
        position_error = math.hypot(dx, dy)
        rotation_error = _rotation_delta(row["rotation"], exp["rotation"])
        layer_ok = row["layer"] == exp["side"]
        row_ok = (
            position_error <= position_tolerance_mm
            and rotation_error <= rotation_tolerance_deg
            and layer_ok
        )
        if position_error > position_tolerance_mm:
            errors.append(
                {
                    "type": "position_mismatch",
                    "ref": ref,
                    "expected": [exp["mid_x"], exp["mid_y"]],
                    "actual": [row["x"], row["y"]],
                    "error_mm": round(position_error, 4),
                }
            )
        if rotation_error > rotation_tolerance_deg:
            errors.append(
                {
                    "type": "rotation_mismatch",
                    "ref": ref,
                    "expected": exp["rotation"],
                    "actual": row["rotation"],
                    "error_deg": round(rotation_error, 4),
                    "correction_pattern": exp["correction_pattern"],
                }
            )
        if not layer_ok:
            errors.append(
                {
                    "type": "layer_mismatch",
                    "ref": ref,
                    "expected": exp["side"],
                    "actual": row["layer"],
                }
            )
        rows.append(
            {
                "ref": ref,
                "ok": row_ok,
                "expected_xy": [exp["mid_x"], exp["mid_y"]],
                "actual_xy": [row["x"], row["y"]],
                "position_error_mm": round(position_error, 4),
                "expected_rotation": exp["rotation"],
                "actual_rotation": row["rotation"],
                "rotation_error_deg": round(rotation_error, 4),
                "expected_layer": exp["side"],
                "actual_layer": row["layer"],
                "correction_pattern": exp["correction_pattern"],
            }
        )

    expected_refs = {ref.upper() for ref in expected_by_ref}
    for ref in cpl_by_ref:
        if ref not in expected_refs:
            errors.append({"type": "extra_in_cpl", "ref": ref})
    for ref in duplicates:
        errors.append({"type": "duplicate_ref", "ref": ref})

    bom_cpl = None
    if bom_path:
        bom_refs = _read_bom_refs(bom_path)
        cpl_refs = set(cpl_by_ref)
        bom_cpl = _bom_cpl_compare(bom_refs, cpl_refs)
        for ref in bom_cpl["missing_in_cpl"]:
            errors.append({"type": "missing_in_cpl", "ref": ref})
        for ref in bom_cpl["missing_in_bom"]:
            errors.append({"type": "missing_in_bom", "ref": ref})

    netlist_pin_map: dict[str, dict[str, dict]] = {}
    warnings: list[dict] = []
    if schematic_path:
        try:
            netlist_pin_map = _netlist_pin_map(schematic_path)
        except Exception as exc:  # noqa: BLE001
            warnings.append({"type": "netlist_unavailable", "detail": str(exc)})

    audit = _audit_polarity(
        data["footprints"], expected_by_ref, netlist_pin_map, board, include_tht
    )
    for item in audit:
        if item["in_cpl"]:
            actual = cpl_by_ref.get(item["ref"].upper())
            item["actual_cpl_rotation"] = actual["rotation"] if actual else None
            item["polarity_ok"] = (
                item["pin1_net_match"]
                and actual is not None
                and _rotation_delta(actual["rotation"], item["cpl_rotation"])
                <= rotation_tolerance_deg
            )
        else:
            item["actual_cpl_rotation"] = None
    for item in audit:
        if not item["in_cpl"]:
            continue
        if item["needs_review"]:
            warnings.append(
                {
                    "type": "no_rotation_correction",
                    "ref": item["ref"],
                    "lib_id": item["lib_id"],
                    "kind": item["kind"],
                    "pin1_name": item["pin1_name"],
                    "note": "No vendor rotation correction is known for this footprint; "
                            "verify its orientation in the assembly preview.",
                }
            )
        if item["pin1_net_match"] is False:
            warnings.append(
                {
                    "type": "pin1_net_mismatch",
                    "ref": item["ref"],
                    "pcb_pad1_net": item["pad1_net"],
                    "schematic_pin1_net": item["schematic_pin1_net"],
                    "schematic_pin1_function": item["schematic_pin1_function"],
                }
            )

    summary = {
        "expected_rows": len(expected_by_ref),
        "cpl_rows": len(cpl_by_ref),
        "matched": sum(1 for row in rows if row["ok"]),
        "missing_in_cpl": sum(1 for e in errors if e["type"] == "missing_in_cpl"),
        "extra_in_cpl": sum(1 for e in errors if e["type"] == "extra_in_cpl"),
        "position_errors": sum(1 for e in errors if e["type"] == "position_mismatch"),
        "rotation_errors": sum(1 for e in errors if e["type"] == "rotation_mismatch"),
        "layer_errors": sum(1 for e in errors if e["type"] == "layer_mismatch"),
        "polarity_items": len(audit),
        "polarity_ok": sum(1 for item in audit if item.get("polarity_ok") is True),
        "polarity_fail": sum(1 for item in audit if item.get("polarity_ok") is False),
        "polarity_review": sum(1 for item in audit if item["in_cpl"] and item["needs_review"]),
    }
    result = {
        "ok": not errors,
        "pcb": str(pcb),
        "cpl": str(paths.normalize_path(cpl_path)),
        "format": cpl["format"],
        "origin": origin_mode,
        "count": len(cpl_by_ref),
        "errors": errors,
        "warnings": warnings,
        "summary": summary,
        "rows": rows,
        "polarity_audit": audit,
        "bom_cpl": bom_cpl,
        "revision": {
            "pcb": paths.file_revision(pcb),
            "cpl": paths.file_revision(cpl_path),
        },
    }
    if strict and errors:
        raise RuntimeError(f"CPL verification failed: {errors}")
    return result
