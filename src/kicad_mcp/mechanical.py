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


_OVERLAP_SCRIPT = r"""
import json, math, re, sys
import pcbnew

MM = 1e6
p = json.load(open(sys.argv[1], encoding="utf-8"))
out = {}

try:
    board = pcbnew.LoadBoard(p["pcb"])
    model_root = p.get("model_root") or ""
    project_dir = p.get("project_dir") or ""
    min_gap = float(p.get("min_gap_mm", 0.0))
    cache = {}

    def expand(fn):
        if not fn:
            return ""
        for tok in ("${KICAD10_3DMODEL_DIR}", "${KICAD9_3DMODEL_DIR}", "${KISYS3DMOD}"):
            if tok in fn and model_root:
                fn = fn.replace(tok, model_root)
        if "${KIPRJMOD}" in fn and project_dir:
            fn = fn.replace("${KIPRJMOD}", project_dir)
        return fn

    _PT_RE = re.compile(
        r"#(\d+)\s*=\s*CARTESIAN_POINT\s*\(\s*'[^']*'\s*,\s*\(\s*([-0-9.Ee+]+)\s*,\s*([-0-9.Ee+]+)\s*,\s*([-0-9.Ee+]+)\s*\)\s*\)"
    )
    _VP_RE = re.compile(r"VERTEX_POINT\s*\(\s*'[^']*'\s*,\s*#(\d+)\s*\)")

    def step_bbox(path):
        # Use VERTEX_POINT references when present: raw CARTESIAN_POINT entities
        # also hold placement origins/directions and inflate the bounding box.
        if path in cache:
            return cache[path]
        xs = []
        ys = []
        try:
            txt = open(path, encoding="utf-8", errors="ignore").read()
            pts = {m.group(1): (float(m.group(2)), float(m.group(3)))
                   for m in _PT_RE.finditer(txt)}
            used = [pts[m.group(1)] for m in _VP_RE.finditer(txt) if m.group(1) in pts]
            if not used:
                used = list(pts.values())
            xs = [p[0] for p in used]
            ys = [p[1] for p in used]
        except Exception:
            pass
        cache[path] = (min(xs), max(xs), min(ys), max(ys)) if xs else None
        return cache[path]

    def rot(x, y, deg):
        a = math.radians(deg)
        c = math.cos(a)
        s = math.sin(a)
        return x * c - y * s, x * s + y * c

    def model_bbox(fp):
        pts = []
        for mo in fp.Models():
            fn = expand(mo.m_Filename)
            bb = step_bbox(fn)
            if not bb:
                continue
            x0, x1, y0, y1 = bb
            sx, sy = mo.m_Scale.x, mo.m_Scale.y
            ox, oy = mo.m_Offset.x, mo.m_Offset.y
            rx, ry, rz = mo.m_Rotation.x, mo.m_Rotation.y, mo.m_Rotation.z
            for cx in (x0, x1):
                for cy in (y0, y1):
                    px, py, pz = cx * sx, cy * sy, 0.0
                    py = -py  # KiCad 3D models are Y-up; board Y is down
                    py, pz = rot(py, pz, rx)
                    px, pz = rot(px, pz, ry)
                    px, py = rot(px, py, rz)
                    px += ox
                    py += oy
                    pts.append((px, py))
        if not pts:
            return None
        deg = fp.GetOrientationDegrees()
        pos = fp.GetPosition()
        xs = []
        ys = []
        for px, py in pts:
            wx, wy = rot(px, py, deg)
            xs.append(pos.x / MM + wx)
            ys.append(pos.y / MM + wy)
        return (min(xs), min(ys), max(xs), max(ys))

    def graphic_bbox(fp, layers):
        segs = []
        for g in fp.GraphicalItems():
            if g.GetLayer() in layers:
                bb = g.GetBoundingBox()
                segs.append((bb.GetLeft() / MM, bb.GetTop() / MM,
                             bb.GetRight() / MM, bb.GetBottom() / MM))
        if not segs:
            return None
        return (min(s[0] for s in segs), min(s[1] for s in segs),
                max(s[2] for s in segs), max(s[3] for s in segs))

    def pad_bbox(fp):
        pads = list(fp.Pads())
        if not pads:
            return None
        return (min(pad.GetBoundingBox().GetLeft() / MM for pad in pads),
                min(pad.GetBoundingBox().GetTop() / MM for pad in pads),
                max(pad.GetBoundingBox().GetRight() / MM for pad in pads),
                max(pad.GetBoundingBox().GetBottom() / MM for pad in pads))

    rows = []
    for fp in board.GetFootprints():
        side = "F" if fp.GetLayer() == pcbnew.F_Cu else "B"
        fab = graphic_bbox(fp, (pcbnew.F_Fab,) if side == "F" else (pcbnew.B_Fab,))
        mod = model_bbox(fp)
        body = None
        src = ""
        # Prefer the 3D model box (it includes leads), but only when it is
        # consistent with the fab outline.  Some project models are drawn with a
        # different origin or carry keepouts, which would create false overlaps.
        if mod and (fab is None or (mod[0] >= fab[0] - 1.0 and mod[1] >= fab[1] - 1.0
                                    and mod[2] <= fab[2] + 1.0 and mod[3] <= fab[3] + 1.0)):
            body, src = mod, "3d"
        elif fab:
            body, src = fab, "fab"
        elif mod:
            body, src = mod, "3d"
        else:
            body = pad_bbox(fp)
            src = "pads"
        if body is None:
            continue
        cy = graphic_bbox(fp, (pcbnew.F_CrtYd,) if side == "F" else (pcbnew.B_CrtYd,))
        rows.append({
            "ref": fp.GetReference(),
            "value": fp.GetValue(),
            "side": side,
            "body": body,
            "source": src,
            "courtyard": cy,
        })

    def overlap(a, c):
        x1 = max(a[0], c[0])
        y1 = max(a[1], c[1])
        x2 = min(a[2], c[2])
        y2 = min(a[3], c[3])
        if x2 > x1 + 1e-4 and y2 > y1 + 1e-4:
            return (x2 - x1, y2 - y1)
        return None

    def gap(a, c):
        dx = max(a[0] - c[2], c[0] - a[2], 0.0)
        dy = max(a[1] - c[3], c[1] - a[3], 0.0)
        return math.hypot(dx, dy)

    body_overlaps = []
    close_pairs = []
    courtyard_only = []
    for i in range(len(rows)):
        for j in range(i + 1, len(rows)):
            a = rows[i]
            b = rows[j]
            if a["side"] != b["side"]:
                continue
            o = overlap(a["body"], b["body"])
            if o:
                body_overlaps.append({
                    "a": a["ref"], "b": b["ref"], "side": a["side"],
                    "overlap_mm": [round(o[0], 3), round(o[1], 3)],
                    "area_mm2": round(o[0] * o[1], 4),
                    "source": [a["source"], b["source"]],
                })
                continue
            d = gap(a["body"], b["body"])
            if min_gap > 0 and d < min_gap:
                close_pairs.append({
                    "a": a["ref"], "b": b["ref"], "side": a["side"],
                    "gap_mm": round(d, 3),
                    "source": [a["source"], b["source"]],
                })
            if a["courtyard"] and b["courtyard"] and overlap(a["courtyard"], b["courtyard"]):
                courtyard_only.append({
                    "a": a["ref"], "b": b["ref"], "side": a["side"],
                    "body_gap_mm": round(d, 3),
                    "source": [a["source"], b["source"]],
                })

    out["body_overlaps"] = body_overlaps
    out["close_pairs"] = close_pairs
    out["courtyard_only_overlaps"] = courtyard_only
    out["fallback_bodies"] = [{"ref": r["ref"], "source": r["source"]}
                              for r in rows if r["source"] != "3d"]
    out["summary"] = {
        "footprint_count": len(rows),
        "body_overlap_count": len(body_overlaps),
        "close_pair_count": len(close_pairs),
        "courtyard_only_overlap_count": len(courtyard_only),
        "fallback_body_count": len([r for r in rows if r["source"] != "3d"]),
        "min_gap_mm": min_gap,
    }
except Exception as exc:
    out = {"error": "{}: {}".format(type(exc).__name__, exc)}

print(json.dumps(out))
"""


