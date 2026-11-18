"""Schematic editing through KiCad's text format.

The editor preserves the parsed tree order and quotes only the tokens that
KiCad writes as strings.  Every write is backed up, re-parsed and validated
before it replaces the original file.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from . import library, paths, sexpr

_KIND_TAGS = {
    "local": "label",
    "global": "global_label",
    "hierarchical": "hierarchical_label",
}


def _q(value) -> sexpr.Quoted:
    return sexpr.Quoted(str(value))


def _new_uuid() -> str:
    return str(uuid.uuid4())


def _load(path: str | Path) -> tuple[Path, list]:
    sch = paths.normalize_path(path)
    if not sch.is_file():
        raise FileNotFoundError(f"schematic not found: {sch}")
    if sch.suffix.lower() != ".kicad_sch":
        raise ValueError(f"expected .kicad_sch file, got: {sch.name}")
    tree = sexpr.load_file(sch)
    if not tree or tree[0] != "kicad_sch":
        raise ValueError(f"not a kicad_sch file: {sch.name}")
    return sch, tree


def _insert_node(tree: list, node: list) -> None:
    for key in ("sheet_instances", "embedded_fonts"):
        anchor = sexpr.get_first(tree, key)
        if anchor is not None:
            tree.insert(tree.index(anchor), node)
            return
    tree.append(node)


def _project_context(tree: list) -> tuple[str, str]:
    for symbol in sexpr.get_all(tree, "symbol"):
        instances = sexpr.get_first(symbol, "instances")
        if not instances:
            continue
        project = sexpr.get_first(instances, "project")
        if not project or len(project) < 2:
            continue
        path_node = sexpr.get_first(project, "path")
        if not path_node or len(path_node) < 2:
            continue
        return str(project[1]), str(path_node[1])
    root_uuid = sexpr.get_value(tree, "uuid")
    if root_uuid:
        return "kicad_project", f"/{root_uuid}"
    return "kicad_project", "/"


def _find_symbol(tree: list, ref: str) -> list:
    for symbol in sexpr.get_all(tree, "symbol"):
        prop = _get_property(symbol, "Reference")
        if prop is not None and str(prop[2]) == ref:
            return symbol
    raise ValueError(f"symbol not found: {ref}")


def _get_property(symbol: list, name: str) -> list | None:
    for prop in sexpr.get_all(symbol, "property"):
        if len(prop) > 2 and str(prop[1]) == name:
            return prop
    return None


def _set_property(symbol: list, name: str, value: str) -> list:
    prop = _get_property(symbol, name)
    if prop is not None:
        if len(prop) > 2:
            prop[2] = _q(value)
        else:
            prop.append(_q(value))
        return prop
    at = sexpr.get_first(symbol, "at")
    x = str(at[1]) if at and len(at) > 1 else "0"
    y = str(at[2]) if at and len(at) > 2 else "0"
    prop = [
        "property",
        _q(name),
        _q(value),
        ["at", x, y, "0"],
        ["effects", ["font", ["size", "1.27", "1.27"]], ["hide", "yes"]],
    ]
    symbol.append(prop)
    return prop


def _save(
    sch: Path,
    tree: list,
    backup: bool,
    dry_run: bool,
    expected_revision: str | None,
) -> dict:
    if expected_revision and not paths.revision_matches(sch, expected_revision):
        raise RuntimeError(
            "revision mismatch: schematic changed since it was read "
            f"(expected {expected_revision})"
        )
    text = sexpr.dump(tree) + "\n"
    # Validate before touching the file.
    parsed = sexpr.parse(text)
    if not parsed or parsed[0][0] != "kicad_sch":
        raise RuntimeError("serialized schematic failed validation")
    if dry_run:
        return {
            "dry_run": True,
            "would_write": True,
            "bytes": len(text.encode("utf-8")),
            "revision": paths.file_revision(sch),
        }
    backup_path = paths.backup_file(sch) if backup else None
    paths.atomic_write_text(sch, text)
    revision = paths.file_revision(sch)
    return {
        "dry_run": False,
        "would_write": True,
        "backup": backup_path,
        "revision": revision,
    }


def sch_revision(sch_path: str | Path) -> dict:
    sch, _ = _load(sch_path)
    return paths.file_revision(sch)


def sch_add_wire(
    sch_path: str | Path,
    points: list[list[float]],
    backup: bool = True,
    dry_run: bool = False,
    expected_revision: str | None = None,
) -> dict:
    sch, tree = _load(sch_path)
    if not isinstance(points, list) or len(points) < 2:
        raise ValueError("points must contain at least 2 [x, y] pairs")
    uuids = []
    for index in range(len(points) - 1):
        p0, p1 = points[index], points[index + 1]
        if len(p0) != 2 or len(p1) != 2:
            raise ValueError(f"bad point: {p0!r} / {p1!r}")
        token = _new_uuid()
        node = [
            "wire",
            ["pts", ["xy", str(float(p0[0])), str(float(p0[1]))], ["xy", str(float(p1[0])), str(float(p1[1]))]],
            ["stroke", ["width", "0"], ["type", "solid"]],
            ["uuid", _q(token)],
        ]
        _insert_node(tree, node)
        uuids.append(token)
    result = _save(sch, tree, backup, dry_run, expected_revision)
    result.update({"segments": len(uuids), "uuids": uuids})
    return result


def sch_add_label(
    sch_path: str | Path,
    text: str,
    x: float,
    y: float,
    kind: str = "local",
    rotation: float = 0,
    shape: str = "input",
    backup: bool = True,
    dry_run: bool = False,
    expected_revision: str | None = None,
) -> dict:
    if kind not in _KIND_TAGS:
        raise ValueError("kind must be local, global or hierarchical")
    sch, tree = _load(sch_path)
    token = _new_uuid()
    tag = _KIND_TAGS[kind]
    node = [tag, _q(text)]
    if kind in ("global", "hierarchical"):
        node.append(["shape", shape])
    node.extend(
        [
            ["at", str(float(x)), str(float(y)), str(float(rotation))],
            [
                "effects",
                ["font", ["size", "1.27", "1.27"]],
                ["justify", "left", "bottom"],
            ],
            ["uuid", _q(token)],
        ]
    )
    _insert_node(tree, node)
    result = _save(sch, tree, backup, dry_run, expected_revision)
    result.update({"kind": kind, "text": text, "uuid": token})
    return result


def sch_add_no_connect(
    sch_path: str | Path,
    x: float,
    y: float,
    backup: bool = True,
    dry_run: bool = False,
    expected_revision: str | None = None,
) -> dict:
    sch, tree = _load(sch_path)
    token = _new_uuid()
    node = ["no_connect", ["at", str(float(x)), str(float(y))], ["uuid", _q(token)]]
    _insert_node(tree, node)
    result = _save(sch, tree, backup, dry_run, expected_revision)
    result.update({"at_mm": [float(x), float(y)], "uuid": token})
    return result


def sch_set_value(
    sch_path: str | Path,
    ref: str,
    value: str,
    backup: bool = True,
    dry_run: bool = False,
    expected_revision: str | None = None,
) -> dict:
    sch, tree = _load(sch_path)
    symbol = _find_symbol(tree, ref)
    _set_property(symbol, "Value", value)
    result = _save(sch, tree, backup, dry_run, expected_revision)
    result.update({"ref": ref, "value": value})
    return result


def sch_set_property(
    sch_path: str | Path,
    ref: str,
    name: str,
    value: str,
    backup: bool = True,
    dry_run: bool = False,
    expected_revision: str | None = None,
) -> dict:
    sch, tree = _load(sch_path)
    symbol = _find_symbol(tree, ref)
    _set_property(symbol, name, value)
    result = _save(sch, tree, backup, dry_run, expected_revision)
    result.update({"ref": ref, "property": name, "value": value})
    return result


def sch_set_properties(
    sch_path: str | Path,
    updates: list[dict],
    backup: bool = True,
    dry_run: bool = False,
    expected_revision: str | None = None,
) -> dict:
    """Apply property updates to several symbols in one save.

    Each update is ``{"ref": "R1", "properties": {"LCSC": "C25804", ...}}``.
    The schematic is loaded once, every symbol is updated in the parsed tree,
    and the file is written once (with a backup by default).
    """
    if not isinstance(updates, list) or not updates:
        raise ValueError("updates must be a non-empty list")
    sch, tree = _load(sch_path)
    applied: list[dict] = []
    for update in updates:
        ref = str(update.get("ref") or "").strip()
        if not ref:
            raise ValueError("each update needs a 'ref'")
        properties = update.get("properties") or {}
        if not isinstance(properties, dict) or not properties:
            raise ValueError(f"update for {ref} needs a non-empty 'properties' dict")
        symbol = _find_symbol(tree, ref)
        for name, value in properties.items():
            _set_property(symbol, str(name), str(value))
        applied.append({"ref": ref, "properties": sorted(str(name) for name in properties)})
    result = _save(sch, tree, backup, dry_run, expected_revision)
    result.update({"updated": len(applied), "updates": applied})
    return result


def sch_add_symbol(
    sch_path: str | Path,
    lib_id: str,
    ref: str,
    value: str,
    x: float,
    y: float,
    rotation: float = 0,
    unit: int = 1,
    mirror: str | None = None,
    footprint: str | None = None,
    dnp: bool = False,
    backup: bool = True,
    dry_run: bool = False,
    expected_revision: str | None = None,
) -> dict:
    sch, tree = _load(sch_path)
    if ":" not in lib_id:
        raise ValueError("lib_id must be 'Library:Symbol'")
    lib_symbols = sexpr.get_first(tree, "lib_symbols")
    if lib_symbols is None:
        lib_symbols = ["lib_symbols"]
        _insert_node(tree, lib_symbols)

    lib_symbol_added = False
    existing = None
    for node in sexpr.get_all(lib_symbols, "symbol"):
        if len(node) > 1 and str(node[1]) == lib_id:
            existing = node
            break
    if existing is None:
        data = library.get_symbol_node(lib_id)
        lib_symbols.append(data["node"])
        lib_symbol_added = True

    pinout = library.get_pinout(lib_id)
    project_name, root_path = _project_context(tree)
    token = _new_uuid()
    instance = [
        "symbol",
        ["lib_id", _q(lib_id)],
        ["at", str(float(x)), str(float(y)), str(float(rotation))],
    ]
    if mirror in ("x", "y"):
        instance.append(["mirror", mirror])
    instance.extend(
        [
            ["unit", str(int(unit))],
            ["exclude_from_sim", "no"],
            ["in_bom", "yes"],
            ["on_board", "yes"],
            ["dnp", "yes" if dnp else "no"],
            ["uuid", _q(token)],
            [
                "property",
                _q("Reference"),
                _q(ref),
                ["at", str(float(x)), str(float(y) - 2.54), "0"],
                ["effects", ["font", ["size", "1.27", "1.27"]]],
            ],
            [
                "property",
                _q("Value"),
                _q(value),
                ["at", str(float(x)), str(float(y) + 2.54), "0"],
                ["effects", ["font", ["size", "1.27", "1.27"]]],
            ],
            [
                "property",
                _q("Footprint"),
                _q(footprint or ""),
                ["at", str(float(x)), str(float(y)), "0"],
                ["effects", ["font", ["size", "1.27", "1.27"]], ["hide", "yes"]],
            ],
            [
                "property",
                _q("Datasheet"),
                _q(""),
                ["at", str(float(x)), str(float(y)), "0"],
                ["effects", ["font", ["size", "1.27", "1.27"]], ["hide", "yes"]],
            ],
            [
                "property",
                _q("Description"),
                _q(""),
                ["at", str(float(x)), str(float(y)), "0"],
                ["effects", ["font", ["size", "1.27", "1.27"]], ["hide", "yes"]],
            ],
        ]
    )
    for pin in pinout["pins"]:
        number = pin.get("number")
        if number is None:
            continue
        instance.append(["pin", _q(str(number)), ["uuid", _q(_new_uuid())]])
    instance.append(
        [
            "instances",
            [
                "project",
                _q(project_name),
                [
                    "path",
                    _q(root_path),
                    ["reference", _q(ref)],
                    ["unit", str(int(unit))],
                ],
            ],
        ]
    )
    _insert_node(tree, instance)
    result = _save(sch, tree, backup, dry_run, expected_revision)
    result.update(
        {
            "uuid": token,
            "lib_id": lib_id,
            "ref": ref,
            "value": value,
            "unit": int(unit),
            "lib_symbol_added": lib_symbol_added,
            "pin_count": len(pinout["pins"]),
        }
    )
    return result


def _find_parent(root, target):
    if not isinstance(root, list):
        return None
    for child in root:
        if child is target:
            return root
        found = _find_parent(child, target)
        if found is not None:
            return found
    return None


def sch_delete_item(
    sch_path: str | Path,
    item_uuid: str,
    backup: bool = True,
    dry_run: bool = False,
    expected_revision: str | None = None,
) -> dict:
    sch, tree = _load(sch_path)
    removed = 0
    for node in list(sexpr.walk(tree)):
        if node and node[0] in ("symbol", "wire", "label", "global_label", "hierarchical_label", "no_connect"):
            uuid_node = sexpr.get_first(node, "uuid")
            if uuid_node and len(uuid_node) > 1 and str(uuid_node[1]) == item_uuid:
                parent = _find_parent(tree, node)
                if parent is not None:
                    sexpr.remove_child(parent, node)
                    removed += 1
                    break
    if not removed:
        raise ValueError(f"item not found: {item_uuid}")
    result = _save(sch, tree, backup, dry_run, expected_revision)
    result.update({"removed": removed, "uuid": item_uuid})
    return result
