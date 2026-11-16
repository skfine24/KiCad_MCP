from pathlib import Path

from . import paths, sexpr


def _net_name(node: list) -> str | None:
    """KiCad 7-9 wrote ``(net 1 "GND")``; KiCad 10 writes ``(net "GND")``."""
    if len(node) == 2:
        name = str(node[1])
    elif len(node) > 2:
        name = str(node[2])
    else:
        return None
    return name or None


def _collect_nets(root: list) -> list[str]:
    names = set()
    for node in sexpr.walk(root):
        if node and node[0] == "net":
            name = _net_name(node)
            if name:
                names.add(name)
    return sorted(names)


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
            ex, ey = float(end[1]), float(end[2])
            radius = ((ex - cx) ** 2 + (ey - cy) ** 2) ** 0.5
            points.extend(
                [(cx - radius, cy - radius), (cx + radius, cy + radius)]
            )
    for kind in ("gr_poly", "gr_curve"):
        for node in sexpr.get_all(root, kind):
            if sexpr.get_value(node, "layer") != "Edge.Cuts":
                continue
            pts = sexpr.get_first(node, "pts")
            if pts:
                for xy in sexpr.get_all(pts, "xy"):
                    add_xy(xy)
    return points


def _stackup(root: list) -> list[dict] | None:
    setup = sexpr.get_first(root, "setup")
    if not setup:
        return None
    stackup = sexpr.get_first(setup, "stackup")
    if not stackup:
        return None
    rows = []
    for layer in sexpr.get_all(stackup, "layer"):
        if len(layer) < 2:
            continue
        thickness = sexpr.get_value(layer, "thickness")
        rows.append(
            {
                "name": str(layer[1]),
                "type": sexpr.get_value(layer, "type"),
                "thickness_mm": float(thickness) if thickness is not None else None,
                "material": sexpr.get_value(layer, "material"),
                "epsilon_r": sexpr.get_value(layer, "epsilon_r"),
                "loss_tangent": sexpr.get_value(layer, "loss_tangent"),
                "color": sexpr.get_value(layer, "color"),
            }
        )
    return rows


def _netclasses(pcb_path: Path) -> list[dict]:
    pro_path = pcb_path.with_suffix(".kicad_pro")
    if not pro_path.is_file():
        return []
    try:
        import json

        data = json.loads(pro_path.read_text(encoding="utf-8"))
    except Exception:
        return []
    classes = data.get("net_settings", {}).get("classes", []) or []
    rows = []
    for cls in classes:
        rows.append(
            {
                "name": cls.get("name"),
                "clearance_mm": cls.get("clearance"),
                "track_width_mm": cls.get("track_width"),
                "via_diameter_mm": cls.get("via_diameter"),
                "via_drill_mm": cls.get("via_drill"),
                "priority": cls.get("priority"),
                "microvia_diameter_mm": cls.get("microvia_diameter"),
                "microvia_drill_mm": cls.get("microvia_drill"),
                "diff_pair_width_mm": cls.get("diff_pair_width"),
                "diff_pair_gap_mm": cls.get("diff_pair_gap"),
            }
        )
    return rows


def _schematic_nets(pcb_path: Path, refresh: bool) -> tuple[list[str], dict]:
    sch_path = pcb_path.with_suffix(".kicad_sch")
    if not sch_path.is_file():
        return [], {"available": False, "reason": "no matching .kicad_sch"}
    from . import kicad_cli, sch_parser

    netlist_path = sch_path.parent / f"{sch_path.stem}-netlist.net"
    exported = False
    if (
        refresh
        or not netlist_path.is_file()
        or netlist_path.stat().st_mtime_ns < sch_path.stat().st_mtime_ns
    ):
        kicad_cli.export_netlist(sch_path)
        exported = True
    try:
        data = sch_parser.read_netlist(netlist_path)
    except Exception as exc:
        return [], {
            "available": False,
            "reason": f"{type(exc).__name__}: {exc}",
            "netlist": str(netlist_path),
        }
    nets = sorted({n["name"] for n in data["nets"] if n.get("name")})
    return nets, {
        "available": True,
        "netlist": str(netlist_path),
        "netlist_mtime_ns": netlist_path.stat().st_mtime_ns,
        "exported": exported,
        "net_count": len(nets),
    }