def check_component_overlaps(pcb_path: str | Path, min_gap_mm: float = 0.0) -> dict:
    """Detect physical component body overlaps and near-touches.

    Body boxes use the footprint 3D model vertex bounding box when the model box
    is consistent with the fab outline (within 1 mm), otherwise the F.Fab/B.Fab
    outline, then the pads. Same-side pairs whose body boxes intersect are
    reported as overlaps; with min_gap_mm > 0 the remaining pairs closer than
    that are listed as close_pairs. Courtyard overlaps where the bodies do NOT
    touch are reported separately as cosmetic.
    """
    pcb = paths.normalize_path(pcb_path)
    if not pcb.is_file():
        raise FileNotFoundError(f"pcb not found: {pcb}")
    root = _model_root()
    result = pcb_edit._run_on_pcb(
        _OVERLAP_SCRIPT,
        pcb,
        {
            "min_gap_mm": float(min_gap_mm),
            "model_root": str(root) if root else "",
            "project_dir": str(pcb.parent),
        },
        timeout=900.0,
        write_back=False,
    )
    if not result.get("ok"):
        return {"ok": False, "error": result.get("result") or result}
    data = result["result"]
    return {
        "ok": True,
        "body_overlaps": data.get("body_overlaps", []),
        "close_pairs": data.get("close_pairs", []),
        "courtyard_only_overlaps": data.get("courtyard_only_overlaps", []),
        "fallback_bodies": data.get("fallback_bodies", []),
        "summary": data.get("summary", {}),
        "limitations": [
            "body boxes use the 3D model STEP vertex bounding box (leads included) when the "
            "model box stays within 1 mm of the fab outline; otherwise the fab outline is used, "
            "then pads (fab outlines may include actuator travel or keepouts)",
            "boxes are axis-aligned, so rotated parts are compared by their bounding box",
            "courtyard_only_overlaps are cosmetic: the courtyards touch but the bodies do not",
        ],
    }


