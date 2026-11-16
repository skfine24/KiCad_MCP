"""PCB editing and copper-connectivity analysis via KiCad's bundled pcbnew python.

Edit helpers (add_tracks / add_via / refill_zones) save the board in place and,
by default, write a timestamped .bak next to the file first. Analysis helpers
(find_isolated_islands) are read-only. All pcbnew scripts print exactly one
JSON line on stdout; SWIG chatter goes to stderr and is returned for diagnostics.

Note: pcbnew scripts must never call PCB_TRACK.GetWidth() without a layer
argument on vias - that raises a wx assert dialog and hangs the process.
Always pass an explicit layer for vias (t.GetWidth(pcbnew.F_Cu)).
"""

import json
import os
import shutil
import subprocess
import tempfile
import uuid
from datetime import datetime
from pathlib import Path

from . import kicad_cli

_DEFAULT_TIMEOUT = 300.0

_ADD_TRACKS_SCRIPT = r"""
import json, math, sys
import pcbnew

MM = 1e6
p = json.load(open(sys.argv[1], encoding="utf-8"))
out = {}

_LAYERS = {
    "F.Cu": pcbnew.F_Cu, "B.Cu": pcbnew.B_Cu,
    "F.SilkS": pcbnew.F_SilkS, "B.SilkS": pcbnew.B_SilkS,
    "F.Mask": pcbnew.F_Mask, "B.Mask": pcbnew.B_Mask,
    "F.Paste": pcbnew.F_Paste, "B.Paste": pcbnew.B_Paste,
    "F.Adhes": pcbnew.F_Adhes, "B.Adhes": pcbnew.B_Adhes,
    "Edge.Cuts": pcbnew.Edge_Cuts, "F.CrtYd": pcbnew.F_CrtYd,
    "B.CrtYd": pcbnew.B_CrtYd, "F.Fab": pcbnew.F_Fab, "B.Fab": pcbnew.B_Fab,
}


def _layer(name):
    if name in _LAYERS:
        return _LAYERS[name]
    try:
        return pcbnew.GetLayer(name)
    except Exception:
        return None


try:
    board = pcbnew.LoadBoard(p["pcb"])
    net = board.FindNet(p["net"])
    if net is None:
        names = set()
        for t in board.GetTracks():
            if t.GetNetname():
                names.add(t.GetNetname())
        for fp in board.GetFootprints():
            for pad in fp.Pads():
                if pad.GetNetname():
                    names.add(pad.GetNetname())
        out = {"error": "net_not_found", "net": p["net"], "available_nets": sorted(names)}
    else:
        layer_id = _layer(p["layer"])
        if layer_id is None:
            out = {"error": "bad_layer", "layer": p["layer"]}
        else:
            pts = p["points"]
            width = int(round(p["width"] * MM))
            added = 0
            uuids = []
            for i in range(len(pts) - 1):
                t = pcbnew.PCB_TRACK(board)
                t.SetStart(pcbnew.VECTOR2I(int(round(pts[i][0] * MM)), int(round(pts[i][1] * MM))))
                t.SetEnd(pcbnew.VECTOR2I(int(round(pts[i + 1][0] * MM)), int(round(pts[i + 1][1] * MM))))
                t.SetWidth(width)
                t.SetLayer(layer_id)
                t.SetNetCode(net.GetNetCode())
                board.Add(t)
                added += 1
                uuids.append(t.m_Uuid.AsString())
            pcbnew.SaveBoard(p["pcb"], board)
            out = {
                "added_segments": added,
                "uuids": uuids,
                "net": net.GetNetname(),
                "layer": p["layer"],
                "width_mm": p["width"],
            }
except Exception as exc:
    out = {"error": "exception", "message": "{}: {}".format(type(exc).__name__, exc)}
print(json.dumps(out))
"""

_ADD_VIA_SCRIPT = r"""
import json, sys
import pcbnew

MM = 1e6
p = json.load(open(sys.argv[1], encoding="utf-8"))
out = {}

try:
    board = pcbnew.LoadBoard(p["pcb"])
    net = board.FindNet(p["net"])
    if net is None:
        names = set()
        for t in board.GetTracks():
            if t.GetNetname():
                names.add(t.GetNetname())
        for fp in board.GetFootprints():
            for pad in fp.Pads():
                if pad.GetNetname():
                    names.add(pad.GetNetname())
        out = {"error": "net_not_found", "net": p["net"], "available_nets": sorted(names)}
    else:
        v = pcbnew.PCB_VIA(board)
        v.SetPosition(pcbnew.VECTOR2I(int(round(p["x"] * MM)), int(round(p["y"] * MM))))
        v.SetWidth(int(round(p["diameter"] * MM)))
        v.SetDrill(int(round(p["drill"] * MM)))
        v.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
        v.SetNetCode(net.GetNetCode())
        board.Add(v)
        pcbnew.SaveBoard(p["pcb"], board)
        out = {"via_added": True, "uuid": v.m_Uuid.AsString(), "at_mm": [p["x"], p["y"]],
               "diameter_mm": p["diameter"], "drill_mm": p["drill"], "net": net.GetNetname()}
except Exception as exc:
    out = {"error": "exception", "message": "{}: {}".format(type(exc).__name__, exc)}
print(json.dumps(out))
"""

_REFILL_SCRIPT = r"""
import json, sys
import pcbnew

p = json.load(open(sys.argv[1], encoding="utf-8"))
out = {}
try:
    board = pcbnew.LoadBoard(p["pcb"])
    zones = board.Zones()
    filler = pcbnew.ZONE_FILLER(board)
    ok = filler.Fill(zones)
    pcbnew.SaveBoard(p["pcb"], board)
    out = {"refilled": bool(ok), "zone_count": len(list(zones))}
except Exception as exc:
    out = {"error": "exception", "message": "{}: {}".format(type(exc).__name__, exc)}
print(json.dumps(out))
"""

