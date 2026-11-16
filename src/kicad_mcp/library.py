"""KiCad symbol/footprint library discovery and parsing."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path

from . import kicad_cli, paths, sexpr

_CACHE_DIR = Path(tempfile.gettempdir()) / "kicad-mcp"
_SYMBOL_CACHE = _CACHE_DIR / "symbol_index.json"
_FOOTPRINT_CACHE = _CACHE_DIR / "footprint_index.json"


def _program_root() -> Path | None:
    program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    for version in kicad_cli.DEFAULT_KICAD_VERSIONS:
        candidate = Path(program_files) / "KiCad" / version / "share" / "kicad"
        if candidate.is_dir():
            return candidate
    return None


def symbol_dirs() -> list[Path]:
    dirs = []
    for env_name in ("KICAD_SYMBOL_DIR", "KICAD10_SYMBOL_DIR"):
        env = os.environ.get(env_name)
        if env:
            dirs.append(Path(env).expanduser())
    root = _program_root()
    if root is not None:
        dirs.append(root / "symbols")
    return [path for path in dirs if path.is_dir()]


def footprint_dirs() -> list[Path]:
    dirs = []
    for env_name in ("KICAD_FOOTPRINT_DIR", "KICAD10_FOOTPRINT_DIR"):
        env = os.environ.get(env_name)
        if env:
            dirs.append(Path(env).expanduser())
    root = _program_root()
    if root is not None:
        dirs.append(root / "footprints")
    return [path for path in dirs if path.is_dir()]


def _mtime_key(files: list[Path]) -> dict:
    return {str(path): path.stat().st_mtime_ns for path in files}


def _load_cache(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _save_cache(path: Path, payload: dict) -> None:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    paths.atomic_write_text(path, json.dumps(payload, ensure_ascii=False))


def _symbol_files() -> list[Path]:
    files = []
    for directory in symbol_dirs():
        files.extend(sorted(directory.glob("*.kicad_sym")))
    return files


def _footprint_files() -> list[Path]:
    files = []
    for directory in footprint_dirs():
        for pretty in sorted(directory.glob("*.pretty")):
            files.extend(sorted(pretty.glob("*.kicad_mod")))
    return files


def _symbol_properties(node: list) -> dict:
    props = {}
    for prop in sexpr.get_all(node, "property"):
        if len(prop) > 2:
            props[str(prop[1])] = str(prop[2])
    return props


def _pin_count(node: list) -> int:
    count = 0
    for child in sexpr.walk(node):
        if child and child[0] == "pin":
            count += 1
    return count


def _find_symbol_node(root: list, name: str) -> list | None:
    for node in sexpr.get_all(root, "symbol"):
        if len(node) > 1 and str(node[1]) == name:
            return node
    return None


def _suffix(name: str, base: str) -> str | None:
    prefix = base + "_"
    if name.startswith(prefix):
        return name[len(prefix) :]
    return None


def _flatten_symbol(derived: list, parent: list, derived_name: str) -> list:
    """Merge a derived symbol into its parent and rename unit children."""
    clone = sexpr.clone(parent)
    parent_name = str(parent[1])
    clone[1] = sexpr.Quoted(derived_name)

    parent_props = {str(prop[1]): prop for prop in sexpr.get_all(clone, "property")}
    for prop in sexpr.get_all(derived, "property"):
        name = str(prop[1])
        if name in parent_props:
            sexpr.replace_child(clone, parent_props[name], sexpr.clone(prop))
        else:
            clone.append(sexpr.clone(prop))

    for key in (
        "pin_numbers",
        "pin_names",
        "exclude_from_sim",
        "in_bom",
        "on_board",
        "in_pos_files",
        "duplicate_pin_numbers_are_jumpers",
        "power",
    ):
        child = sexpr.get_first(derived, key)
        if child is not None:
            existing = sexpr.get_first(clone, key)
            if existing is not None:
                sexpr.replace_child(clone, existing, sexpr.clone(child))
            else:
                clone.append(sexpr.clone(child))

    derived_children = {}
    for child in sexpr.get_all(derived, "symbol"):
        suffix = _suffix(str(child[1]), derived_name)
        if suffix is not None:
            derived_children[suffix] = child

    for child in sexpr.get_all(clone, "symbol"):
        suffix = _suffix(str(child[1]), parent_name)
        if suffix is None:
            continue
        if suffix in derived_children:
            replacement = sexpr.clone(derived_children[suffix])
            replacement[1] = sexpr.Quoted(derived_name + "_" + suffix)
            for position, item in enumerate(clone):
                if item is child:
                    clone[position] = replacement
                    break
        else:
            child[1] = sexpr.Quoted(derived_name + "_" + suffix)

    # Remove a stale extends child if the parent somehow carried one.
    extends = sexpr.get_first(clone, "extends")
    if extends is not None:
        sexpr.remove_child(clone, extends)
    return clone


def _resolve_symbol_node(root: list, name: str, seen: set[str] | None = None) -> list:
    seen = seen or set()
    if name in seen:
        raise ValueError(f"symbol inheritance cycle: {name}")
    seen.add(name)
    node = _find_symbol_node(root, name)
    if node is None:
        raise KeyError(f"symbol not found in library: {name}")
    extends = sexpr.get_value(node, "extends")
    if extends:
        parent = _resolve_symbol_node(root, extends, seen)
        return _flatten_symbol(node, parent, name)
    return sexpr.clone(node)


def _build_symbol_index() -> dict:
    rows = []
    for path in _symbol_files():
        try:
            root = sexpr.load_file(path)
        except Exception:
            continue
        if not root or root[0] != "kicad_symbol_lib":
            continue
        library = path.stem
        for node in sexpr.get_all(root, "symbol"):
            if len(node) < 2:
                continue
            name = str(node[1])
            props = _symbol_properties(node)
            rows.append(
                {
                    "lib_id": f"{library}:{name}",
                    "library": library,
                    "name": name,
                    "reference": props.get("Reference"),
                    "description": props.get("Description"),
                    "keywords": props.get("ki_keywords"),
                    "footprint_filters": props.get("ki_fp_filters"),
                    "extends": sexpr.get_value(node, "extends"),
                    "pin_count": _pin_count(node),
                    "path": str(path),
                }
            )
    return {
        "built": datetime.now().isoformat(timespec="seconds"),
        "files": _mtime_key(_symbol_files()),
        "symbols": rows,
    }


def symbol_index(refresh: bool = False) -> dict:
    files = _symbol_files()
    cached = None if refresh else _load_cache(_SYMBOL_CACHE)
    if cached and cached.get("files") == _mtime_key(files):
        return cached
    payload = _build_symbol_index()
    _save_cache(_SYMBOL_CACHE, payload)
    return payload


def search_symbols(
    query: str,
    library: str | None = None,
    limit: int = 50,
    refresh: bool = False,
) -> dict:
    needle = (query or "").lower()
    rows = symbol_index(refresh=refresh)["symbols"]
    matches = []
    for row in rows:
        if library and row["library"].lower() != library.lower():
            continue
        haystack = " ".join(
            str(row.get(key) or "")
            for key in ("lib_id", "name", "reference", "description", "keywords")
        ).lower()
        if needle and needle not in haystack:
            continue
        matches.append(row)
        if len(matches) >= max(1, limit):
            break
    return {"query": query, "library": library, "count": len(matches), "symbols": matches}


def get_symbol_node(lib_id: str) -> dict:
    if ":" not in lib_id:
        raise ValueError("lib_id must be 'Library:Symbol'")
    library, name = lib_id.split(":", 1)
    path = None
    for directory in symbol_dirs():
        candidate = directory / f"{library}.kicad_sym"
        if candidate.is_file():
            path = candidate
            break
    if path is None:
        raise FileNotFoundError(f"symbol library not found: {library}")
    root = sexpr.load_file(path)
    node = _resolve_symbol_node(root, name)
    node[1] = sexpr.Quoted(lib_id)
    return {
        "lib_id": lib_id,
        "library": library,
        "name": name,
        "path": str(path),
        "node": node,
        "properties": _symbol_properties(node),
        "pin_count": _pin_count(node),
    }


def get_pinout(lib_id: str) -> dict:
    data = get_symbol_node(lib_id)
    node = data["node"]
    pins = []
    for child in sexpr.get_all(node, "symbol"):
        child_name = str(child[1])
        suffix = _suffix(child_name, data["name"])
        unit = None
        style = None
        if suffix:
            parts = suffix.split("_")
            if len(parts) >= 2:
                try:
                    unit = int(parts[0])
                    style = int(parts[1])
                except ValueError:
                    pass
        for pin in sexpr.get_all(child, "pin"):
            if len(pin) < 3:
                continue
            at = sexpr.get_first(pin, "at")
            length = sexpr.get_value(pin, "length")
            name_node = sexpr.get_first(pin, "name")
            number_node = sexpr.get_first(pin, "number")
            pins.append(
                {
                    "number": str(number_node[1]) if number_node and len(number_node) > 1 else None,
                    "name": str(name_node[1]) if name_node and len(name_node) > 1 else None,
                    "type": str(pin[1]),
                    "shape": str(pin[2]),
                    "unit": unit,
                    "style": style,
                    "position_mm": [str(v) for v in at[1:4]] if at else None,
                    "length_mm": length,
                }
            )
    return {
        "lib_id": lib_id,
        "path": data["path"],
        "pin_count": len(pins),
        "pins": pins,
        "properties": data["properties"],
    }


def _build_footprint_index() -> dict:
    rows = []
    for path in _footprint_files():
        try:
            root = sexpr.load_file(path)
        except Exception:
            continue
        if not root or root[0] != "footprint":
            continue
        library = path.parent.stem
        name = str(root[1]) if len(root) > 1 else path.stem
        pads = sexpr.get_all(root, "pad")
        models = [str(node[1]) for node in sexpr.get_all(root, "model") if len(node) > 1]
        rows.append(
            {
                "lib_id": f"{library}:{name}",
                "library": library,
                "name": name,
                "description": sexpr.get_value(root, "descr"),
                "tags": sexpr.get_value(root, "tags"),
                "pad_count": len(pads),
                "models": models,
                "path": str(path),
            }
        )
    return {
        "built": datetime.now().isoformat(timespec="seconds"),
        "files": _mtime_key(_footprint_files()),
        "footprints": rows,
    }


def footprint_index(refresh: bool = False) -> dict:
    files = _footprint_files()
    cached = None if refresh else _load_cache(_FOOTPRINT_CACHE)
    if cached and cached.get("files") == _mtime_key(files):
        return cached
    payload = _build_footprint_index()
    _save_cache(_FOOTPRINT_CACHE, payload)
    return payload


def search_footprints(
    query: str,
    library: str | None = None,
    limit: int = 50,
    refresh: bool = False,
) -> dict:
    needle = (query or "").lower()
    rows = footprint_index(refresh=refresh)["footprints"]
    matches = []
    for row in rows:
        if library and row["library"].lower() != library.lower():
            continue
        haystack = " ".join(
            str(row.get(key) or "")
            for key in ("lib_id", "name", "description", "tags")
        ).lower()
        if needle and needle not in haystack:
            continue
        matches.append(row)
        if len(matches) >= max(1, limit):
            break
    return {"query": query, "library": library, "count": len(matches), "footprints": matches}


def resolve_footprint(footprint: str) -> dict:
    raw = str(footprint)
    if raw.lower().endswith(".kicad_mod"):
        path = paths.normalize_path(raw)
        if not path.is_file():
            raise FileNotFoundError(f"footprint file not found: {path}")
        return {"lib_path": str(path.parent), "name": path.stem, "lib_id": path.stem}
    if ":" in raw:
        library, name = raw.split(":", 1)
        for directory in footprint_dirs():
            candidate = directory / f"{library}.pretty"
            if candidate.is_dir():
                return {"lib_path": str(candidate), "name": name, "lib_id": raw}
        raise FileNotFoundError(f"footprint library not found: {library}")
    for directory in footprint_dirs():
        for candidate in directory.glob("*.pretty"):
            if (candidate / f"{raw}.kicad_mod").is_file():
                return {
                    "lib_path": str(candidate),
                    "name": raw,
                    "lib_id": f"{candidate.stem}:{raw}",
                }
    raise FileNotFoundError(f"footprint not found: {raw}")


def get_footprint_info(footprint: str) -> dict:
    resolved = resolve_footprint(footprint)
    path = Path(resolved["lib_path"]) / f"{resolved['name']}.kicad_mod"
    root = sexpr.load_file(path)
    pads = []
    for pad in sexpr.get_all(root, "pad"):
        at = sexpr.get_first(pad, "at")
        size = sexpr.get_first(pad, "size")
        layers = sexpr.get_first(pad, "layers")
        pads.append(
            {
                "number": str(pad[1]) if len(pad) > 1 else None,
                "type": str(pad[2]) if len(pad) > 2 else None,
                "shape": str(pad[3]) if len(pad) > 3 else None,
                "position_mm": [str(v) for v in at[1:4]] if at else None,
                "size_mm": [str(v) for v in size[1:3]] if size else None,
                "layers": [str(v) for v in layers[1:]] if layers else [],
            }
        )
    models = [str(node[1]) for node in sexpr.get_all(root, "model") if len(node) > 1]
    courtyard = []
    for kind in ("fp_line", "fp_rect", "fp_arc", "fp_circle", "fp_poly"):
        for node in sexpr.get_all(root, kind):
            if sexpr.get_value(node, "layer") in ("F.CrtYd", "B.CrtYd"):
                courtyard.append({"kind": kind, "layer": sexpr.get_value(node, "layer")})
    return {
        "lib_id": resolved["lib_id"],
        "path": str(path),
        "name": resolved["name"],
        "description": sexpr.get_value(root, "descr"),
        "tags": sexpr.get_value(root, "tags"),
        "pad_count": len(pads),
        "pads": pads,
        "models": models,
        "courtyard_items": courtyard,
    }


def list_libraries(kind: str = "symbols") -> dict:
    if kind == "symbols":
        rows = []
        for directory in symbol_dirs():
            for path in sorted(directory.glob("*.kicad_sym")):
                rows.append({"library": path.stem, "path": str(path)})
        return {"kind": kind, "count": len(rows), "libraries": rows}
    if kind == "footprints":
        rows = []
        for directory in footprint_dirs():
            for path in sorted(directory.glob("*.pretty")):
                rows.append({"library": path.stem, "path": str(path)})
        return {"kind": kind, "count": len(rows), "libraries": rows}
    raise ValueError("kind must be 'symbols' or 'footprints'")
