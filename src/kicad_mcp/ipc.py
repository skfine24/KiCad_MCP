import math
import os
import platform
import shutil
import subprocess
import time
from tempfile import gettempdir


def _default_socket_path() -> str:
    path = os.environ.get("KICAD_API_SOCKET")
    if path:
        return path
    if platform.system() == "Windows":
        return f"ipc://{gettempdir()}\\kicad\\api.sock"
    return "ipc:///tmp/kicad/api.sock"


def _connect(timeout_seconds: float = 3.0):
    from kipy import KiCad
    from kipy.client import KiCadClient

    client = KiCadClient(
        _default_socket_path(),
        "kicad-mcp",
        os.environ.get("KICAD_API_TOKEN", ""),
        timeout_ms=int(timeout_seconds * 1000),
    )
    return KiCad.from_client(client)


def _require_board(kicad):
    board = kicad.get_board()
    if board is None:
        raise RuntimeError("no PCB document is open in KiCad")
    return board


def _mm(nm) -> float | None:
    if nm is None:
        return None
    return round(nm / 1e6, 4)


def _vec_mm(v) -> list[float]:
    return [_mm(v.x), _mm(v.y)]


def _layer_name(board, layer) -> str:
    try:
        return board.get_layer_name(layer)
    except Exception:
        return str(layer)


def _item_layers(item) -> list:
    try:
        if hasattr(item, "layers"):
            return list(item.layers)
    except Exception:
        pass
    try:
        if hasattr(item, "layer"):
            return [item.layer]
    except Exception:
        pass
    try:
        return [cl.layer for cl in item.padstack.copper_layers]
    except Exception:
        return []


def _item_summary(item, board) -> dict:
    kind = type(item).__name__
    base: dict = {"kind": kind}
    try:
        if getattr(item, "id", None) is not None:
            base["id"] = item.id.value
    except Exception:
        pass
    try:
        if kind in ("Track", "ArcTrack"):
            base["layer"] = _layer_name(board, item.layer)
            base["net"] = item.net.name
            base["width_mm"] = _mm(item.width)
            base["start_mm"] = _vec_mm(item.start)
            if kind == "Track":
                base["end_mm"] = _vec_mm(item.end)
                base["length_mm"] = _mm(int(item.length()))
            else:
                base["mid_mm"] = _vec_mm(item.mid)
                base["end_mm"] = _vec_mm(item.end)
        elif kind == "Via":
            base["position_mm"] = _vec_mm(item.position)
            base["net"] = item.net.name
            base["diameter_mm"] = _mm(item.diameter)
            base["drill_mm"] = _mm(item.drill_diameter)
            base["via_type"] = int(item.type)
        elif kind == "Pad":
            size = None
            try:
                if item.padstack.copper_layers:
                    s = item.padstack.copper_layers[0].size
                    size = [_mm(s.x), _mm(s.y)]
            except Exception:
                pass
            base.update({
                "number": item.number,
                "net": item.net.name,
                "position_mm": _vec_mm(item.position),
                "pad_type": int(item.pad_type),
                "size_mm": size,
            })
        elif kind == "FootprintInstance":
            base.update({
                "reference": item.reference_field.text.value,
                "value": item.value_field.text.value,
                "position_mm": _vec_mm(item.position),
                "layer": _layer_name(board, item.layer),
                "orientation_deg": item.orientation.degrees,
            })
        elif kind == "Zone":
            base.update({
                "net": item.net.name,
                "name": item.name,
                "layers": [_layer_name(board, l) for l in item.layers],
                "filled": bool(item.filled),
                "priority": item.priority,
                "clearance_mm": _mm(item.clearance),
                "min_thickness_mm": _mm(item.min_thickness),
            })
        else:
            base["repr"] = repr(item)
    except Exception as exc:
        base["error"] = f"{type(exc).__name__}: {exc}"
    return base