_ISLANDS_SCRIPT = r"""
import json, math, sys
import pcbnew

MM = 1e6
EPS = 0.05
p = json.load(open(sys.argv[1], encoding="utf-8"))
out = {}

try:
    board = pcbnew.LoadBoard(p["pcb"])
    net_name = p["net"]

    pads, vias, tracks = [], [], []
    for fp in board.GetFootprints():
        for pad in fp.Pads():
            pos = pad.GetPosition()
            pads.append({
                "id": ("pad", fp.GetReference(), pad.GetPadName()),
                "x": pos.x / MM, "y": pos.y / MM,
                "hw": max(pad.GetSizeX(), pad.GetSizeY()) / 2.0 / MM,
                "net": pad.GetNetname(),
            })
    for t in board.GetTracks():
        if t.Type() == pcbnew.PCB_TRACE_T:
            s, e = t.GetStart(), t.GetEnd()
            tracks.append({
                "id": ("trk", len(tracks)),
                "x1": s.x / MM, "y1": s.y / MM, "x2": e.x / MM, "y2": e.y / MM,
                "layer": pcbnew.LayerName(t.GetLayer()),
                "net": t.GetNetname(),
            })
        else:
            pos = t.GetPosition()
            vias.append({
                "id": ("via", round(pos.x / MM, 3), round(pos.y / MM, 3)),
                "x": pos.x / MM, "y": pos.y / MM,
                "hw": t.GetWidth(pcbnew.F_Cu) / 2.0 / MM,
                "net": t.GetNetname(),
            })
    items = [i for i in (pads + vias + tracks) if i["net"] == net_name]

    islands = []
    copper_lids = [lid for lid in range(32) if pcbnew.IsCopperLayer(lid)]
    for zi, zone in enumerate(board.Zones()):
        for lid in copper_lids:
            if not zone.IsOnLayer(lid):
                continue
            polys = zone.GetFilledPolysList(lid)
            for k in range(polys.OutlineCount()):
                bb = polys.BBox(k)
                p0, p1 = bb.GetPosition(), bb.GetEnd()
                try:
                    area = abs(polys.COutline(k).Area()) / MM / MM
                except Exception:
                    area = max(0.0, (p1.x - p0.x) / MM) * max(0.0, (p1.y - p0.y) / MM)
                islands.append({
                    "zone_index": zi, "layer": pcbnew.LayerName(lid), "island": k,
                    "polys": polys,
                    "bbox": [p0.x / MM, p0.y / MM, p1.x / MM, p1.y / MM],
                    "area": area,
                })

    def inside(isl, x, y):
        try:
            return bool(isl["polys"].Contains(
                pcbnew.VECTOR2I(int(round(x * MM)), int(round(y * MM))), isl["island"]))
        except Exception:
            return False

    def circle_touch(isl, item):
        if inside(isl, item["x"], item["y"]):
            return True
        # ring just inside the copper edge catches solid fill overlap...
        r = item["hw"] * 0.99
        for a in range(8):
            ang = a * math.pi / 4.0
            if inside(isl, item["x"] + r * math.cos(ang), item["y"] + r * math.sin(ang)):
                return True
        # ...and a ring slightly outside the copper edge crosses thermal spokes,
        # which span the clearance annulus; stays inside the clearance gap
        # (default 0.2 mm) so an unconnected pad/via is never a false hit
        r = item["hw"] + 0.1
        for a in range(36):
            ang = a * math.pi / 18.0
            if inside(isl, item["x"] + r * math.cos(ang), item["y"] + r * math.sin(ang)):
                return True
        return False

    def track_touch(isl, trk):
        # zone fill keeps a clearance slot around tracks, so only the endpoints
        # (pad/via centers) can be inside the fill - sample those
        return inside(isl, trk["x1"], trk["y1"]) or inside(isl, trk["x2"], trk["y2"])

    island_items = [[] for _ in islands]
    item_touched = [False] * len(items)
    for ii, isl in enumerate(islands):
        for idx, item in enumerate(items):
            touched = track_touch(isl, item) if "x1" in item else circle_touch(isl, item)
            if touched:
                island_items[ii].append(idx)
                item_touched[idx] = True

    # spatial hash over point contact sites for item-item connectivity
    CELL = 2.0

    def cell_of(x, y):
        return (int(x // CELL), int(y // CELL))

    grid = {}

    def add_point_site(x, y, idx):
        grid.setdefault(cell_of(x, y), []).append(idx)

    def near_nodes(x, y):
        cx, cy = cell_of(x, y)
        res = []
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                res.extend(grid.get((cx + dx, cy + dy), []))
        return res

    for idx, it in enumerate(items):
        if "x1" in it:
            x0, x1 = min(it["x1"], it["x2"]), max(it["x1"], it["x2"])
            y0, y1 = min(it["y1"], it["y2"]), max(it["y1"], it["y2"])
            cells = set()
            for cx in range(int(x0 // CELL) - 1, int(x1 // CELL) + 2):
                for cy in range(int(y0 // CELL) - 1, int(y1 // CELL) + 2):
                    cells.add((cx, cy))
            if len(cells) <= 256:
                for c in cells:
                    grid.setdefault(c, []).append(idx)
            else:
                add_point_site(it["x1"], it["y1"], idx)
                add_point_site(it["x2"], it["y2"], idx)
        else:
            add_point_site(it["x"], it["y"], idx)

    def dist_point_seg(px, py, x1, y1, x2, y2):
        dx, dy = x2 - x1, y2 - y1
        L2 = dx * dx + dy * dy
        if L2 == 0.0:
            return math.hypot(px - x1, py - y1)
        t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / L2))
        return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))

    parent = list(range(len(islands) + len(items)))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    ISL_OFF = len(items)
    for ii, idxs in enumerate(island_items):
        for idx in idxs:
            union(ISL_OFF + ii, idx)

    for i, it in enumerate(items):
        if "x1" in it:
            cand = set(near_nodes(it["x1"], it["y1"])) | set(near_nodes(it["x2"], it["y2"]))
        else:
            cand = set(near_nodes(it["x"], it["y"]))
        cand.discard(i)
        for j in cand:
            other = items[j]
            if "x1" in it and "x1" in other:
                d = min(
                    dist_point_seg(it["x1"], it["y1"], other["x1"], other["y1"], other["x2"], other["y2"]),
                    dist_point_seg(it["x2"], it["y2"], other["x1"], other["y1"], other["x2"], other["y2"]),
                )
            elif "x1" in it:
                d = dist_point_seg(other["x"], other["y"], it["x1"], it["y1"], it["x2"], it["y2"])
            elif "x1" in other:
                d = dist_point_seg(it["x"], it["y"], other["x1"], other["y1"], other["x2"], other["y2"])
            else:
                d = math.hypot(it["x"] - other["x"], it["y"] - other["y"])
            if d <= EPS:
                union(i, j)

    comps = {}
    for a in range(len(parent)):
        comps.setdefault(find(a), []).append(a)

    def item_label(iid):
        if iid[0] == "pad":
            return "{}.{}".format(iid[1], iid[2])
        if iid[0] == "via":
            return "via@({:.2f},{:.2f})".format(iid[1], iid[2])
        return "trk{}".format(iid[1])

    out_groups = []
    for members in comps.values():
        isl_idx = [m - ISL_OFF for m in members if m >= ISL_OFF]
        item_idx = [m for m in members if m < ISL_OFF]
        g_pads, g_vias = set(), set()
        g_trk = 0
        area = 0.0
        for ii in isl_idx:
            area += islands[ii]["area"]
            for idx in island_items[ii]:
                iid = items[idx]["id"]
                if iid[0] == "pad":
                    g_pads.add(item_label(iid))
                elif iid[0] == "via":
                    g_vias.add(item_label(iid))
                else:
                    g_trk += 1
        for m in item_idx:
            iid = items[m]["id"]
            if iid[0] == "pad":
                g_pads.add(item_label(iid))
            elif iid[0] == "via":
                g_vias.add(item_label(iid))
            else:
                g_trk += 1
        out_groups.append({
            "island_count": len(isl_idx),
            "islands": [
                {"layer": islands[ii]["layer"], "island": islands[ii]["island"],
                 "zone_index": islands[ii]["zone_index"], "bbox_mm": islands[ii]["bbox"]}
                for ii in isl_idx
            ],
            "pads": sorted(g_pads),
            "vias": sorted(g_vias),
            "track_count": g_trk,
            "fill_area_mm2": round(area, 2),
        })

    if out_groups:
        main = max(range(len(out_groups)),
                   key=lambda i: (out_groups[i]["fill_area_mm2"],
                                  len(out_groups[i]["pads"]) + len(out_groups[i]["vias"])))
        for gi, g in enumerate(out_groups):
            g["is_main"] = gi == main
            g["isolated"] = gi != main
        out_groups.sort(key=lambda g: (not g["is_main"], -g["fill_area_mm2"]))

    out = {
        "net": net_name,
        "island_count": len(islands),
        "group_count": len(out_groups),
        "isolated_group_count": max(0, len(out_groups) - 1) if out_groups else 0,
        "groups": out_groups,
        "islands_detail": [
            {
                "layer": islands[ii]["layer"], "island": islands[ii]["island"],
                "zone_index": islands[ii]["zone_index"], "area_mm2": round(islands[ii]["area"], 2),
                "bbox_mm": islands[ii]["bbox"],
                "items": [item_label(items[idx]["id"]) for idx in island_items[ii]],
            }
            for ii in range(len(islands))
        ],
        "items_without_island": [item_label(items[i]["id"]) for i in range(len(items)) if not item_touched[i]],
    }
except Exception as exc:
    out = {"error": "exception", "message": "{}: {}".format(type(exc).__name__, exc)}
print(json.dumps(out))

"""

