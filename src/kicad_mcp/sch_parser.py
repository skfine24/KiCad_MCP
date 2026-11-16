from pathlib import Path

from . import sexpr


def _flag(node: list, key: str, default: bool = False) -> bool:
    child = sexpr.get_first(node, key)
    if not child:
        return default
    if len(child) == 1:
        return True
    return str(child[1]).lower() in ("yes", "true", "1")


def _symbol_data(node: list) -> dict | None:
    lib_id = sexpr.get_value(node, "lib_id")
    if lib_id is None:
        return None
    data: dict = {"lib_id": lib_id}
    at = sexpr.get_first(node, "at")
    if at:
        data["position_mm"] = [str(v) for v in at[1:4]]
    for prop_name, key in (("Reference", "reference"), ("Value", "value"), ("Footprint", "footprint"), ("Datasheet", "datasheet")):
        prop = _get_property(node, prop_name)
        data[key] = prop
    uuid = sexpr.get_value(node, "uuid")
    data["uuid"] = uuid
    data["dnp"] = _flag(node, "dnp")
    data["in_bom"] = _flag(node, "in_bom", default=True)
    pins = []
    for pin in sexpr.get_all(node, "pin"):
        if len(pin) > 1:
            pins.append(str(pin[1]))
    data["pins"] = pins
    return data


def _get_property(symbol_node: list, name: str) -> str | None:
    for prop in sexpr.get_all(symbol_node, "property"):
        if len(prop) > 2 and str(prop[1]) == name:
            return str(prop[2])
    return None


def _wire_data(node: list) -> dict | None:
    pts = sexpr.get_first(node, "pts")
    if not pts:
        return None
    points = []
    for xy in sexpr.get_all(pts, "xy"):
        points.append([str(v) for v in xy[1:3]])
    return {"points": points}


def _label_data(node: list, kind: str) -> dict:
    text = str(node[1]) if len(node) > 1 and not isinstance(node[1], list) else ""
    at = sexpr.get_first(node, "at")
    pos = [str(v) for v in at[1:3]] if at else None
    return {"text": text, "kind": kind, "position_mm": pos}


def _sheet_data(node: list) -> dict:
    name = _get_property(node, "Sheetname")
    filename = _get_property(node, "Sheetfile")
    uuid = sexpr.get_value(node, "uuid")
    return {"name": name, "file": filename, "uuid": uuid}


def read_schematic(path: str | Path) -> dict:
    sch_path = Path(path).expanduser().resolve()
    if not sch_path.is_file():
        raise FileNotFoundError(f"schematic not found: {sch_path}")
    if sch_path.suffix.lower() != ".kicad_sch":
        raise ValueError(f"expected .kicad_sch file, got: {sch_path.name}")
    root = sexpr.load_file(sch_path)
    if not root or root[0] != "kicad_sch":
        raise ValueError(f"not a kicad_sch file: {sch_path.name}")

    symbols = []
    for node in sexpr.get_all(root, "symbol"):
        data = _symbol_data(node)
        if data:
            symbols.append(data)

    wires = []
    for node in sexpr.get_all(root, "wire"):
        data = _wire_data(node)
        if data:
            wires.append(data)

    labels = []
    labels.extend(_label_data(n, "local") for n in sexpr.get_all(root, "label"))
    labels.extend(_label_data(n, "global") for n in sexpr.get_all(root, "global_label"))
    labels.extend(_label_data(n, "hierarchical") for n in sexpr.get_all(root, "hierarchical_label"))

    sheets = [_sheet_data(n) for n in sexpr.get_all(root, "sheet")]

    junctions = sexpr.get_all(root, "junction")
    buses = sexpr.get_all(root, "bus")

    title = ""
    title_block = sexpr.get_first(root, "title_block")
    if title_block:
        title = sexpr.get_value(title_block, "title") or ""

    return {
        "file": str(sch_path),
        "title": title,
        "stats": {
            "components": len(symbols),
            "wires": len(wires),
            "labels": len(labels),
            "sheets": len(sheets),
            "junctions": len(junctions),
            "buses": len(buses),
        },
        "components": symbols,
        "labels": labels,
        "sheets": sheets,
    }


def list_components(path: str | Path, filter_text: str | None = None) -> list[dict]:
    data = read_schematic(path)
    comps = data["components"]
    if filter_text:
        needle = filter_text.lower()
        comps = [
            c
            for c in comps
            if needle in (c.get("reference") or "").lower()
            or needle in (c.get("value") or "").lower()
            or needle in (c.get("lib_id") or "").lower()
            or needle in (c.get("footprint") or "").lower()
        ]
    return comps


def read_netlist(path: str | Path) -> dict:
    net_path = Path(path).expanduser().resolve()
    if not net_path.is_file():
        raise FileNotFoundError(f"netlist not found: {net_path}")
    root = sexpr.load_file(net_path)
    if not root or root[0] != "export":
        raise ValueError(f"not a KiCad netlist file: {net_path.name}")

    components_section = sexpr.get_first(root, "components") or root
    components = []
    for comp in sexpr.get_all(components_section, "comp"):
        ref = sexpr.get_value(comp, "ref")
        value = sexpr.get_value(comp, "value")
        footprint = sexpr.get_value(comp, "footprint")
        fields = {}
        for field in sexpr.get_all(comp, "field"):
            if len(field) > 2:
                fields[str(field[1])] = str(field[2])
        components.append({"ref": ref, "value": value, "footprint": footprint, "fields": fields})

    nets_section = sexpr.get_first(root, "nets") or root
    nets = []
    for net in sexpr.get_all(nets_section, "net"):
        name = sexpr.get_value(net, "name")
        nodes = []
        for node in sexpr.get_all(net, "node"):
            ref = sexpr.get_value(node, "ref")
            pin = sexpr.get_value(node, "pin")
            if ref is not None:
                nodes.append({"ref": ref, "pin": pin})
        nets.append({"name": name, "nodes": nodes})

    return {"file": str(net_path), "components": components, "nets": nets}