def get_status(timeout_seconds: float = 3.0) -> dict:
    try:
        from kipy import KiCad  # noqa: F401
        from kipy.client import KiCadClient  # noqa: F401
    except ImportError as e:
        return {"connected": False, "error": f"kicad-python not installed: {e}"}
    try:
        kicad = _connect(timeout_seconds)
        version = kicad.get_version()
    except Exception as e:
        return {"connected": False, "error": f"IPC connect failed: {type(e).__name__}: {e}"}
    result = {
        "connected": True,
        "kicad_version": version.full_version if version else None,
        "api_version": None,
        "open_board": None,
    }
    try:
        api = kicad.get_api_version()
        result["api_version"] = api.full_version if hasattr(api, "full_version") else str(api)
    except Exception:
        pass
    try:
        board = kicad.get_board()
        if board is not None:
            result["open_board"] = board.name
    except Exception as e:
        result["board_error"] = f"{type(e).__name__}: {e}"
    return result


def _find_pcbnew() -> str:
    program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    for version in ("10.0", "9.0", "8.0"):
        candidate = os.path.join(program_files, "KiCad", version, "bin", "pcbnew.exe")
        if os.path.isfile(candidate):
            return candidate
    which = shutil.which("pcbnew")
    if which:
        return which
    raise FileNotFoundError("pcbnew.exe not found; set KICAD_PCBNEW or add KiCad bin to PATH")


def _pcbnew_running() -> bool:
    if platform.system() != "Windows":
        return False
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq pcbnew.exe", "/NH"],
            capture_output=True, text=True, timeout=5,
        ).stdout
        return "pcbnew.exe" in out.lower()
    except Exception:
        return False


def open_board(pcb_path: str, timeout_seconds: float = 30.0) -> dict:
    """Open a PCB in the KiCad PCB editor and wait until the IPC document is available.
    The IPC API has no open-document command, so this launches pcbnew.exe with the file
    when no PCB editor is running. If pcbnew.exe is already running it is not launched
    again, to avoid disrupting an open editor with unsaved changes."""
    from pathlib import Path

    path = Path(pcb_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"pcb not found: {path}")
    board_name = path.name
    last = None
    for _ in range(4):
        try:
            status = get_status(timeout_seconds=2.0)
        except Exception as e:
            last = {"error": f"{type(e).__name__}: {e}"}
        else:
            if status.get("open_board") == board_name:
                return {"opened": True, "already_open": True, "board": board_name,
                        "kicad_version": status.get("kicad_version"),
                        "api_version": status.get("api_version")}
            last = status
        time.sleep(0.5)
    if _pcbnew_running():
        deadline = time.time() + float(timeout_seconds)
        while time.time() < deadline:
            time.sleep(1.0)
            try:
                status = get_status(timeout_seconds=2.0)
            except Exception as e:
                last = {"error": f"{type(e).__name__}: {e}"}
                continue
            if status.get("open_board") == board_name:
                return {"opened": True, "already_open": True, "board": board_name,
                        "kicad_version": status.get("kicad_version"),
                        "api_version": status.get("api_version")}
            last = status
        return {"opened": False, "board": board_name,
                "reason": "pcbnew.exe is already running; open the board manually "
                          "or close the PCB editor first",
                "last_status": last}
    exe = _find_pcbnew()
    proc = subprocess.Popen([exe, str(path)], close_fds=True)
    deadline = time.time() + float(timeout_seconds)
    while time.time() < deadline:
        time.sleep(1.0)
        try:
            status = get_status(timeout_seconds=2.0)
        except Exception as e:
            last = {"error": f"{type(e).__name__}: {e}"}
            continue
        if status.get("open_board") == board_name:
            return {"opened": True, "already_open": False, "board": board_name,
                    "pid": proc.pid, "kicad_version": status.get("kicad_version"),
                    "api_version": status.get("api_version")}
        last = status
    return {"opened": False, "board": board_name, "pid": proc.pid, "last_status": last}


