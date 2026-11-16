"""Mechanical checks: courtyards, 3D models and board-edge clearance."""

from __future__ import annotations

import os
from pathlib import Path

from . import kicad_cli, pcb_edit, paths

_MECHANICAL_SCRIPT = r"""
import json, math, sys
import pcbnew

MM = 1e6
p = json.load(open(sys.argv[1], encoding="utf-8"))
out = {}

try:
    board = pcbnew.LoadBoard(p["pcb"])
    outline = pcbnew.SHAPE_POLY_SET()
    outline_ok = bool(board.GetBoardPolygonOutlines(outline, True, None, True, False))
    out["board_outline_ok"] = outline_ok
    if outline_ok:
        bb = outline.BBox()
        out["board_bbox_mm"] = [
            bb.GetPosition().x / MM, bb.GetPosition().y / MM,
            bb.GetEnd().x / MM, bb.GetEnd().y / MM,
        ]

    min_clearance = int(float(p.get("min_edge_clearance_mm", 0.5)) * MM)
    footprints = []
    courtyard_overlaps = []
    missing_courtyards = []
    outside_courtyards = []
    edge_violations = []
    connectors = []

    def outside_board(poly):
        if not outline_ok:
            return False
        for k in range(poly.OutlineCount()):
            chain = poly.COutline(k)
            for i in range(chain.PointCount()):
                if not outline.Contains(chain.CPoint(i)):
                    return True
        return False

    for fp in board.GetFootprints():
        ref = fp.GetReference()
        pos = fp.GetPosition()
        cf = fp.GetCourtyard(pcbnew.F_CrtYd)
        cb = fp.GetCourtyard(pcbnew.B_CrtYd)
        cf_count = cf.OutlineCount()
        cb_count = cb.OutlineCount()
        bbox = fp.GetBoundingBox()
        models = []
        for model in fp.Models():
            models.append({"filename": model.m_Filename, "show": bool(model.m_Show)})
        row = {
            "ref": ref,
            "value": fp.GetValue(),
            "position_mm": [pos.x / MM, pos.y / MM],
            "layer": pcbnew.LayerName(fp.GetLayer()),
            "bbox_mm": [
                bbox.GetLeft() / MM, bbox.GetTop() / MM,
                bbox.GetRight() / MM, bbox.GetBottom() / MM,
            ],
            "courtyard_f_outlines": cf_count,
            "courtyard_b_outlines": cb_count,
            "models": models,
        }
        footprints.append(row)
        if cf_count == 0 and cb_count == 0:
            missing_courtyards.append(ref)
        for layer, poly, count in (("F.CrtYd", cf, cf_count), ("B.CrtYd", cb, cb_count)):
            if count and outside_board(poly):
                outside_courtyards.append({"ref": ref, "layer": layer})

    # courtyard overlaps
    for i in range(len(footprints)):
        for j in range(i + 1, len(footprints)):
            a = footprints[i]
            b = footprints[j]
            if a["courtyard_f_outlines"] and b["courtyard_f_outlines"]:
                fp_a = board.FindFootprintByReference(a["ref"])
                fp_b = board.FindFootprintByReference(b["ref"])
                if fp_a and fp_b and fp_a.GetCourtyard(pcbnew.F_CrtYd).Collide(
                    fp_b.GetCourtyard(pcbnew.F_CrtYd)
                ):
                    courtyard_overlaps.append({"a": a["ref"], "b": b["ref"], "layer": "F.CrtYd"})
            if a["courtyard_b_outlines"] and b["courtyard_b_outlines"]:
                fp_a = board.FindFootprintByReference(a["ref"])
                fp_b = board.FindFootprintByReference(b["ref"])
                if fp_a and fp_b and fp_a.GetCourtyard(pcbnew.B_CrtYd).Collide(
                    fp_b.GetCourtyard(pcbnew.B_CrtYd)
                ):
                    courtyard_overlaps.append({"a": a["ref"], "b": b["ref"], "layer": "B.CrtYd"})

    # pad / track / via / courtyard edge clearance, sampled against a
    # deflated board outline so items fully inside the board are not flagged.
    if outline_ok:
        deflated = pcbnew.SHAPE_POLY_SET(outline)
        try:
            deflated.Deflate(
                min_clearance,
                pcbnew.CORNER_STRATEGY_ROUND_ALL_CORNERS,
                5000,
            )
        except Exception:
            pass

        def point_inside(x, y):
            return deflated.Contains(pcbnew.VECTOR2I(int(x), int(y)))

        def bbox_inside(bbox):
            points = [
                (bbox.GetLeft(), bbox.GetTop()),
                (bbox.GetRight(), bbox.GetTop()),
                (bbox.GetRight(), bbox.GetBottom()),
                (bbox.GetLeft(), bbox.GetBottom()),
                ((bbox.GetLeft() + bbox.GetRight()) // 2,
                 (bbox.GetTop() + bbox.GetBottom()) // 2),
            ]
            return all(point_inside(x, y) for x, y in points)

        for fp in board.GetFootprints():
            for pad in fp.Pads():
                bbox = pad.GetBoundingBox()
                if not bbox_inside(bbox):
                    pos = pad.GetPosition()
                    edge_violations.append({
                        "kind": "pad", "ref": fp.GetReference(), "pad": pad.GetNumber(),
                        "position_mm": [pos.x / MM, pos.y / MM],
                    })
        for track in board.GetTracks():
            if track.Type() == pcbnew.PCB_VIA_T:
                pos = track.GetPosition()
                radius = track.GetWidth(pcbnew.F_Cu) // 2
                points = [(pos.x, pos.y)]
                for a in range(8):
                    angle = a * math.pi / 4.0
                    points.append((pos.x + radius * math.cos(angle),
                                   pos.y + radius * math.sin(angle)))
                if not all(point_inside(x, y) for x, y in points):
                    edge_violations.append({
                        "kind": "via", "net": track.GetNetname(),
                        "position_mm": [pos.x / MM, pos.y / MM],
                    })
            elif track.Type() == pcbnew.PCB_TRACE_T:
                start, end = track.GetStart(), track.GetEnd()
                width = track.GetWidth()
                dx, dy = end.x - start.x, end.y - start.y
                length = math.hypot(dx, dy) or 1.0
                nx, ny = -dy / length, dx / length
                points = [(start.x, start.y), (end.x, end.y),
                          ((start.x + end.x) // 2, (start.y + end.y) // 2)]
                for x, y in list(points):
                    points.append((x + nx * width / 2, y + ny * width / 2))
                    points.append((x - nx * width / 2, y - ny * width / 2))
                if not all(point_inside(x, y) for x, y in points):
                    edge_violations.append({
                        "kind": "track", "net": track.GetNetname(),
                        "layer": pcbnew.LayerName(track.GetLayer()),
                        "uuid": track.m_Uuid.AsString(),
                    })

        for fp in board.GetFootprints():
            for layer, poly in (("F.CrtYd", fp.GetCourtyard(pcbnew.F_CrtYd)),
                                ("B.CrtYd", fp.GetCourtyard(pcbnew.B_CrtYd))):
                if poly.OutlineCount() == 0:
                    continue
                points = []
                for k in range(poly.OutlineCount()):
                    chain = poly.COutline(k)
                    for i in range(chain.PointCount()):
                        pt = chain.CPoint(i)
                        points.append((pt.x, pt.y))
                if not all(point_inside(x, y) for x, y in points):
                    edge_violations.append({
                        "kind": "courtyard", "ref": fp.GetReference(), "layer": layer,
                    })

    # connector accessibility heuristic
    if outline_ok and out.get("board_bbox_mm"):
        x0, y0, x1, y1 = out["board_bbox_mm"]
        cx = (x0 + x1) / 2.0
        cy = (y0 + y1) / 2.0
        for fp in board.GetFootprints():
            ref = fp.GetReference()
            value = (fp.GetValue() or "").upper()
            if not (ref.upper().startswith("J") or "USB" in value or "ANT" in value or "CONN" in value):
                continue
            bbox = fp.GetBoundingBox()
            left = bbox.GetLeft() / MM - x0
            right = x1 - bbox.GetRight() / MM
            top = bbox.GetTop() / MM - y0
            bottom = y1 - bbox.GetBottom() / MM
            distances = {"left": left, "right": right, "top": top, "bottom": bottom}
            side = min(distances, key=distances.get)
            pos = fp.GetPosition()
            connectors.append({
                "ref": ref,
                "value": fp.GetValue(),
                "position_mm": [pos.x / MM, pos.y / MM],
                "nearest_edge": side,
                "edge_distance_mm": round(distances[side], 2),
                "facing": side,
                "heuristic": True,
            })

    out["footprints"] = footprints
    out["courtyard_overlaps"] = courtyard_overlaps
    out["missing_courtyards"] = missing_courtyards
    out["courtyards_outside_board"] = outside_courtyards
    out["edge_clearance_violations"] = edge_violations
    out["connectors"] = connectors
    out["summary"] = {
        "footprint_count": len(footprints),
        "courtyard_overlap_count": len(courtyard_overlaps),
        "missing_courtyard_count": len(missing_courtyards),
        "courtyard_outside_count": len(outside_courtyards),
        "edge_clearance_violation_count": len(edge_violations),
        "connector_count": len(connectors),
    }
except Exception as exc:
    out = {"error": "{}: {}".format(type(exc).__name__, exc)}
print(json.dumps(out))
"""


