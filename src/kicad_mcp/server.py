import json
import os
from pathlib import Path

from fastmcp import FastMCP

from . import board_parser, ipc, kicad_cli, pcb_edit, sch_parser

mcp = FastMCP("kicad")


def _resolve(path: str) -> Path:
    return Path(path).expanduser().resolve()


@mcp.tool
def get_kicad_version() -> dict:
    """Get kicad-cli version and live KiCad IPC connection status."""
    result: dict = {"kicad_cli": kicad_cli._version()}
    result["ipc"] = _ipc_status_safe()
    return result


def _ipc_status_safe() -> dict:
    from . import ipc

    return ipc.get_status()


@mcp.tool
def list_projects(directory: str) -> dict:
    """List KiCad projects (*.kicad_pro) in a directory (non-recursive)."""
    root = _resolve(directory)
    if not root.is_dir():
        raise FileNotFoundError(f"directory not found: {root}")
    projects = sorted(str(p) for p in root.glob("*.kicad_pro"))
    return {"directory": str(root), "projects": projects, "count": len(projects)}


@mcp.tool
def create_project(project_path: str) -> dict:
    """Create a new KiCad project. project_path may be a directory (project name = folder name) or a path ending in .kicad_pro."""
    p = _resolve(project_path)
    if p.suffix.lower() == ".kicad_pro":
        base = p.with_suffix("")
    else:
        base = p
    base.parent.mkdir(parents=True, exist_ok=True)

    sch_path = base.with_suffix(".kicad_sch")
    pro_path = base.with_suffix(".kicad_pro")
    pcb_path = base.with_suffix(".kicad_pcb")

    if pro_path.exists() or sch_path.exists() or pcb_path.exists():
        raise FileExistsError(f"project files already exist: {base}")

    sch_content = (
        "(kicad_sch\n"
        "\t(version 20260101)\n"
        '\t(generator "eeschema")\n'
        '\t(generator_version "10.0")\n'
        "\t(uuid \"{}\")\n"
        '\t(paper "A4")\n'
        "\t(title_block)\n"
        "\t(lib_symbols)\n"
        "\t(sheet_instances\n"
        '\t\t(path "/"\n'
        '\t\t\t(page "1")\n'
        "\t\t)\n"
        "\t)\n"
        "\t(embedded_fonts no)\n"
        ")\n"
    ).format(_new_uuid())
    sch_path.write_text(sch_content, encoding="utf-8")

    board_result = kicad_cli.create_board_file(pcb_path)
    if not board_result["created"]:
        raise RuntimeError(f"failed to create board file: {board_result.get('stderr')}")

    project_json = {
        "board": {"design_settings": {}, "layer_presets": [], "viewports": []},
        "boards": [],
        "cvpcb": {"equivalence_files": []},
        "libraries": {"pinned_footprint_libs": [], "pinned_symbol_libs": []},
        "meta": {"filename": pro_path.name, "version": 3},
        "net_settings": {"classes": [{"name": "Default", "clearance": 0.2, "track_width": 0.2, "via_diameter": 0.6, "via_drill": 0.3}]},
        "schematic": {"drawing": {}, "legacy_lib_dir": "", "legacy_lib_list": []},
        "sheets": [],
        "text_variables": {},
    }
    pro_path.write_text(json.dumps(project_json, indent=2), encoding="utf-8")

    return {
        "project": str(pro_path),
        "files": {
            "schematic": str(sch_path),
            "pcb": str(pcb_path),
            "project": str(pro_path),
        },
    }


def _new_uuid() -> str:
    import uuid

    return str(uuid.uuid4())


@mcp.tool
def read_schematic(schematic_path: str) -> dict:
    """Read a KiCad schematic: title, component list, wires, labels, hierarchical sheets."""
    return sch_parser.read_schematic(_resolve(schematic_path))


@mcp.tool
def list_components(schematic_path: str, filter_text: str | None = None) -> dict:
    """List schematic components. Optional filter matches reference, value, lib_id or footprint (case-insensitive substring)."""
    comps = sch_parser.list_components(_resolve(schematic_path), filter_text)
    return {"count": len(comps), "components": comps}