_SILK_SCRIPT = r"""import json, math, sys
import pcbnew

MM = 1e6
p = json.load(open(sys.argv[1], encoding="utf-8"))
out = {}

try:
    board = pcbnew.LoadBoard(p["pcb"])
    min_gap = float(p.get("min_gap_mm", 0.0))
    include_text = bool(p.get("include_text", True))
    SILK = (pcbnew.F_SilkS, pcbnew.B_SilkS)

    def shape_rect(sh):
        try:
            bb = sh.BBox()
        except Exception:
            bb = sh
        return (bb.GetLeft() / MM, bb.GetTop() / MM,
                bb.GetRight() / MM, bb.GetBottom() / MM)

    def item_rect(it):
        # Text uses the effective stroke shape: GetBoundingBox() pads the glyph.
        if type(it).__name__ in ("PCB_TEXT", "PCB_FIELD"):
            return shape_rect(it.GetEffectiveTextShape())
        bb = it.GetBoundingBox()
        return (bb.GetLeft() / MM, bb.GetTop() / MM,
                bb.GetRight() / MM, bb.GetBottom() / MM)

    def kind_of(it):
        cls = type(it).__name__
        if cls in ("PCB_TEXT", "PCB_FIELD"):
            return "text"
        try:
            st = it.GetShape()
        except Exception:
            return cls
        return {
            pcbnew.SHAPE_T_SEGMENT: "segment",
            pcbnew.SHAPE_T_RECT: "rect",
            pcbnew.SHAPE_T_CIRCLE: "circle",
            pcbnew.SHAPE_T_ARC: "arc",
            pcbnew.SHAPE_T_POLY: "poly",
        }.get(st, cls)

    def text_of(it):
        if type(it).__name__ in ("PCB_TEXT", "PCB_FIELD"):
            try:
                return it.GetText()
            except Exception:
                return ""
        return ""

    def rect_edges(it):
        # A rectangle's silk is its outline, not the filled box.
        s = it.GetStart()
        e = it.GetEnd()
        x0, x1 = sorted((s.x, e.x))
        y0, y1 = sorted((s.y, e.y))
        w = it.GetWidth() / 2.0

        def box(ax, ay, bx, by):
            return ((min(ax, bx) - w) / MM, (min(ay, by) - w) / MM,
                    (max(ax, bx) + w) / MM, (max(ay, by) + w) / MM)

        return [box(x0, y0, x1, y0), box(x0, y1, x1, y1),
                box(x0, y0, x0, y1), box(x1, y0, x1, y1)]

    def prims(it):
        if kind_of(it) == "rect":
            try:
                return rect_edges(it)
            except Exception:
                pass
        return [item_rect(it)]

    items = []

    def add(owner, side, it):
        if type(it).__name__ == "PCB_TEXT" and not include_text:
            return
        r = item_rect(it)
        items.append({
            "owner": owner,
            "side": side,
            "kind": kind_of(it),
            "text": text_of(it),
            "rect": r,
            "prims": prims(it),
            "at": [round((r[0] + r[2]) / 2.0, 2), round((r[1] + r[3]) / 2.0, 2)],
        })

    for d in board.GetDrawings():
        if d.GetLayer() in SILK:
            add("BOARD", "F" if d.GetLayer() == pcbnew.F_SilkS else "B", d)

    for fp in board.GetFootprints():
        side = "F" if fp.GetLayer() == pcbnew.F_Cu else "B"
        for g in fp.GraphicalItems():
            if g.GetLayer() in SILK:
                add(fp.GetReference(), side, g)
        for name in fp.GetFieldsText():
            try:
                fld = fp.GetField(name)
            except Exception:
                continue
            if fld.GetLayer() in SILK and fld.IsVisible():
                add(fp.GetReference(), side, fld)

    pads = []
    for fp in board.GetFootprints():
        for pad in fp.Pads():
            bb = pad.GetBoundingBox()
            r = (bb.GetLeft() / MM, bb.GetTop() / MM,
                 bb.GetRight() / MM, bb.GetBottom() / MM)
            if pad.IsOnLayer(pcbnew.F_Cu):
                pads.append({"ref": fp.GetReference(), "num": pad.GetNumber(),
                             "side": "F", "rect": r})
            if pad.IsOnLayer(pcbnew.B_Cu):
                pads.append({"ref": fp.GetReference(), "num": pad.GetNumber(),
                             "side": "B", "rect": r})

    def inter(a, c):
        x1 = max(a[0], c[0])
        y1 = max(a[1], c[1])
        x2 = min(a[2], c[2])
        y2 = min(a[3], c[3])
        if x2 > x1 + 1e-4 and y2 > y1 + 1e-4:
            return (x2 - x1, y2 - y1)
        return None

    def gap(a, c):
        dx = max(a[0] - c[2], c[0] - a[2], 0.0)
        dy = max(a[1] - c[3], c[1] - a[3], 0.0)
        return math.hypot(dx, dy)

    def pair_overlap(a, b):
        best = None
        for pa in a["prims"]:
            for pb in b["prims"]:
                o = inter(pa, pb)
                if o and (best is None or o[0] * o[1] > best[0] * best[1]):
                    best = o
        return best

    def pair_gap(a, b):
        best = 1e9
        for pa in a["prims"]:
            for pb in b["prims"]:
                d = gap(pa, pb)
                if d < best:
                    best = d
        return best

    def same_rect(a, c, tol=0.03):
        return (abs(a[0] - c[0]) <= tol and abs(a[1] - c[1]) <= tol
                and abs(a[2] - c[2]) <= tol and abs(a[3] - c[3]) <= tol)

    overlaps = []
    duplicates = []
    close_pairs = []
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            a = items[i]
            b = items[j]
            if a["side"] != b["side"]:
                continue
            # Within one footprint outline vs notch is by design; keep text-text.
            if a["owner"] == b["owner"] and a["owner"] != "BOARD":
                if not (a["kind"] == "text" and b["kind"] == "text"):
                    continue
            o = pair_overlap(a, b)
            row = {
                "a": a["owner"], "b": b["owner"],
                "side": a["side"],
                "a_kind": a["kind"], "b_kind": b["kind"],
                "a_text": a["text"], "b_text": b["text"],
                "a_at": a["at"], "b_at": b["at"],
            }
            if o:
                row["overlap_mm"] = [round(o[0], 3), round(o[1], 3)]
                row["area_mm2"] = round(o[0] * o[1], 4)
                if same_rect(a["rect"], b["rect"]):
                    duplicates.append(row)
                else:
                    overlaps.append(row)
                continue
            d = pair_gap(a, b)
            if min_gap > 0 and d < min_gap:
                row["gap_mm"] = round(d, 3)
                close_pairs.append(row)

    over_pads = []
    for it in items:
        if it["kind"] in ("circle", "poly", "arc"):
            continue
        for pad in pads:
            if it["side"] != pad["side"]:
                continue
            # A footprint's own outline around its pads is normal.
            if it["owner"] == pad["ref"] and it["kind"] != "text":
                continue
            o = None
            for pr in it["prims"]:
                oo = inter(pr, pad["rect"])
                if oo and (o is None or oo[0] * oo[1] > o[0] * o[1]):
                    o = oo
            if o:
                over_pads.append({
                    "owner": it["owner"], "kind": it["kind"], "text": it["text"],
                    "at": it["at"], "side": it["side"],
                    "pad": pad["ref"] + "." + pad["num"],
                    "overlap_mm": [round(o[0], 3), round(o[1], 3)],
                })

    out["silk_overlaps"] = overlaps
    out["silk_duplicates"] = duplicates
    out["silk_close_pairs"] = close_pairs
    out["silk_over_pads"] = over_pads
    out["summary"] = {
        "item_count": len(items),
        "overlap_count": len(overlaps),
        "duplicate_count": len(duplicates),
        "close_pair_count": len(close_pairs),
        "over_pad_count": len(over_pads),
        "min_gap_mm": min_gap,
        "include_text": include_text,
    }
except Exception as exc:
    out = {"error": "{}: {}".format(type(exc).__name__, exc)}

print(json.dumps(out))
"""