def _model_root() -> Path | None:
    program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    for version in kicad_cli.DEFAULT_KICAD_VERSIONS:
        candidate = Path(program_files) / "KiCad" / version / "share" / "kicad" / "3dmodels"
        if candidate.is_dir():
            return candidate
    return None


def _expand_model(filename: str) -> str:
    if not filename:
        return ""
    root = _model_root()
    text = filename
    for token in ("${KICAD10_3DMODEL_DIR}", "${KICAD9_3DMODEL_DIR}", "${KISYS3DMOD}"):
        if token in text and root is not None:
            text = text.replace(token, str(root))
    return text


def _run(pcb_path: str | Path, min_edge_clearance_mm: float) -> dict:
    pcb = paths.normalize_path(pcb_path)
    if not pcb.is_file():
        raise FileNotFoundError(f"pcb not found: {pcb}")
    result = pcb_edit._run_on_pcb(
        _MECHANICAL_SCRIPT,
        pcb,
        {"min_edge_clearance_mm": float(min_edge_clearance_mm)},
        timeout=600.0,
        write_back=False,
    )
    if not result.get("ok"):
        return {"ok": False, "error": result.get("result") or result}
    data = result["result"]
    for row in data.get("footprints", []):
        for model in row.get("models", []):
            expanded = _expand_model(model.get("filename") or "")
            model["expanded"] = expanded
            model["exists"] = bool(expanded and Path(expanded).is_file())
    return {"ok": True, "result": data}


