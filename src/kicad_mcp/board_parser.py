from pathlib import Path

from . import sexpr


def read_board(path: str | Path) -> dict:
    pcb_path = Path(path).expanduser().resolve()
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

    nets = []
    for net in sexpr.get_all(root, "net"):
        if len(net) > 2:
            nets.append(str(net[2]))

    footprints = []
    for fp in sexpr.get_all(root, "footprint"):
        lib_id = str(fp[1]) if len(fp) > 1 and not isinstance(fp[1], list) else None
        at = sexpr.get_first(fp, "at")
        position = [str(v) for v in at[1:3]] if at else None
        layer_node = sexpr.get_value(fp, "layer")
        reference = None
        value = None
        for prop in sexpr.get_all(fp, "property"):
            if len(prop) > 2 and str(prop[1]) == "Reference":
                reference = str(prop[2])
            if len(prop) > 2 and str(prop[1]) == "Value":
                value = str(prop[2])
        for prop in sexpr.get_all(fp, "fp_text"):
            if len(prop) > 2 and str(prop[1]) == "reference":
                reference = str(prop[2])
            if len(prop) > 2 and str(prop[1]) == "value":
                value = str(prop[2])
        footprints.append(
            {
                "lib_id": lib_id,
                "reference": reference,
                "value": value,
                "layer": layer_node,
                "position_mm": position,
            }
        )

    tracks = sexpr.get_all(root, "segment")
    vias = sexpr.get_all(root, "via")
    zones = sexpr.get_all(root, "zone")
    pads = []
    for fp in sexpr.get_all(root, "footprint"):
        pads.extend(sexpr.get_all(fp, "pad"))

    edge_points = []
    for layer_node in sexpr.get_all(root, "gr_line"):
        layer_name = sexpr.get_value(layer_node, "layer")
        if layer_name == "Edge.Cuts":
            start = sexpr.get_first(layer_node, "start")
            end = sexpr.get_first(layer_node, "end")
            if start and len(start) > 2:
                edge_points.append((float(start[1]), float(start[2])))
            if end and len(end) > 2:
                edge_points.append((float(end[1]), float(end[2])))
    for rect in sexpr.get_all(root, "gr_rect"):
        layer_name = sexpr.get_value(rect, "layer")
        if layer_name == "Edge.Cuts":
            for key in ("start", "end"):
                pt = sexpr.get_first(rect, key)
                if pt and len(pt) > 2:
                    edge_points.append((float(pt[1]), float(pt[2])))

    size = None
    if edge_points:
        xs = [p[0] for p in edge_points]
        ys = [p[1] for p in edge_points]
        size = {
            "width_mm": round(max(xs) - min(xs), 2),
            "height_mm": round(max(ys) - min(ys), 2),
        }

    return {
        "file": str(pcb_path),
        "layers": layers,
        "copper_layers": len([l for l in layers if l.endswith(".Cu")]),
        "nets": nets,
        "net_count": len(nets),
        "stats": {
            "footprints": len(footprints),
            "tracks": len(tracks),
            "vias": len(vias),
            "zones": len(zones),
            "pads": len(pads),
        },
        "footprints": footprints,
        "board_size": size,
    }