def board_info(timeout_seconds: float = 3.0) -> dict:
    kicad = _connect(timeout_seconds)
    board = _require_board(kicad)
    project = board.get_project()
    tb = board.get_title_block_info()
    enabled = list(board.get_enabled_layers())
    visible = list(board.get_visible_layers())
    enabled_set = set(enabled)
    return {
        "board": board.name,
        "project": {"name": project.name, "path": project.path},
        "title_block": {
            "title": tb.title,
            "date": tb.date,
            "revision": tb.revision,
            "company": tb.company,
        },
        "copper_layers": board.get_copper_layer_count(),
        "active_layer": _layer_name(board, board.get_active_layer()),
        "enabled_layers": [_layer_name(board, l) for l in enabled],
        "visible_layers": [_layer_name(board, l) for l in visible if l in enabled_set],
        "counts": {
            "footprints": len(board.get_footprints()),
            "tracks": len(board.get_tracks()),
            "vias": len(board.get_vias()),
            "pads": len(board.get_pads()),
            "zones": len(board.get_zones()),
            "dimensions": len(board.get_dimensions()),
            "text": len(board.get_text()),
            "shapes": len(board.get_shapes()),
            "nets": len(board.get_nets()),
        },
    }


def list_nets(timeout_seconds: float = 3.0, netclass_filter=None) -> dict:
    kicad = _connect(timeout_seconds)
    board = _require_board(kicad)
    nets = list(board.get_nets(netclass_filter))
    classes = board.get_netclass_for_nets(nets) if nets else {}
    rows = []
    for n in nets:
        nc = classes.get(n.name)
        rows.append({
            "name": n.name,
            "netclass": nc.name if nc else None,
            "track_width_mm": _mm(nc.track_width) if nc and nc.track_width is not None else None,
            "via_diameter_mm": _mm(nc.via_diameter) if nc and nc.via_diameter is not None else None,
        })
    return {"count": len(rows), "nets": rows}


_ITEM_GETTERS = {
    "footprints": lambda b: b.get_footprints(),
    "tracks": lambda b: b.get_tracks(),
    "vias": lambda b: b.get_vias(),
    "pads": lambda b: b.get_pads(),
    "zones": lambda b: b.get_zones(),
    "dimensions": lambda b: b.get_dimensions(),
    "text": lambda b: b.get_text(),
    "shapes": lambda b: b.get_shapes(),
}


def list_items(kind: str = "tracks", net: str | None = None, layer: str | None = None,
               timeout_seconds: float = 3.0) -> dict:
    kicad = _connect(timeout_seconds)
    board = _require_board(kicad)
    getter = _ITEM_GETTERS.get(kind)
    if getter is None:
        raise ValueError(f"unknown kind: {kind}; expected one of {sorted(_ITEM_GETTERS)}")
    rows = []
    for item in getter(board):
        if net is not None:
            try:
                if item.net.name != net:
                    continue
            except Exception:
                continue
        if layer is not None:
            names = [_layer_name(board, l) for l in _item_layers(item)]
            if layer not in names:
                continue
        rows.append(_item_summary(item, board))
    return {"kind": kind, "count": len(rows), "items": rows}


def get_selection(timeout_seconds: float = 3.0) -> dict:
    kicad = _connect(timeout_seconds)
    board = _require_board(kicad)
    sel = list(board.get_selection())
    return {
        "count": len(sel),
        "as_string": board.get_selection_as_string(),
        "items": [_item_summary(i, board) for i in sel],
    }