@mcp.tool
def export_netlist(schematic_path: str, output_path: str | None = None) -> dict:
    """Export a KiCad netlist from a schematic (kicad-cli headless)."""
    return kicad_cli.export_netlist(_resolve(schematic_path), output_path)


@mcp.tool
def list_nets(schematic_path: str) -> dict:
    """List all nets with pin-level connections, derived from an exported netlist (100% accurate connectivity)."""
    sch = _resolve(schematic_path)
    netlist_path = sch.parent / f"{sch.stem}-netlist.net"
    if not netlist_path.is_file():
        kicad_cli.export_netlist(sch)
    data = sch_parser.read_netlist(netlist_path)
    return {
        "net_count": len(data["nets"]),
        "component_count": len(data["components"]),
        "nets": data["nets"],
        "components": [
            {"ref": c["ref"], "value": c["value"], "footprint": c["footprint"]} for c in data["components"]
        ],
    }


@mcp.tool
def trace_net(schematic_path: str, net_name: str) -> dict:
    """Trace all pin connections for a specific net by name (case-insensitive substring match)."""
    sch = _resolve(schematic_path)
    netlist_path = sch.parent / f"{sch.stem}-netlist.net"
    if not netlist_path.is_file():
        kicad_cli.export_netlist(sch)
    data = sch_parser.read_netlist(netlist_path)
    needle = net_name.lower()
    matches = [n for n in data["nets"] if n["name"] and needle in n["name"].lower()]
    return {"query": net_name, "matches": matches, "match_count": len(matches)}


@mcp.tool
def run_erc(schematic_path: str, report_dir: str | None = None) -> dict:
    """Run Electrical Rules Check (ERC) on a schematic and return violations."""
    return kicad_cli.run_erc(_resolve(schematic_path), report_dir)


@mcp.tool
def run_drc(pcb_path: str, report_dir: str | None = None, schematic_parity: bool = False,
            refill_first: bool = False) -> dict:
    """Run Design Rules Check (DRC) on a PCB and return violations. With refill_first, copper zones are refilled and saved before DRC so stale fills do not produce false clearance errors."""
    pcb = _resolve(pcb_path)
    refill = None
    if refill_first:
        refill = pcb_edit.refill_zones(pcb, backup=False)
    result = kicad_cli.run_drc(pcb, report_dir, schematic_parity=schematic_parity)
    if refill is not None:
        result["refill"] = refill.get("result") if refill.get("ok") else refill
    return result


@mcp.tool
def export_gerbers(pcb_path: str, output_dir: str | None = None, layers: str | None = None) -> dict:
    """Export Gerber files from a PCB. layers is a comma-separated list like 'F.Cu,B.Cu,F.Mask,B.Mask,Edge.Cuts' (default: all)."""
    return kicad_cli.export_gerbers(_resolve(pcb_path), output_dir, layers)


@mcp.tool
def export_drill(pcb_path: str, output_dir: str | None = None, generate_map: bool = False) -> dict:
    """Export Excellon drill files from a PCB."""
    return kicad_cli.export_drill(_resolve(pcb_path), output_dir, generate_map)


@mcp.tool
def export_bom(schematic_path: str, output_path: str | None = None) -> dict:
    """Export a grouped CSV Bill of Materials from a schematic."""
    return kicad_cli.export_bom(_resolve(schematic_path), output_path)


@mcp.tool
def export_pos(pcb_path: str, output_path: str | None = None) -> dict:
    """Export component placement (pick & place) CSV from a PCB."""
    return kicad_cli.export_pos(_resolve(pcb_path), output_path)


@mcp.tool
def render_pcb(pcb_path: str, output_png: str | None = None, width: int = 1280, height: int = 720) -> dict:
    """Render a PCB to a PNG image (3D view via kicad-cli)."""
    return kicad_cli.render_pcb(_resolve(pcb_path), output_png, width, height)


@mcp.tool
def get_board_info(pcb_path: str) -> dict:
    """Inspect a PCB file: layer stack, nets, footprint list, track/via/zone counts, board size from Edge.Cuts."""
    return board_parser.read_board(_resolve(pcb_path))