_DUPLICATE_TRACKS_SCRIPT = r"""
import json, math, sys
import pcbnew

MM = 1e6
EPS_MM = 1e-4
PARALLEL_REL = 1e-6
p = json.load(open(sys.argv[1], encoding="utf-8"))
out = {}

try:
    board = pcbnew.LoadBoard(p["pcb"])
    net_filter = p.get("net") or None
    max_gap = float(p.get("max_gap_mm", 1.0))
    min_overlap = float(p.get("min_overlap_mm", 2.0))

    segs, vias, arc_count = [], [], 0
    for t in board.GetTracks():
        ttype = t.Type()
        if ttype == pcbnew.PCB_VIA_T:
            pos = t.GetPosition()
            vias.append({
                "uuid": t.m_Uuid.AsString(),
                "x_nm": int(pos.x), "y_nm": int(pos.y),
                "x_mm": pos.x / MM, "y_mm": pos.y / MM,
                "diameter_nm": int(t.GetFrontWidth()),
                "diameter_mm": t.GetFrontWidth() / MM,
                "drill_nm": int(t.GetDrillValue()),
                "drill_mm": t.GetDrillValue() / MM,
                "layers": [pcbnew.LayerName(t.TopLayer()), pcbnew.LayerName(t.BottomLayer())],
                "net": t.GetNetname(),
            })
        elif ttype == pcbnew.PCB_TRACE_T:
            s, e = t.GetStart(), t.GetEnd()
            segs.append({
                "uuid": t.m_Uuid.AsString(),
                "x1_nm": int(s.x), "y1_nm": int(s.y),
                "x2_nm": int(e.x), "y2_nm": int(e.y),
                "x1_mm": s.x / MM, "y1_mm": s.y / MM,
                "x2_mm": e.x / MM, "y2_mm": e.y / MM,
                "width_nm": int(t.GetWidth()),
                "width_mm": t.GetWidth() / MM,
                "layer": pcbnew.LayerName(t.GetLayer()),
                "net": t.GetNetname(),
            })
        elif ttype == pcbnew.PCB_ARC_T:
            arc_count += 1

    if net_filter is not None:
        segs = [s for s in segs if s["net"] == net_filter]
        vias = [v for v in vias if v["net"] == net_filter]

    exact = []
    groups = {}
    for s in segs:
        ends = tuple(sorted(((s["x1_nm"], s["y1_nm"]), (s["x2_nm"], s["y2_nm"]))))
        key = (s["layer"], s["net"], s["width_nm"], ends)
        groups.setdefault(key, []).append(s)
    for g in groups.values():
        if len(g) < 2:
            continue
        a = g[0]
        exact.append({
            "layer": a["layer"], "net": a["net"], "width_mm": a["width_mm"],
            "start_mm": [a["x1_mm"], a["y1_mm"]], "end_mm": [a["x2_mm"], a["y2_mm"]],
            "count": len(g), "uuids": [x["uuid"] for x in g],
        })
    exact.sort(key=lambda r: (-r["count"], r["net"], r["layer"]))

    dup_vias = []
    vgroups = {}
    for v in vias:
        key = (v["net"], v["x_nm"], v["y_nm"], v["diameter_nm"], v["drill_nm"], tuple(v["layers"]))
        vgroups.setdefault(key, []).append(v)
    for g in vgroups.values():
        if len(g) < 2:
            continue
        a = g[0]
        dup_vias.append({
            "net": a["net"], "at_mm": [a["x_mm"], a["y_mm"]],
            "diameter_mm": a["diameter_mm"], "drill_mm": a["drill_mm"],
            "layers": a["layers"], "count": len(g), "uuids": [x["uuid"] for x in g],
        })

    zero = [{"layer": s["layer"], "net": s["net"], "width_mm": s["width_mm"],
             "at_mm": [s["x1_mm"], s["y1_mm"]], "uuid": s["uuid"]}
            for s in segs if s["x1_nm"] == s["x2_nm"] and s["y1_nm"] == s["y2_nm"]]

    def geom(s):
        dx = s["x2_mm"] - s["x1_mm"]
        dy = s["y2_mm"] - s["y1_mm"]
        return dx, dy, math.hypot(dx, dy)

    def seg_info(s):
        return {"uuid": s["uuid"], "start_mm": [s["x1_mm"], s["y1_mm"]],
                "end_mm": [s["x2_mm"], s["y2_mm"]], "width_mm": s["width_mm"]}

    collinear = []
    cgroups = {}
    for s in segs:
        cgroups.setdefault((s["layer"], s["net"], s["width_nm"]), []).append(s)
    for (layer, net, width_nm), arr in cgroups.items():
        for i in range(len(arr)):
            s1 = arr[i]
            dx1, dy1, L1 = geom(s1)
            if L1 <= EPS_MM:
                continue
            ux, uy = dx1 / L1, dy1 / L1
            for j in range(i + 1, len(arr)):
                s2 = arr[j]
                dx2, dy2, L2 = geom(s2)
                if L2 <= EPS_MM:
                    continue
                if abs(dx1 * dy2 - dy1 * dx2) > PARALLEL_REL * L1 * L2:
                    continue
                off1 = abs((s2["x1_mm"] - s1["x1_mm"]) * uy - (s2["y1_mm"] - s1["y1_mm"]) * ux)
                off2 = abs((s2["x2_mm"] - s1["x1_mm"]) * uy - (s2["y2_mm"] - s1["y1_mm"]) * ux)
                if max(off1, off2) > EPS_MM:
                    continue
                t3 = (s2["x1_mm"] - s1["x1_mm"]) * ux + (s2["y1_mm"] - s1["y1_mm"]) * uy
                t4 = (s2["x2_mm"] - s1["x1_mm"]) * ux + (s2["y2_mm"] - s1["y1_mm"]) * uy
                lo = max(min(t3, t4), 0.0)
                hi = min(max(t3, t4), L1)
                ov = hi - lo
                if ov > EPS_MM:
                    collinear.append({"layer": layer, "net": net,
                                      "width_mm": width_nm / MM,
                                      "overlap_mm": round(ov, 4),
                                      "a": seg_info(s1), "b": seg_info(s2)})
    collinear.sort(key=lambda r: -r["overlap_mm"])

    parallel = []
    pgroups = {}
    for s in segs:
        pgroups.setdefault((s["layer"], s["net"]), []).append(s)
    for (layer, net), arr in pgroups.items():
        for i in range(len(arr)):
            s1 = arr[i]
            dx1, dy1, L1 = geom(s1)
            if L1 <= EPS_MM:
                continue
            ux, uy = dx1 / L1, dy1 / L1
            for j in range(i + 1, len(arr)):
                s2 = arr[j]
                dx2, dy2, L2 = geom(s2)
                if L2 <= EPS_MM:
                    continue
                if abs(dx1 * dy2 - dy1 * dx2) > PARALLEL_REL * L1 * L2:
                    continue
                off1 = (s2["x1_mm"] - s1["x1_mm"]) * uy - (s2["y1_mm"] - s1["y1_mm"]) * ux
                off2 = (s2["x2_mm"] - s1["x1_mm"]) * uy - (s2["y2_mm"] - s1["y1_mm"]) * ux
                gap = (abs(off1) + abs(off2)) / 2.0
                if gap <= EPS_MM or gap > max_gap:
                    continue
                if abs(abs(off1) - abs(off2)) > 1e-3:
                    continue
                t3 = (s2["x1_mm"] - s1["x1_mm"]) * ux + (s2["y1_mm"] - s1["y1_mm"]) * uy
                t4 = (s2["x2_mm"] - s1["x1_mm"]) * ux + (s2["y2_mm"] - s1["y1_mm"]) * uy
                lo = max(min(t3, t4), 0.0)
                hi = min(max(t3, t4), L1)
                ov = hi - lo
                if ov < min_overlap:
                    continue
                parallel.append({
                    "layer": layer, "net": net,
                    "gap_mm": round(gap, 4),
                    "copper_gap_mm": round(gap - (s1["width_mm"] + s2["width_mm"]) / 2.0, 4),
                    "overlap_mm": round(ov, 4),
                    "a": seg_info(s1), "b": seg_info(s2),
                })
    parallel.sort(key=lambda r: (-r["overlap_mm"], r["gap_mm"]))

    dangling = []
    single_layer_vias = []
    isolated_vias = []
    item_by_uuid = {}
    conn = None
    try:
        conn = board.GetConnectivity()
        conn.Build(board)
        for t in board.GetTracks():
            item_by_uuid[t.m_Uuid.AsString()] = t
        for s in segs:
            t = item_by_uuid.get(s["uuid"])
            if t is None:
                continue
            pos = pcbnew.VECTOR2I(0, 0)
            try:
                if conn.TestTrackEndpointDangling(t, False, pos):
                    dangling.append({
                        "layer": s["layer"], "net": s["net"], "uuid": s["uuid"],
                        "dangling_end_mm": [pos.x / MM, pos.y / MM],
                        "start_mm": [s["x1_mm"], s["y1_mm"]],
                        "end_mm": [s["x2_mm"], s["y2_mm"]],
                    })
            except Exception:
                pass
        for v in vias:
            obj = item_by_uuid.get(v["uuid"])
            if obj is None:
                continue
            layers = []
            try:
                if conn.IsConnectedOnLayer(obj, pcbnew.F_Cu):
                    layers.append("F.Cu")
                if conn.IsConnectedOnLayer(obj, pcbnew.B_Cu):
                    layers.append("B.Cu")
            except Exception:
                continue
            rec = {
                "net": v["net"], "uuid": v["uuid"],
                "at_mm": [v["x_mm"], v["y_mm"]],
                "connected_layers": layers,
            }
            if len(layers) == 1:
                single_layer_vias.append(rec)
            elif len(layers) == 0:
                isolated_vias.append(rec)
    except Exception:
        conn = None

    # classify parallel pairs: "loop" (cycle) vs "series" (bridge/jog)
    if conn is not None and parallel:
        try:
            sys.setrecursionlimit(20000)

            def _net_graph(net_name):
                adj = {}
                for s in segs:
                    if s["net"] == net_name:
                        adj.setdefault(s["uuid"], set())
                for v in vias:
                    if v["net"] == net_name:
                        adj.setdefault(v["uuid"], set())
                for fp in board.GetFootprints():
                    for pad in fp.Pads():
                        if pad.GetNetname() == net_name:
                            adj.setdefault(pad.m_Uuid.AsString(), set())
                for u in list(adj.keys()):
                    obj = item_by_uuid.get(u)
                    if obj is None:
                        continue
                    for nb in conn.GetConnectedTracks(obj):
                        w = nb.m_Uuid.AsString()
                        if w in adj:
                            adj[u].add(w)
                            adj[w].add(u)
                    for pad in conn.GetConnectedPads(obj):
                        w = pad.m_Uuid.AsString()
                        if w in adj:
                            adj[u].add(w)
                            adj[w].add(u)
                return adj

            def _biconnected(adj):
                disc = {}
                low = {}
                parent = {}
                stack = []
                comps = []
                counter = [0]

                def dfs(u):
                    disc[u] = low[u] = counter[0]
                    counter[0] += 1
                    for w in adj.get(u, ()):
                        if w not in disc:
                            parent[w] = u
                            stack.append((u, w))
                            dfs(w)
                            low[u] = min(low[u], low[w])
                            if low[w] >= disc[u]:
                                comp = []
                                while True:
                                    e = stack.pop()
                                    comp.append(e)
                                    if e == (u, w):
                                        break
                                comps.append(comp)
                        elif w != parent.get(u) and disc[w] < disc[u]:
                            stack.append((u, w))
                            low[u] = min(low[u], disc[w])

                for u in adj:
                    if u not in disc:
                        dfs(u)
                vert_comps = {}
                for i, comp in enumerate(comps):
                    verts = set()
                    for a, b in comp:
                        verts.add(a)
                        verts.add(b)
                    for v in verts:
                        vert_comps.setdefault(v, set()).add(i)
                return vert_comps, comps

            graphs = {}
            for pair in parallel:
                graphs.setdefault(pair["net"], _net_graph(pair["net"]))
            classified = {}
            for net_name, adj in graphs.items():
                classified[net_name] = _biconnected(adj)

            for pair in parallel:
                vert_comps, comps = classified[pair["net"]]
                ca = vert_comps.get(pair["a"]["uuid"], set())
                cb = vert_comps.get(pair["b"]["uuid"], set())
                shared = ca & cb
                is_loop = any(len(comps[i]) >= 2 for i in shared)
                pair["classification"] = "loop" if is_loop else "series"
        except Exception:
            for pair in parallel:
                pair.setdefault("classification", "unknown")

    out = {
        "pcb": p["pcb"],
        "net_filter": net_filter,
        "params": {"max_gap_mm": max_gap, "min_overlap_mm": min_overlap},
        "summary": {
            "segment_count": len(segs),
            "via_count": len(vias),
            "arc_count": arc_count,
            "exact_duplicate_groups": len(exact),
            "duplicate_via_groups": len(dup_vias),
            "collinear_overlap_pairs": len(collinear),
            "parallel_pair_count": len(parallel),
            "zero_length_count": len(zero),
            "dangling_end_count": len(dangling),
            "single_layer_via_count": len(single_layer_vias),
            "isolated_via_count": len(isolated_vias),
        },
        "exact_duplicate_segments": exact,
        "duplicate_vias": dup_vias,
        "collinear_overlap_pairs": collinear,
        "parallel_pairs": parallel,
        "zero_length_segments": zero,
        "dangling_ends": dangling,
        "single_layer_vias": single_layer_vias,
        "isolated_vias": isolated_vias,
    }
    limitations = []
    if arc_count:
        limitations.append("{} arc track(s) not analyzed".format(arc_count))
    if parallel:
        limitations.append("parallel-pair classification uses track/via/pad connectivity; zone connections are not included")
    if limitations:
        out["limitations"] = limitations
except Exception as exc:
    out = {"error": "exception", "message": "{}: {}".format(type(exc).__name__, exc)}
print(json.dumps(out))

"""