def check_courtyard(pcb_path: str | Path) -> dict:
    data = _run(pcb_path, 0.0)
    if not data["ok"]:
        return data
    result = data["result"]
    return {
        "ok": True,
        "courtyard_overlaps": result.get("courtyard_overlaps", []),
        "missing_courtyards": result.get("missing_courtyards", []),
        "courtyards_outside_board": result.get("courtyards_outside_board", []),
        "summary": {
            "overlap_count": len(result.get("courtyard_overlaps", [])),
            "missing_count": len(result.get("missing_courtyards", [])),
            "outside_count": len(result.get("courtyards_outside_board", [])),
        },
    }


def check_3d_models(pcb_path: str | Path) -> dict:
    data = _run(pcb_path, 0.0)
    if not data["ok"]:
        return data
    result = data["result"]
    missing = []
    without_model = []
    for row in result.get("footprints", []):
        models = row.get("models", [])
        if not models:
            without_model.append(row["ref"])
            continue
        for model in models:
            if not model.get("expanded") or not model.get("exists"):
                missing.append(
                    {
                        "ref": row["ref"],
                        "filename": model.get("filename"),
                        "expanded": model.get("expanded"),
                    }
                )
    return {
        "ok": True,
        "missing_models": missing,
        "footprints_without_model": without_model,
        "summary": {
            "missing_model_count": len(missing),
            "without_model_count": len(without_model),
        },
    }


def board_edge_clearance(pcb_path: str | Path, min_mm: float = 0.5) -> dict:
    data = _run(pcb_path, min_mm)
    if not data["ok"]:
        return data
    result = data["result"]
    return {
        "ok": True,
        "min_edge_clearance_mm": float(min_mm),
        "violations": result.get("edge_clearance_violations", []),
        "summary": {
            "violation_count": len(result.get("edge_clearance_violations", [])),
        },
    }


def mechanical_check(pcb_path: str | Path, min_edge_clearance_mm: float = 0.5) -> dict:
    data = _run(pcb_path, min_edge_clearance_mm)
    if not data["ok"]:
        return data
    result = data["result"]
    missing_3d = []
    without_model = []
    for row in result.get("footprints", []):
        models = row.get("models", [])
        if not models:
            without_model.append(row["ref"])
            continue
        for model in models:
            if not model.get("expanded") or not model.get("exists"):
                missing_3d.append(
                    {
                        "ref": row["ref"],
                        "filename": model.get("filename"),
                        "expanded": model.get("expanded"),
                    }
                )
    summary = dict(result.get("summary", {}))
    summary["missing_3d_model_count"] = len(missing_3d)
    summary["footprints_without_model_count"] = len(without_model)
    return {
        "ok": True,
        "board_outline_ok": result.get("board_outline_ok"),
        "board_bbox_mm": result.get("board_bbox_mm"),
        "courtyard_overlaps": result.get("courtyard_overlaps", []),
        "missing_courtyards": result.get("missing_courtyards", []),
        "courtyards_outside_board": result.get("courtyards_outside_board", []),
        "edge_clearance_violations": result.get("edge_clearance_violations", []),
        "missing_3d_models": missing_3d,
        "footprints_without_model": without_model,
        "connectors": result.get("connectors", []),
        "summary": summary,
        "limitations": [
            "connector accessibility is a position/edge heuristic, not a 3D clearance simulation",
            "3D model existence is checked against the installed KiCad 3D model root",
        ],
    }