def check_silk_overlaps(pcb_path: str | Path, min_gap_mm: float = 0.0,
                        include_text: bool = True) -> dict:
    """Detect silkscreen overlaps, coincident duplicates, close pairs and silk over pads.

    Every silk item (footprint graphics, visible text fields, board texts) is
    reduced to its tight bounding box (text uses the effective stroke shape, not
    the padded text box). Same-side pairs whose boxes intersect are reported as
    silk_overlaps, or as silk_duplicates when the boxes coincide. With
    min_gap_mm > 0, non-touching pairs closer than that are listed as
    silk_close_pairs. Silk items intersecting pads on the same side are listed
    as silk_over_pads.
    """
    pcb = paths.normalize_path(pcb_path)
    if not pcb.is_file():
        raise FileNotFoundError(f"pcb not found: {pcb}")
    result = pcb_edit._run_on_pcb(
        _SILK_SCRIPT,
        pcb,
        {"min_gap_mm": float(min_gap_mm), "include_text": bool(include_text)},
        timeout=600.0,
        write_back=False,
    )
    if not result.get("ok"):
        return {"ok": False, "error": result.get("result") or result}
    data = result["result"]
    return {
        "ok": True,
        "silk_overlaps": data.get("silk_overlaps", []),
        "silk_duplicates": data.get("silk_duplicates", []),
        "silk_close_pairs": data.get("silk_close_pairs", []),
        "silk_over_pads": data.get("silk_over_pads", []),
        "summary": data.get("summary", {}),
        "limitations": [
            "items are compared by axis-aligned bounding boxes; a diagonal line's box is "
            "larger than the line, so it can report a false touch",
            "text uses the effective stroke shape (glyph outline), not the padded text box",
            "segments belonging to the same footprint are skipped (outlines meet at corners)",
            "silk_over_pads uses the pad copper box (mask opening is larger)",
        ],
    }