def read_board(
    path: str | Path,
    include_schematic_nets: bool = False,
    refresh_netlist: bool = False,
) -> dict:
    pcb_path = paths.normalize_path(path)
    if not pcb_path.is_file():
        raise FileNotFoundError(f"pcb not found: {pcb_path}")
    if pcb_path.suffix.lower() != ".kicad_pcb":
        raise ValueError(f"expected .kicad_pcb file, got: {pcb_path.name}")
    root = sexpr.load_file(pcb_path)
    if not root or root[0] != "kicad_pcb":
        raise ValueError(f"not a kicad_pcb file: {pcb_path.name}")

    layers = []
    for layer in sexpr.get_all(root, "layers"):
        for entry in layer[1:]:
            if isinstance(entry, list) and entry:
                layers.append(str(entry[1]))
            elif isinstance(entry, str):
                layers.append(entry)

    nets = _collect_nets(root)

    footprints = []
    unconnected_pads = []
    for fp in sexpr.get_all(root, "footprint"):
        lib_id = str(fp[1]) if len(fp) > 1 and not isinstance(fp[1], list) else None
        at = sexpr.get_first(fp, "at")
        position = [str(v) for v in at[1:3]] if at else None
        layer_node = sexpr.get_value(fp, "layer")
        reference = _footprint_reference(fp)
        value = _footprint_value(fp)
        footprints.append(
            {
                "lib_id": lib_id,
                "reference": reference,
                "value": value,
                "layer": layer_node,
                "position_mm": position,
                "dnp": sexpr.get_value(fp, "dnp"),
                "exclude_from_pos_files": sexpr.get_value(fp, "exclude_from_pos_files"),
            }
        )
        for pad in sexpr.get_all(fp, "pad"):
            number = str(pad[1]) if len(pad) > 1 else None
            net_node = sexpr.get_first(pad, "net")
            net = _net_name(net_node) if net_node else None
            if not net:
                unconnected_pads.append({"ref": reference, "pad": number})

    tracks = sexpr.get_all(root, "segment")
    arcs = sexpr.get_all(root, "arc")
    vias = sexpr.get_all(root, "via")
    zones = sexpr.get_all(root, "zone")
    pads = []
    for fp in sexpr.get_all(root, "footprint"):
        pads.extend(sexpr.get_all(fp, "pad"))

    edge_points = _edge_points(root)
    size = None
    if edge_points:
        xs = [p[0] for p in edge_points]
        ys = [p[1] for p in edge_points]
        size = {
            "width_mm": round(max(xs) - min(xs), 2),
            "height_mm": round(max(ys) - min(ys), 2),
            "min_x_mm": round(min(xs), 2),
            "min_y_mm": round(min(ys), 2),
            "max_x_mm": round(max(xs), 2),
            "max_y_mm": round(max(ys), 2),
        }

    general = sexpr.get_first(root, "general")
    thickness = sexpr.get_value(general, "thickness") if general else None
    try:
        thickness_mm = float(thickness) if thickness is not None else None
    except ValueError:
        thickness_mm = None

    result = {
        "file": str(pcb_path),
        "revision": paths.file_revision(pcb_path),
        "title_block": {
            "title": sexpr.get_value(sexpr.get_first(root, "title_block") or [], "title"),
            "date": sexpr.get_value(sexpr.get_first(root, "title_block") or [], "date"),
            "rev": sexpr.get_value(sexpr.get_first(root, "title_block") or [], "rev"),
            "company": sexpr.get_value(sexpr.get_first(root, "title_block") or [], "company"),
        },
        "board_thickness_mm": thickness_mm,
        "stackup": _stackup(root),
        "netclasses": _netclasses(pcb_path),
        "layers": layers,
        "copper_layers": len([layer for layer in layers if layer.endswith(".Cu")]),
        "nets": nets,
        "net_count": len(nets),
        "unconnected_pads": unconnected_pads,
        "unconnected_pad_count": len(unconnected_pads),
        "stats": {
            "footprints": len(footprints),
            "tracks": len(tracks),
            "arcs": len(arcs),
            "vias": len(vias),
            "zones": len(zones),
            "pads": len(pads),
        },
        "footprints": footprints,
        "board_size": size,
    }
    if include_schematic_nets:
        schematic_nets, meta = _schematic_nets(pcb_path, refresh_netlist)
        result["schematic_nets"] = schematic_nets
        result["schematic_net_count"] = len(schematic_nets)
        result["schematic_netlist"] = meta

        def normalized(name: str) -> str:
            return name[1:] if name.startswith("/") else name

        merged = sorted({normalized(name) for name in nets} | {normalized(name) for name in schematic_nets})
        result["net_count_all"] = len(merged)
    return result