_DELETE_TRACKS_SCRIPT = r"""
import json, sys
import pcbnew

p = json.load(open(sys.argv[1], encoding="utf-8"))
out = {}
try:
    board = pcbnew.LoadBoard(p["pcb"])
    targets = {}
    for t in board.GetTracks():
        targets[t.m_Uuid.AsString()] = t
    removed = []
    missing = []
    for u in p["uuids"]:
        t = targets.get(u)
        if t is None:
            missing.append(u)
        else:
            board.Remove(t)
            removed.append(u)
    pcbnew.SaveBoard(p["pcb"], board)
    out = {"removed": removed, "removed_count": len(removed), "missing": missing}
except Exception as exc:
    out = {"error": "exception", "message": "{}: {}".format(type(exc).__name__, exc)}
print(json.dumps(out))
"""

_TRIM_TRACK_SCRIPT = r"""
import json, sys
import pcbnew

MM = 1e6
p = json.load(open(sys.argv[1], encoding="utf-8"))
out = {}
try:
    board = pcbnew.LoadBoard(p["pcb"])
    target = None
    for t in board.GetTracks():
        if t.Type() == pcbnew.PCB_VIA_T:
            continue
        if t.m_Uuid.AsString() == p["uuid"]:
            target = t
            break
    if target is None:
        out = {"error": "track_not_found", "uuid": p["uuid"]}
    else:
        s, e = target.GetStart(), target.GetEnd()
        frm = p["from_mm"]
        to = p["to_mm"]
        ds = (pcbnew.ToMM(s.x) - frm[0]) ** 2 + (pcbnew.ToMM(s.y) - frm[1]) ** 2
        de = (pcbnew.ToMM(e.x) - frm[0]) ** 2 + (pcbnew.ToMM(e.y) - frm[1]) ** 2
        if ds <= de:
            target.SetStart(pcbnew.VECTOR2I(int(round(to[0] * MM)), int(round(to[1] * MM))))
            which = "start"
        else:
            target.SetEnd(pcbnew.VECTOR2I(int(round(to[0] * MM)), int(round(to[1] * MM))))
            which = "end"
        pcbnew.SaveBoard(p["pcb"], board)
        out = {
            "trimmed": True, "uuid": p["uuid"], "endpoint": which,
            "start_mm": [pcbnew.ToMM(target.GetStart().x), pcbnew.ToMM(target.GetStart().y)],
            "end_mm": [pcbnew.ToMM(target.GetEnd().x), pcbnew.ToMM(target.GetEnd().y)],
        }
except Exception as exc:
    out = {"error": "exception", "message": "{}: {}".format(type(exc).__name__, exc)}
print(json.dumps(out))
"""