@mcp.tool
def add_tracks(pcb_path: str, points: list[list[float]], layer: str, width: float, net: str, backup: bool = True) -> dict:
    """Add track segments along a polyline to a PCB. points=[[x,y],...] in mm (at least 2 points), layer 'F.Cu' or 'B.Cu', width in mm, net by name. Saves in place with a timestamped .bak backup (default). Run refill_zones afterwards."""
    return pcb_edit.add_tracks(_resolve(pcb_path), points, layer, width, net, backup=backup)


@mcp.tool
def add_via(pcb_path: str, x: float, y: float, net: str, diameter: float = 0.6, drill: float = 0.3, backup: bool = True) -> dict:
    """Add a through via at (x, y) mm with the given net (default 0.6 mm / 0.3 mm drill). Saves in place with a timestamped .bak backup (default)."""
    return pcb_edit.add_via(_resolve(pcb_path), x, y, net, diameter=diameter, drill=drill, backup=backup)


@mcp.tool
def refill_zones(pcb_path: str, backup: bool = True) -> dict:
    """Re-fill all copper zones (ZONE_FILLER) and save. Run after any routing change and before DRC/exports. Writes a timestamped .bak backup by default."""
    return pcb_edit.refill_zones(_resolve(pcb_path), backup=backup)


@mcp.tool
def find_isolated_islands(pcb_path: str, net: str = "GND") -> dict:
    """Analyze zone fill connectivity for a net: groups fill islands together with same-net pads/vias/tracks they touch and reports groups isolated from the main plane. Read-only; zone fills must be current (run refill_zones after routing changes)."""
    return pcb_edit.find_isolated_islands(_resolve(pcb_path), net)


@mcp.tool
def find_duplicate_tracks(pcb_path: str, net: str | None = None, max_gap_mm: float = 1.0,
                          min_overlap_mm: float = 2.0) -> dict:
    """Detect duplicate and redundant track geometry: exact duplicate segments, duplicate vias, collinear overlapping segments, parallel same-net pairs (gap <= max_gap_mm, projected overlap >= min_overlap_mm; classified loop vs series), zero-length segments, dangling track ends, single-layer/isolated vias. Read-only; run refill_zones first so zone fills are current."""
    return pcb_edit.find_duplicate_tracks(_resolve(pcb_path), net, max_gap_mm=max_gap_mm, min_overlap_mm=min_overlap_mm)


@mcp.tool
def delete_tracks(pcb_path: str, uuids: list[str], backup: bool = True) -> dict:
    """Delete track and via items by UUID (batch). Saves in place with a timestamped .bak backup by default."""
    return pcb_edit.delete_tracks(_resolve(pcb_path), uuids, backup=backup)


@mcp.tool
def trim_track(pcb_path: str, uuid: str, from_mm: list[float], to_mm: list[float],
               backup: bool = True) -> dict:
    """Move the track endpoint nearest to from_mm to to_mm (mm), keeping the other end. Saves in place with a timestamped .bak backup by default."""
    return pcb_edit.trim_track(_resolve(pcb_path), uuid, from_mm, to_mm, backup=backup)


@mcp.tool
def prune_dangling(pcb_path: str, backup: bool = True, protect_pads: bool = True,
                   max_iterations: int = 200) -> dict:
    """Remove dangling track segments (KiCad connectivity) one at a time until none remain. protect_pads (default) skips tracks connected to a pad so a pad's only connection is never removed. Saves in place with a timestamped .bak backup by default."""
    return pcb_edit.prune_dangling(_resolve(pcb_path), backup=backup, protect_pads=protect_pads, max_iterations=max_iterations)


@mcp.tool
def edit_begin(pcb_path: str) -> dict:
    """Copy a PCB to a fast local staging file for editing (useful for slow or network paths). Edit the staging copy, then apply it with edit_commit."""
    return pcb_edit.edit_begin(_resolve(pcb_path))


@mcp.tool
def edit_commit(staging_path: str, target_path: str | None = None) -> dict:
    """Copy an edited staging PCB (from edit_begin) back over its source file or an explicit target_path. Overwrites the target and removes the staging copy."""
    return pcb_edit.edit_commit(staging_path, target_path)