def items_at_point(x_mm: float, y_mm: float, tolerance_mm: float = 0.0,
                   timeout_seconds: float = 3.0) -> dict:
    kicad = _connect(timeout_seconds)
    board = _require_board(kicad)
    hits = []
    px, py = float(x_mm), float(y_mm)
    tol = float(tolerance_mm)

    def _dist_point_seg(px: float, py: float, x1: float, y1: float,
                        x2: float, y2: float) -> float:
        dx, dy = x2 - x1, y2 - y1
        l2 = dx * dx + dy * dy
        if l2 == 0.0:
            return math.hypot(px - x1, py - y1)
        t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / l2))
        return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))

    for t in board.get_tracks():
        x1, y1 = _vec_mm(t.start)
        x2, y2 = _vec_mm(t.end)
        if _dist_point_seg(px, py, x1, y1, x2, y2) <= (t.width / 2e6) + tol:
            hits.append(_item_summary(t, board))
    for v in board.get_vias():
        vx, vy = _vec_mm(v.position)
        if math.hypot(px - vx, py - vy) <= (v.diameter / 2e6) + tol:
            hits.append(_item_summary(v, board))
    for p in board.get_pads():
        ppx, ppy = _vec_mm(p.position)
        try:
            s = p.padstack.copper_layers[0].size
            rx, ry = s.x / 2e6, s.y / 2e6
        except Exception:
            rx = ry = 0.0
        if abs(px - ppx) <= rx + tol and abs(py - ppy) <= ry + tol:
            hits.append(_item_summary(p, board))
    return {"point_mm": [px, py], "tolerance_mm": tol, "count": len(hits), "items": hits}


def add_track(points: list[list[float]], layer: str, width_mm: float, net: str,
              timeout_seconds: float = 5.0) -> dict:
    from kipy.board_types import Net, Track
    from kipy.geometry import Vector2

    kicad = _connect(timeout_seconds)
    board = _require_board(kicad)
    layer_id = board.get_layer_by_name(layer)
    width_nm = int(round(float(width_mm) * 1e6))
    net_obj = Net(name=net)
    items = []
    for i in range(len(points) - 1):
        t = Track()
        t.start = Vector2.from_xy_mm(float(points[i][0]), float(points[i][1]))
        t.end = Vector2.from_xy_mm(float(points[i + 1][0]), float(points[i + 1][1]))
        t.width = width_nm
        t.layer = layer_id
        t.net = net_obj
        items.append(t)
    created = board.create_items(items)
    return {
        "added": len(created),
        "ids": [i.id.value for i in created],
        "layer": layer,
        "width_mm": float(width_mm),
        "net": net,
    }


def add_via(x_mm: float, y_mm: float, net: str, diameter_mm: float = 0.6,
            drill_mm: float = 0.3, timeout_seconds: float = 5.0) -> dict:
    from kipy.board_types import Net, Via
    from kipy.geometry import Vector2

    kicad = _connect(timeout_seconds)
    board = _require_board(kicad)
    v = Via()
    v.position = Vector2.from_xy_mm(float(x_mm), float(y_mm))
    v.diameter = int(round(float(diameter_mm) * 1e6))
    v.drill_diameter = int(round(float(drill_mm) * 1e6))
    v.net = Net(name=net)
    created = board.create_items([v])
    return {
        "added": len(created),
        "ids": [i.id.value for i in created],
        "at_mm": [float(x_mm), float(y_mm)],
        "diameter_mm": float(diameter_mm),
        "drill_mm": float(drill_mm),
        "net": net,
    }


def delete_items(ids: list[str], timeout_seconds: float = 5.0) -> dict:
    from kipy.proto.common.types import KIID

    kicad = _connect(timeout_seconds)
    board = _require_board(kicad)
    board.remove_items_by_id([KIID(value=i) for i in ids])
    return {"removed": len(ids), "ids": list(ids)}


def save_board(timeout_seconds: float = 10.0) -> dict:
    kicad = _connect(timeout_seconds)
    board = _require_board(kicad)
    board.save()
    return {"saved": True, "board": board.name}


def refill_zones_live(timeout_seconds: float = 30.0) -> dict:
    kicad = _connect(timeout_seconds)
    board = _require_board(kicad)
    board.refill_zones(block=True, max_poll_seconds=timeout_seconds)
    return {"refilled": True, "board": board.name}


def set_active_layer(layer_name: str, timeout_seconds: float = 3.0) -> dict:
    kicad = _connect(timeout_seconds)
    board = _require_board(kicad)
    layer = board.get_layer_by_name(layer_name)
    board.set_active_layer(layer)
    return {"active_layer": _layer_name(board, board.get_active_layer())}