_PRUNE_ONE_SCRIPT = r"""
import json, sys
import pcbnew

p = json.load(open(sys.argv[1], encoding="utf-8"))
out = {}
try:
    board = pcbnew.LoadBoard(p["pcb"])
    conn = board.GetConnectivity()
    conn.Build(board)
    protect_pads = bool(p.get("protect_pads", True))
    victim = None
    for t in board.GetTracks():
        if t.Type() != pcbnew.PCB_TRACE_T:
            continue
        if protect_pads:
            try:
                if conn.GetConnectedPads(t):
                    continue
            except Exception:
                pass
        pos = pcbnew.VECTOR2I(0, 0)
        try:
            if conn.TestTrackEndpointDangling(t, False, pos):
                victim = t
                break
        except Exception:
            pass
    if victim is None:
        out = {"pruned": False, "uuid": None}
    else:
        uid = victim.m_Uuid.AsString()
        board.Remove(victim)
        pcbnew.SaveBoard(p["pcb"], board)
        out = {"pruned": True, "uuid": uid}
except Exception as exc:
    out = {"error": "exception", "message": "{}: {}".format(type(exc).__name__, exc)}
print(json.dumps(out))
"""


def _run_script(script: str, params: dict, timeout: float = _DEFAULT_TIMEOUT) -> dict:
    tmp_dir = Path(tempfile.gettempdir()) / "kicad-mcp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    script_path = tmp_dir / f"pcb_{token}.py"
    params_path = tmp_dir / f"pcb_{token}.json"
    script_path.write_text(script, encoding="utf-8")
    params_path.write_text(json.dumps(params), encoding="utf-8")
    kpython = kicad_cli.find_kicad_python()
    try:
        proc = subprocess.run(
            [str(kpython), str(script_path), str(params_path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    finally:
        for f in (script_path, params_path):
            try:
                f.unlink()
            except OSError:
                pass
    result: dict = {"returncode": proc.returncode, "stderr": proc.stderr or None}
    stdout_lines = [line for line in (proc.stdout or "").strip().splitlines() if line.strip()]
    parsed = None
    for line in reversed(stdout_lines):
        try:
            parsed = json.loads(line)
            break
        except json.JSONDecodeError:
            continue
    if parsed is None:
        result["ok"] = False
        result["stdout"] = (proc.stdout or "")[-2000:]
        return result
    result["ok"] = parsed.get("error") is None
    result["result"] = parsed
    return result


def _require_pcb(path: str | Path) -> Path:
    p = Path(path).expanduser().resolve()
    if not p.is_file():
        raise FileNotFoundError(f"pcb not found: {p}")
    if p.suffix.lower() not in (".kicad_pcb", ".bak"):
        raise ValueError(f"expected .kicad_pcb file, got: {p.name}")
    return p


def _run_on_pcb(script: str, pcb: Path, params: dict, timeout: float, write_back: bool) -> dict:
    """Run a pcbnew script against a board file. pcbnew.LoadBoard only accepts
    .kicad_pcb names, so any other suffix (.bak backups, ...) is analyzed on a
    local temp .kicad_pcb copy; the copy is written back on success for edits."""
    tmp_copy = None
    try:
        if pcb.suffix.lower() != ".kicad_pcb":
            tmp_dir = Path(tempfile.gettempdir()) / "kicad-mcp"
            tmp_dir.mkdir(parents=True, exist_ok=True)
            tmp_copy = tmp_dir / f"{uuid.uuid4().hex}{pcb.name}.kicad_pcb"
            shutil.copy2(pcb, tmp_copy)
            run_path = tmp_copy
        else:
            run_path = pcb
        result = _run_script(script, {**params, "pcb": str(run_path)}, timeout=timeout)
        if write_back and tmp_copy is not None and result.get("ok"):
            shutil.copy2(tmp_copy, pcb)
        return result
    finally:
        if tmp_copy is not None:
            try:
                tmp_copy.unlink()
            except OSError:
                pass


def _backup(pcb: Path) -> str | None:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    bak = pcb.with_name(f"{pcb.name}.{stamp}_pre_edit.bak")
    shutil.copy2(pcb, bak)
    return str(bak)


def add_tracks(pcb_path: str | Path, points: list, layer: str, width: float, net: str,
               backup: bool = True) -> dict:
    """Add track segments along a polyline. points=[[x,y],...] in mm, layer like 'F.Cu'/'B.Cu', width in mm, net by name. Saves in place (timestamped .bak first by default)."""
    pcb = _require_pcb(pcb_path)
    if not isinstance(points, list) or len(points) < 2:
        raise ValueError("points must be a polyline with at least 2 [x, y] pairs (mm)")
    for pt in points:
        if not (isinstance(pt, (list, tuple)) and len(pt) == 2):
            raise ValueError(f"bad point: {pt!r} (expected [x, y] in mm)")
    bak = _backup(pcb) if backup else None
    result = _run_on_pcb(_ADD_TRACKS_SCRIPT, pcb, {
        "points": [[float(pt[0]), float(pt[1])] for pt in points],
        "layer": layer, "width": float(width), "net": net,
    }, timeout=_DEFAULT_TIMEOUT, write_back=True)
    result["backup"] = bak
    return result


def add_via(pcb_path: str | Path, x: float, y: float, net: str, diameter: float = 0.6,
            drill: float = 0.3, backup: bool = True) -> dict:
    """Add a through via at (x, y) mm with the given net. Saves in place (timestamped .bak first by default)."""
    pcb = _require_pcb(pcb_path)
    bak = _backup(pcb) if backup else None
    result = _run_on_pcb(_ADD_VIA_SCRIPT, pcb, {
        "x": float(x), "y": float(y), "net": net,
        "diameter": float(diameter), "drill": float(drill),
    }, timeout=_DEFAULT_TIMEOUT, write_back=True)
    result["backup"] = bak
    return result


def refill_zones(pcb_path: str | Path, backup: bool = True) -> dict:
    """Re-fill all copper zones (ZONE_FILLER) and save. Run after any routing change before DRC/exports. Saves in place (timestamped .bak first by default)."""
    pcb = _require_pcb(pcb_path)
    bak = _backup(pcb) if backup else None
    result = _run_on_pcb(_REFILL_SCRIPT, pcb, {}, timeout=_DEFAULT_TIMEOUT, write_back=True)
    result["backup"] = bak
    return result


def find_isolated_islands(pcb_path: str | Path, net: str = "GND") -> dict:
    """Analyze zone fill connectivity of a net: groups fill islands together with same-net pads/vias/tracks they touch and reports groups isolated from the main plane. Read-only. Zone fills must be current (run refill_zones first if stale)."""
    pcb = _require_pcb(pcb_path)
    return _run_on_pcb(_ISLANDS_SCRIPT, pcb, {"net": net}, timeout=600.0, write_back=False)


def find_duplicate_tracks(pcb_path: str | Path, net: str | None = None,
                          max_gap_mm: float = 1.0, min_overlap_mm: float = 2.0) -> dict:
    """Detect duplicate and redundant track geometry: exact duplicate segments,
    duplicate vias, collinear overlapping segments, parallel same-net pairs
    (gap <= max_gap_mm and projected overlap >= min_overlap_mm), zero-length
    segments and dangling track ends (KiCad connectivity). Read-only; run
    refill_zones first so zone fills are current."""
    pcb = _require_pcb(pcb_path)
    if max_gap_mm <= 0:
        raise ValueError("max_gap_mm must be > 0")
    if min_overlap_mm < 0:
        raise ValueError("min_overlap_mm must be >= 0")
    return _run_on_pcb(_DUPLICATE_TRACKS_SCRIPT, pcb, {
        "net": net, "max_gap_mm": float(max_gap_mm), "min_overlap_mm": float(min_overlap_mm),
    }, timeout=600.0, write_back=False)


def delete_tracks(pcb_path: str | Path, uuids: list[str], backup: bool = True) -> dict:
    """Delete track and via items by UUID. Saves in place (timestamped .bak first by default)."""
    pcb = _require_pcb(pcb_path)
    if not isinstance(uuids, list) or not uuids:
        raise ValueError("uuids must be a non-empty list of UUID strings")
    for u in uuids:
        if not isinstance(u, str) or not u.strip():
            raise ValueError(f"bad uuid: {u!r}")
    bak = _backup(pcb) if backup else None
    result = _run_on_pcb(_DELETE_TRACKS_SCRIPT, pcb, {"uuids": [u.strip() for u in uuids]},
                         timeout=_DEFAULT_TIMEOUT, write_back=True)
    result["backup"] = bak
    return result


def trim_track(pcb_path: str | Path, uuid: str, from_mm: list[float], to_mm: list[float],
               backup: bool = True) -> dict:
    """Move the track endpoint nearest to from_mm to to_mm (mm), keeping the other end. Saves in place (timestamped .bak first by default)."""
    pcb = _require_pcb(pcb_path)
    if not (isinstance(from_mm, (list, tuple)) and len(from_mm) == 2):
        raise ValueError("from_mm must be [x, y] in mm")
    if not (isinstance(to_mm, (list, tuple)) and len(to_mm) == 2):
        raise ValueError("to_mm must be [x, y] in mm")
    bak = _backup(pcb) if backup else None
    result = _run_on_pcb(_TRIM_TRACK_SCRIPT, pcb, {
        "uuid": uuid, "from_mm": [float(from_mm[0]), float(from_mm[1])],
        "to_mm": [float(to_mm[0]), float(to_mm[1])],
    }, timeout=_DEFAULT_TIMEOUT, write_back=True)
    result["backup"] = bak
    return result


def prune_dangling(pcb_path: str | Path, backup: bool = True, protect_pads: bool = True,
                   max_iterations: int = 200) -> dict:
    """Remove track segments whose endpoint is dangling (KiCad connectivity), one at a time, until none remain. With protect_pads (default) tracks connected to a pad are skipped so a pad's only connection is never removed. Saves in place (timestamped .bak first by default)."""
    pcb = _require_pcb(pcb_path)
    if max_iterations < 1:
        raise ValueError("max_iterations must be >= 1")
    bak = _backup(pcb) if backup else None
    removed = []
    iterations = 0
    while iterations < max_iterations:
        iterations += 1
        res = _run_on_pcb(_PRUNE_ONE_SCRIPT, pcb, {"protect_pads": bool(protect_pads)},
                          timeout=_DEFAULT_TIMEOUT, write_back=True)
        if not res.get("ok"):
            return {
                "ok": False, "result": res.get("result") or res,
                "backup": bak, "iterations": iterations, "removed": removed,
            }
        uid = (res.get("result") or {}).get("uuid")
        if not uid:
            break
        removed.append(uid)
    return {
        "ok": True,
        "result": {"pruned_count": len(removed), "removed": removed,
                   "iterations": iterations, "protect_pads": bool(protect_pads)},
        "backup": bak,
    }


def edit_begin(pcb_path: str | Path) -> dict:
    """Copy a PCB to a fast local staging file for editing (useful for slow/network paths). Edit the staging copy, then commit it with edit_commit."""
    pcb = _require_pcb(pcb_path)
    stage_dir = Path(tempfile.gettempdir()) / "kicad-mcp-edit"
    stage_dir.mkdir(parents=True, exist_ok=True)
    staging = stage_dir / f"{pcb.stem}.{uuid.uuid4().hex[:8]}{pcb.suffix}"
    shutil.copy2(pcb, staging)
    sidecar = staging.with_suffix(staging.suffix + ".src.json")
    sidecar.write_text(json.dumps({"source": str(pcb)}), encoding="utf-8")
    return {"source": str(pcb), "staging": str(staging)}


def edit_commit(staging_path: str | Path, target_path: str | Path | None = None) -> dict:
    """Copy an edited staging PCB back over its source file (recorded by edit_begin) or an explicit target_path. Overwrites the target; removes the staging copy."""
    staging = Path(staging_path).expanduser().resolve()
    if not staging.is_file():
        raise FileNotFoundError(f"staging pcb not found: {staging}")
    if target_path is None:
        sidecar = staging.with_suffix(staging.suffix + ".src.json")
        if not sidecar.is_file():
            raise ValueError("target_path not given and no edit_begin sidecar found; pass target_path")
        target = Path(json.loads(sidecar.read_text(encoding="utf-8"))["source"])
    else:
        target = Path(target_path).expanduser().resolve()
    if not target.is_file():
        raise FileNotFoundError(f"target pcb not found: {target}")
    shutil.copy2(staging, target)
    try:
        staging.unlink()
        staging.with_suffix(staging.suffix + ".src.json").unlink()
    except OSError:
        pass
    return {"committed": True, "target": str(target), "staging_removed": True}