@mcp.tool
def ipc_open_board(pcb_path: str, timeout_seconds: float = 30.0) -> dict:
    """Open a PCB in the KiCad PCB editor (launches pcbnew.exe) and wait until the IPC document is available. Use before live IPC tools when no board is open."""
    return ipc.open_board(str(_resolve(pcb_path)), timeout_seconds=timeout_seconds)


@mcp.tool
def ipc_board_info(timeout_seconds: float = 3.0) -> dict:
    """Live board summary from the open KiCad GUI via IPC: project, title block, layers and item counts. Requires KiCad GUI with the API server enabled and a PCB open."""
    return ipc.board_info(timeout_seconds)


@mcp.tool
def ipc_list_nets(netclass_filter: str | None = None, timeout_seconds: float = 3.0) -> dict:
    """List nets (and netclasses) on the open KiCad board via IPC. netclass_filter is an optional netclass name."""
    return ipc.list_nets(timeout_seconds, netclass_filter=netclass_filter)


@mcp.tool
def ipc_list_items(kind: str = "tracks", net: str | None = None, layer: str | None = None,
                   timeout_seconds: float = 3.0) -> dict:
    """List items on the open KiCad board via IPC. kind: footprints, tracks, vias, pads, zones, dimensions, text, shapes. Optional net and layer filters."""
    return ipc.list_items(kind, net=net, layer=layer, timeout_seconds=timeout_seconds)


@mcp.tool
def ipc_get_selection(timeout_seconds: float = 3.0) -> dict:
    """Get the current selection in the open KiCad board via IPC."""
    return ipc.get_selection(timeout_seconds)


@mcp.tool
def ipc_items_at_point(x_mm: float, y_mm: float, tolerance_mm: float = 0.0,
                       timeout_seconds: float = 3.0) -> dict:
    """Find tracks, vias and pads at a point (mm) on the open KiCad board via IPC. Pad hits are bounding-box based."""
    return ipc.items_at_point(x_mm, y_mm, tolerance_mm=tolerance_mm, timeout_seconds=timeout_seconds)


@mcp.tool
def ipc_add_track(points: list[list[float]], layer: str, width_mm: float, net: str,
                  timeout_seconds: float = 5.0) -> dict:
    """Add track segments along a polyline to the open KiCad board via IPC. points=[[x,y],...] in mm; commits immediately (undoable in KiCad)."""
    return ipc.add_track(points, layer, width_mm, net, timeout_seconds=timeout_seconds)


@mcp.tool
def ipc_add_via(x_mm: float, y_mm: float, net: str, diameter_mm: float = 0.6,
                drill_mm: float = 0.3, timeout_seconds: float = 5.0) -> dict:
    """Add a through via to the open KiCad board via IPC. Commits immediately (undoable in KiCad)."""
    return ipc.add_via(x_mm, y_mm, net, diameter_mm=diameter_mm, drill_mm=drill_mm, timeout_seconds=timeout_seconds)


@mcp.tool
def ipc_delete_items(ids: list[str], timeout_seconds: float = 5.0) -> dict:
    """Delete board items by their IPC IDs (from ipc_list_items or ipc_get_selection) on the open KiCad board."""
    return ipc.delete_items(ids, timeout_seconds=timeout_seconds)


@mcp.tool
def ipc_save(timeout_seconds: float = 10.0) -> dict:
    """Save the open KiCad board via IPC (writes the document to its file)."""
    return ipc.save_board(timeout_seconds)


@mcp.tool
def ipc_refill_zones(timeout_seconds: float = 30.0) -> dict:
    """Refill all copper zones on the open KiCad board via IPC (blocks until complete)."""
    return ipc.refill_zones_live(timeout_seconds)


@mcp.tool
def ipc_set_active_layer(layer_name: str, timeout_seconds: float = 3.0) -> dict:
    """Set the active layer of the open KiCad board via IPC (for example 'F.Cu')."""
    return ipc.set_active_layer(layer_name, timeout_seconds)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
