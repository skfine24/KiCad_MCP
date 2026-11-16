import json
import os
from pathlib import Path

from fastmcp import Context, FastMCP
from fastmcp.resources import ResourceSecurity

from . import board_parser, bom, diagnostics, ipc, kicad_cli, library, manufacturing, mechanical, paths, pcb_edit, sch_edit, sch_parser

mcp = FastMCP("kicad")

READ_ONLY = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}
WRITE = {
    "readOnlyHint": False,
    "destructiveHint": True,
    "idempotentHint": False,
    "openWorldHint": False,
}


def _object_schema(properties: dict) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "additionalProperties": True,
    }


_ARRAY_OBJECT = {"type": "array", "items": {"type": "object", "additionalProperties": True}}
_REVISION_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string"},
        "exists": {"type": "boolean"},
        "mtime_ns": {"type": ["integer", "null"]},
        "size": {"type": ["integer", "null"]},
        "sha256": {"type": ["string", "null"]},
    },
    "additionalProperties": True,
}


def _resolve(path: str) -> Path:
    return paths.normalize_path(path)


def _safe_mode() -> bool:
    return os.environ.get("KICAD_MCP_SAFE_MODE", "").lower() in ("1", "true", "yes", "on")


def _require_confirm(confirm: bool, action: str) -> None:
    if _safe_mode() and not confirm:
        raise RuntimeError(
            f"{action} requires confirm=True because KICAD_MCP_SAFE_MODE is enabled"
        )


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


@mcp.tool(annotations=READ_ONLY)
def sch_revision(schematic_path: str) -> dict:
    """Return mtime/size/SHA-256 revision for a schematic (use as expected_revision)."""
    return sch_edit.sch_revision(_resolve(schematic_path))


@mcp.tool(annotations=WRITE)
def sch_add_wire(
    schematic_path: str,
    points: list[list[float]],
    backup: bool = True,
    dry_run: bool = False,
    expected_revision: str | None = None,
) -> dict:
    """Add wire segments along a polyline (points in mm)."""
    return sch_edit.sch_add_wire(
        _resolve(schematic_path), points,
        backup=backup, dry_run=dry_run, expected_revision=expected_revision,
    )


@mcp.tool(annotations=WRITE)
def sch_add_label(
    schematic_path: str,
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
    """Add a local, global or hierarchical label."""
    return sch_edit.sch_add_label(
        _resolve(schematic_path), text, x, y, kind=kind, rotation=rotation, shape=shape,
        backup=backup, dry_run=dry_run, expected_revision=expected_revision,
    )


@mcp.tool(annotations=WRITE)
def sch_add_no_connect(
    schematic_path: str,
    x: float,
    y: float,
    backup: bool = True,
    dry_run: bool = False,
    expected_revision: str | None = None,
) -> dict:
    """Add a no-connect marker at (x, y) mm."""
    return sch_edit.sch_add_no_connect(
        _resolve(schematic_path), x, y,
        backup=backup, dry_run=dry_run, expected_revision=expected_revision,
    )


@mcp.tool(annotations=WRITE)
def sch_set_value(
    schematic_path: str,
    ref: str,
    value: str,
    backup: bool = True,
    dry_run: bool = False,
    expected_revision: str | None = None,
) -> dict:
    """Set a symbol's Value field."""
    return sch_edit.sch_set_value(
        _resolve(schematic_path), ref, value,
        backup=backup, dry_run=dry_run, expected_revision=expected_revision,
    )


@mcp.tool(annotations=WRITE)
def sch_set_property(
    schematic_path: str,
    ref: str,
    name: str,
    value: str,
    backup: bool = True,
    dry_run: bool = False,
    expected_revision: str | None = None,
) -> dict:
    """Set or add a symbol property (MPN, Manufacturer, Supplier, ...)."""
    return sch_edit.sch_set_property(
        _resolve(schematic_path), ref, name, value,
        backup=backup, dry_run=dry_run, expected_revision=expected_revision,
    )


@mcp.tool(annotations=WRITE)
def sch_add_symbol(
    schematic_path: str,
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
    """Place a symbol from a KiCad library (embeds/flattens the lib_symbols entry)."""
    return sch_edit.sch_add_symbol(
        _resolve(schematic_path), lib_id, ref, value, x, y,
        rotation=rotation, unit=unit, mirror=mirror, footprint=footprint, dnp=dnp,
        backup=backup, dry_run=dry_run, expected_revision=expected_revision,
    )


@mcp.tool(annotations=WRITE)
def sch_delete_item(
    schematic_path: str,
    item_uuid: str,
    backup: bool = True,
    dry_run: bool = False,
    expected_revision: str | None = None,
) -> dict:
    """Delete a schematic item by UUID (symbol/wire/label/no_connect)."""
    return sch_edit.sch_delete_item(
        _resolve(schematic_path), item_uuid,
        backup=backup, dry_run=dry_run, expected_revision=expected_revision,
    )


@mcp.tool(annotations=READ_ONLY)
def search_symbols(
    query: str,
    library_name: str | None = None,
    limit: int = 50,
    refresh: bool = False,
) -> dict:
    """Search KiCad symbol libraries by name, description or keywords."""
    return library.search_symbols(query, library=library_name, limit=limit, refresh=refresh)


@mcp.tool(annotations=READ_ONLY)
def search_footprints(
    query: str,
    library_name: str | None = None,
    limit: int = 50,
    refresh: bool = False,
) -> dict:
    """Search KiCad footprint libraries by name, description or tags."""
    return library.search_footprints(query, library=library_name, limit=limit, refresh=refresh)


@mcp.tool(annotations=READ_ONLY)
def get_pinout(lib_id: str) -> dict:
    """Return a symbol's pins (number, name, type, unit, position)."""
    return library.get_pinout(lib_id)


@mcp.tool(annotations=READ_ONLY)
def get_footprint_info(footprint: str) -> dict:
    """Return footprint pads, courtyard items and 3D model paths."""
    return library.get_footprint_info(footprint)


@mcp.tool(annotations=READ_ONLY)
def list_libraries(kind: str = "symbols") -> dict:
    """List installed symbol or footprint libraries."""
    return library.list_libraries(kind)


@mcp.tool
def export_netlist(schematic_path: str, output_path: str | None = None, refresh: bool = True) -> dict:
    """Export a KiCad netlist from a schematic (kicad-cli headless)."""
    return kicad_cli.export_netlist(_resolve(schematic_path), output_path, refresh=refresh)


def _netlist_for(sch: Path, refresh: bool = True, expected_revision: str | None = None) -> dict:
    if expected_revision and not paths.revision_matches(sch, expected_revision):
        raise RuntimeError(
            "revision mismatch: schematic changed since it was read "
            f"(expected {expected_revision})"
        )
    netlist_path = sch.parent / f"{sch.stem}-netlist.net"
    meta_path = netlist_path.with_name(netlist_path.name + ".meta.json")
    source_rev = paths.file_revision(sch)
    netlist_rev = paths.file_revision(netlist_path)
    stale = False
    if netlist_path.is_file():
        stale = netlist_path.stat().st_mtime_ns < sch.stat().st_mtime_ns
        if not stale and meta_path.is_file():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                meta_source = meta.get("source_revision", {}).get("sha256")
                stale = bool(meta_source and meta_source != source_rev.get("sha256"))
            except Exception:
                stale = True
    export = refresh or not netlist_path.is_file() or stale
    export_result = kicad_cli.export_netlist(sch, netlist_path, refresh=export)
    return {
        "netlist": str(netlist_path),
        "meta": export_result.get("meta"),
        "meta_path": str(meta_path),
        "exported": export_result.get("exported", export),
        "stale": stale,
        "source_revision": source_rev,
        "netlist_revision": export_result.get("revision") or paths.file_revision(netlist_path),
    }


@mcp.tool(
    annotations=READ_ONLY,
    output_schema=_object_schema(
        {
            "net_count": {"type": "integer"},
            "component_count": {"type": "integer"},
            "nets": _ARRAY_OBJECT,
            "components": _ARRAY_OBJECT,
            "netlist": {"type": "string"},
            "exported": {"type": "boolean"},
            "stale": {"type": "boolean"},
            "source_revision": _REVISION_SCHEMA,
            "netlist_revision": _REVISION_SCHEMA,
        }
    ),
)
def list_nets(
    schematic_path: str,
    refresh: bool = True,
    expected_revision: str | None = None,
) -> dict:
    """List all nets with pin-level connections from a fresh netlist export.

    refresh=True (default) always re-exports the netlist. Set refresh=False to
    reuse the cached netlist only when its mtime/hash matches the schematic.
    expected_revision refuses to read when the schematic SHA-256 changed.
    """
    sch = _resolve(schematic_path)
    netlist = _netlist_for(sch, refresh=refresh, expected_revision=expected_revision)
    data = sch_parser.read_netlist(netlist["netlist"])
    return {
        "net_count": len(data["nets"]),
        "component_count": len(data["components"]),
        "nets": data["nets"],
        "components": [
            {"ref": c["ref"], "value": c["value"], "footprint": c["footprint"]}
            for c in data["components"]
        ],
        "netlist": netlist["netlist"],
        "netlist_meta": netlist["meta"],
        "exported": netlist["exported"],
        "stale": netlist["stale"],
        "source_revision": netlist["source_revision"],
        "netlist_revision": netlist["netlist_revision"],
    }


@mcp.tool(annotations=READ_ONLY)
def trace_net(
    schematic_path: str,
    net_name: str,
    refresh: bool = True,
    expected_revision: str | None = None,
) -> dict:
    """Trace all pin connections for a specific net by name (case-insensitive substring match)."""
    sch = _resolve(schematic_path)
    netlist = _netlist_for(sch, refresh=refresh, expected_revision=expected_revision)
    data = sch_parser.read_netlist(netlist["netlist"])
    needle = net_name.lower()
    matches = [n for n in data["nets"] if n["name"] and needle in n["name"].lower()]
    return {
        "query": net_name,
        "matches": matches,
        "match_count": len(matches),
        "netlist": netlist["netlist"],
        "exported": netlist["exported"],
        "stale": netlist["stale"],
        "source_revision": netlist["source_revision"],
        "netlist_revision": netlist["netlist_revision"],
    }


@mcp.tool(annotations=READ_ONLY)
def run_erc(
    schematic_path: str,
    report_dir: str | None = None,
    offset: int = 0,
    limit: int = 200,
    severity: str | None = None,
    type_filter: str | None = None,
    net: str | None = None,
    include_items: bool = True,
    summary_only: bool = False,
    ctx: Context | None = None,
) -> dict:
    """Run ERC and return structured, paginated violations.

    severity/type_filter/net accept comma-separated or substring values.
    limit=0 returns every matching row. summary_only omits the rows and keeps
    the summary, counts and report path.
    """
    if ctx is not None:
        ctx.report_progress(0, 100, "running ERC")
    result = kicad_cli.run_erc(
        _resolve(schematic_path),
        report_dir,
        offset=offset,
        limit=limit,
        severity=severity,
        type_filter=type_filter,
        net=net,
        include_items=include_items,
        summary_only=summary_only,
    )
    if ctx is not None:
        ctx.report_progress(100, 100, "ERC complete")
    return result


@mcp.tool(
    annotations=READ_ONLY,
    output_schema=_object_schema(
        {
            "command": {"type": "string"},
            "source": {"type": "string"},
            "source_revision": _REVISION_SCHEMA,
            "report": {"type": "string"},
            "report_revision": _REVISION_SCHEMA,
            "counts": {"type": "object", "additionalProperties": True},
            "summary": {"type": "object", "additionalProperties": True},
            "violations": _ARRAY_OBJECT,
            "unconnected_items": _ARRAY_OBJECT,
            "page": {"type": "object", "additionalProperties": True},
            "unconnected_page": {"type": "object", "additionalProperties": True},
        }
    ),
)
def run_drc(
    pcb_path: str,
    report_dir: str | None = None,
    schematic_parity: bool = False,
    refill_first: bool = False,
    offset: int = 0,
    limit: int = 200,
    severity: str | None = None,
    type_filter: str | None = None,
    net: str | None = None,
    include_items: bool = True,
    summary_only: bool = False,
    ctx: Context | None = None,
) -> dict:
    """Run DRC and return structured, paginated violations.

    Each violation carries id/severity/type/nets/items (uuid, pos_mm, net).
    limit=0 returns every matching row; summary_only omits the rows.
    """
    pcb = _resolve(pcb_path)
    refill = None
    if refill_first:
        if ctx is not None:
            ctx.report_progress(0, 100, "refilling zones")
        refill = pcb_edit.refill_zones(pcb, backup=False)
    if ctx is not None:
        ctx.report_progress(10, 100, "running DRC")
    result = kicad_cli.run_drc(
        pcb,
        report_dir,
        schematic_parity=schematic_parity,
        offset=offset,
        limit=limit,
        severity=severity,
        type_filter=type_filter,
        net=net,
        include_items=include_items,
        summary_only=summary_only,
    )
    if refill is not None:
        result["refill"] = refill.get("result") if refill.get("ok") else refill
    if ctx is not None:
        ctx.report_progress(100, 100, "DRC complete")
    return result


@mcp.tool(annotations=READ_ONLY)
def export_gerbers(
    pcb_path: str,
    output_dir: str | None = None,
    layers: str | None = None,
    profile: str | None = None,
    board_plot_params: bool = False,
    check_zones: bool = False,
) -> dict:
    """Export Gerber files from a PCB.

    layers is a comma-separated list. profile='standard' selects the
    manufacturing layer set (no Adhesive/Courtyard/Fab/Margin/User layers).
    layers=None with profile=None keeps kicad-cli's default (all layers).
    """
    return kicad_cli.export_gerbers(
        _resolve(pcb_path),
        output_dir,
        layers,
        profile=profile,
        board_plot_params=board_plot_params,
        check_zones=check_zones,
    )


@mcp.tool(annotations=READ_ONLY)
def export_drill(
    pcb_path: str,
    output_dir: str | None = None,
    generate_map: bool = False,
    map_format: str = "gerber",
    separate_th: bool = False,
    generate_report: bool = False,
    report_path: str | None = None,
) -> dict:
    """Export Excellon drill files from a PCB."""
    return kicad_cli.export_drill(
        _resolve(pcb_path),
        output_dir,
        generate_map,
        map_format=map_format,
        separate_th=separate_th,
        generate_report=generate_report,
        report_path=report_path,
    )


@mcp.tool(annotations=READ_ONLY)
def export_bom(
    schematic_path: str,
    output_path: str | None = None,
    exclude_power: bool = True,
    exclude_dnp: bool = False,
    fields: str | None = None,
    labels: str | None = None,
    group_by: str = "Value",
) -> dict:
    """Export a grouped CSV BOM.

    PWR_FLAG symbols are removed by default. exclude_dnp=True also removes
    DNP rows. fields/labels allow MPN and supplier columns.
    """
    return kicad_cli.export_bom(
        _resolve(schematic_path),
        output_path,
        exclude_power=exclude_power,
        exclude_dnp=exclude_dnp,
        fields=fields,
        labels=labels,
        group_by=group_by,
    )


@mcp.tool(annotations=READ_ONLY)
def export_step(
    pcb_path: str,
    output_path: str | None = None,
    subst_models: bool = True,
    no_dnp: bool = False,
    include_tracks: bool = False,
    include_zones: bool = False,
) -> dict:
    """Export a STEP model from a PCB (kicad-cli pcb export step)."""
    return kicad_cli.export_step(
        _resolve(pcb_path),
        output_path,
        subst_models=subst_models,
        no_dnp=no_dnp,
        include_tracks=include_tracks,
        include_zones=include_zones,
    )


@mcp.tool(annotations=READ_ONLY)
def export_pdf(
    source_path: str,
    output_path: str | None = None,
    layers: str | None = None,
    mode: str = "single",
    black_and_white: bool = False,
) -> dict:
    """Export a PCB or schematic PDF. The source suffix selects the command."""
    return kicad_cli.export_pdf(
        _resolve(source_path),
        output_path,
        layers=layers,
        mode=mode,
        black_and_white=black_and_white,
    )


@mcp.tool(annotations=WRITE)
def bom_enrich(
    schematic_path: str,
    output_path: str | None = None,
    database_path: str | None = None,
    exclude_power: bool = True,
    exclude_dnp: bool = False,
) -> dict:
    """Export an enriched BOM with MPN/Manufacturer/Supplier/Stock/Alternate.

    database_path may be a CSV or JSON keyed by MPN or Value. Missing MPNs and
    duplicate MPNs are reported.
    """
    return bom.bom_enrich(
        _resolve(schematic_path),
        output_path,
        database_path=database_path,
        exclude_power=exclude_power,
        exclude_dnp=exclude_dnp,
    )


@mcp.tool(annotations=READ_ONLY)
def bom_validate_mpn(bom_path: str) -> dict:
    """Validate that a BOM has an MPN column with no missing or duplicate MPNs."""
    return bom.bom_validate_mpn(_resolve(bom_path))


@mcp.tool(annotations=WRITE)
def manufacturing_profile(
    pcb_path: str,
    schematic_path: str | None = None,
    output_dir: str | None = None,
    profile: str = "generic",
    layers: list[str] | None = None,
    include_step: bool = True,
    include_pdf: bool = True,
    include_bom: bool = True,
    refill_first: bool = False,
    ctx: Context | None = None,
) -> dict:
    """Export Gerbers/drill/POS/BOM/STEP/PDF and a SHA-256 manifest.

    The standard layer set excludes Adhesive/Courtyard/Fab/Margin/User.
    """
    if ctx is not None:
        ctx.report_progress(0, 100, "exporting manufacturing files")
    result = manufacturing.manufacturing_profile(
        _resolve(pcb_path),
        schematic_path=_resolve(schematic_path) if schematic_path else None,
        output_dir=output_dir,
        profile=profile,
        layers=layers,
        include_step=include_step,
        include_pdf=include_pdf,
        include_bom=include_bom,
        refill_first=refill_first,
    )
    if ctx is not None:
        ctx.report_progress(100, 100, "manufacturing profile complete")
    return result


@mcp.tool(annotations=READ_ONLY)
def verify_manufacturing_output(output_dir: str, manifest_path: str | None = None) -> dict:
    """Verify a manufacturing manifest: missing files, checksum mismatches, extras."""
    return manufacturing.verify_manufacturing_output(_resolve(output_dir), manifest_path)


@mcp.tool(annotations=READ_ONLY)
def doctor() -> dict:
    """Check kicad-cli, pcbnew, IPC, libraries, temp-dir writes and S-expression round-trip."""
    return diagnostics.doctor()


@mcp.tool
def self_test(ctx: Context | None = None) -> dict:
    """Run a small end-to-end test on a temporary copy of the KiCad demo project."""
    if ctx is not None:
        ctx.report_progress(0, 100, "starting self test")
    result = diagnostics.self_test()
    if ctx is not None:
        ctx.report_progress(100, 100, "self test complete")
    return result


@mcp.tool
def export_pos(pcb_path: str, output_path: str | None = None) -> dict:
    """Export component placement (pick & place) CSV from a PCB."""
    return kicad_cli.export_pos(_resolve(pcb_path), output_path)


@mcp.tool
def render_pcb(pcb_path: str, output_png: str | None = None, width: int = 1280, height: int = 720) -> dict:
    """Render a PCB to a PNG image (3D view via kicad-cli)."""
    return kicad_cli.render_pcb(_resolve(pcb_path), output_png, width, height)


@mcp.tool(
    annotations=READ_ONLY,
    output_schema=_object_schema(
        {
            "file": {"type": "string"},
            "revision": _REVISION_SCHEMA,
            "board_thickness_mm": {"type": ["number", "null"]},
            "stackup": {"type": ["array", "null"], "items": {"type": "object"}},
            "netclasses": _ARRAY_OBJECT,
            "layers": {"type": "array", "items": {"type": "string"}},
            "copper_layers": {"type": "integer"},
            "nets": {"type": "array", "items": {"type": "string"}},
            "net_count": {"type": "integer"},
            "unconnected_pads": _ARRAY_OBJECT,
            "unconnected_pad_count": {"type": "integer"},
            "stats": {"type": "object", "additionalProperties": True},
            "footprints": _ARRAY_OBJECT,
            "board_size": {"type": ["object", "null"], "additionalProperties": True},
        }
    ),
)
def get_board_info(
    pcb_path: str,
    include_schematic_nets: bool = False,
    refresh_netlist: bool = False,
) -> dict:
    """Inspect a PCB file: nets, thickness, stackup, netclasses, footprints, counts and board size.

    KiCad 10 stores net names on pads/tracks/vias/zones; the parser handles
    both that form and the KiCad 7-9 root net table. include_schematic_nets
    merges the matching schematic netlist (52 nets in the v5 project) and
    reports the unconnected pseudo-nets.
    """
    return board_parser.read_board(
        _resolve(pcb_path),
        include_schematic_nets=include_schematic_nets,
        refresh_netlist=refresh_netlist,
    )


@mcp.tool(annotations=READ_ONLY)
def mechanical_check(pcb_path: str, min_edge_clearance_mm: float = 0.5) -> dict:
    """Check courtyards, 3D model presence, edge clearance and connector access.

    Connector access is a position/edge heuristic, not a 3D clearance simulation.
    """
    return mechanical.mechanical_check(_resolve(pcb_path), min_edge_clearance_mm)


@mcp.tool(annotations=READ_ONLY)
def check_courtyard(pcb_path: str) -> dict:
    """Detect courtyard overlaps, missing courtyards and courtyards outside the board."""
    return mechanical.check_courtyard(_resolve(pcb_path))


@mcp.tool(annotations=READ_ONLY)
def check_3d_models(pcb_path: str) -> dict:
    """List missing 3D model files and footprints without a model."""
    return mechanical.check_3d_models(_resolve(pcb_path))


@mcp.tool(annotations=READ_ONLY)
def board_edge_clearance(pcb_path: str, min_mm: float = 0.5) -> dict:
    """Report pads/tracks/vias/courtyards within min_mm of the board edge."""
    return mechanical.board_edge_clearance(_resolve(pcb_path), min_mm)


@mcp.tool(annotations=READ_ONLY)
def pcb_revision(pcb_path: str) -> dict:
    """Return mtime/size/SHA-256 revision for a PCB (use as expected_revision)."""
    return pcb_edit.pcb_revision(_resolve(pcb_path))


@mcp.tool(
    annotations=WRITE,
    output_schema=_object_schema(
        {
            "ok": {"type": "boolean"},
            "dry_run": {"type": "boolean"},
            "result": {"type": "object", "additionalProperties": True},
            "backup": {"type": ["string", "null"]},
            "revision": _REVISION_SCHEMA,
        }
    ),
)
def pcb_batch(
    pcb_path: str,
    operations: list[dict],
    dry_run: bool = False,
    backup: bool = True,
    expected_revision: str | None = None,
    refill: bool = False,
    confirm: bool = False,
    ctx: Context | None = None,
) -> dict:
    """Apply PCB operations in one transaction.

    Supported kinds: add_tracks, add_via, delete_tracks, trim_track,
    set_pad_net, set_footprint_value, replace_footprint, place_footprint,
    set_zone_connection, add_zone, add_keepout, set_zone_priority,
    remove_islands, delete_zone, refill. The board is edited on a local
    staging copy and saved once; any failure leaves the original untouched.
    dry_run returns the planned result without writing. In safe mode,
    confirm=True is required unless dry_run is used.
    """
    if not dry_run:
        _require_confirm(confirm, "pcb_batch")
    if ctx is not None:
        ctx.report_progress(0, 100, "applying PCB batch")
    result = pcb_edit.pcb_batch(
        _resolve(pcb_path),
        operations,
        dry_run=dry_run,
        backup=backup,
        expected_revision=expected_revision,
        refill=refill,
    )
    if ctx is not None:
        ctx.report_progress(100, 100, "PCB batch complete")
    return result


@mcp.tool(annotations=WRITE)
def set_pad_net(
    pcb_path: str,
    ref: str,
    pad: str,
    net: str,
    backup: bool = True,
    dry_run: bool = False,
    expected_revision: str | None = None,
) -> dict:
    """Assign a pad to a net by footprint reference and pad number."""
    return pcb_edit.set_pad_net(
        _resolve(pcb_path), ref, pad, net,
        backup=backup, dry_run=dry_run, expected_revision=expected_revision,
    )


@mcp.tool(annotations=WRITE)
def set_footprint_value(
    pcb_path: str,
    ref: str,
    value: str,
    backup: bool = True,
    dry_run: bool = False,
    expected_revision: str | None = None,
) -> dict:
    """Set a footprint's Value field."""
    return pcb_edit.set_footprint_value(
        _resolve(pcb_path), ref, value,
        backup=backup, dry_run=dry_run, expected_revision=expected_revision,
    )


@mcp.tool(annotations=WRITE)
def replace_footprint(
    pcb_path: str,
    ref: str,
    footprint: str,
    keep_value: bool = True,
    transfer_nets: bool = True,
    value: str | None = None,
    backup: bool = True,
    dry_run: bool = False,
    expected_revision: str | None = None,
) -> dict:
    """Replace a footprint, preserving position/orientation and pad nets by number.

    footprint accepts 'Library:Footprint' or a path to a .kicad_mod file.
    """
    return pcb_edit.replace_footprint(
        _resolve(pcb_path), ref, footprint,
        keep_value=keep_value, transfer_nets=transfer_nets, value=value,
        backup=backup, dry_run=dry_run, expected_revision=expected_revision,
    )


@mcp.tool(annotations=WRITE)
def place_footprint(
    pcb_path: str,
    footprint: str,
    reference: str,
    value: str,
    x_mm: float,
    y_mm: float,
    layer: str = "F.Cu",
    rotation: float = 0.0,
    pad_nets: dict | None = None,
    backup: bool = True,
    dry_run: bool = False,
    expected_revision: str | None = None,
) -> dict:
    """Place a footprint from a KiCad library onto a PCB."""
    return pcb_edit.place_footprint(
        _resolve(pcb_path), footprint, reference, value, x_mm, y_mm,
        layer=layer, rotation=rotation, pad_nets=pad_nets,
        backup=backup, dry_run=dry_run, expected_revision=expected_revision,
    )


@mcp.tool(annotations=WRITE)
def set_zone_connection(
    pcb_path: str,
    mode: str,
    ref: str | None = None,
    pad: str | None = None,
    zone_index: int | None = None,
    backup: bool = True,
    dry_run: bool = False,
    expected_revision: str | None = None,
) -> dict:
    """Set a pad's local zone connection or a zone's pad connection.

    mode: full, thermal, none, tht_thermal, inherited.
    """
    return pcb_edit.set_zone_connection(
        _resolve(pcb_path), mode, ref=ref, pad=pad, zone_index=zone_index,
        backup=backup, dry_run=dry_run, expected_revision=expected_revision,
    )


@mcp.tool(annotations=WRITE)
def add_zone(
    pcb_path: str,
    net: str,
    layers: list[str],
    points: list[list[float]],
    priority: int = 0,
    clearance: float = 0.2,
    min_thickness: float = 0.25,
    fill_mode: str = "polygons",
    pad_connection: str = "thermal",
    island_removal: str = "always",
    min_island_area_mm2: float = 1.0,
    refill: bool = True,
    backup: bool = True,
    dry_run: bool = False,
    expected_revision: str | None = None,
) -> dict:
    """Add a copper zone from a polygon (points in mm) and optionally refill."""
    return pcb_edit.add_zone(
        _resolve(pcb_path), net, layers, points,
        priority=priority, clearance=clearance, min_thickness=min_thickness,
        fill_mode=fill_mode, pad_connection=pad_connection,
        island_removal=island_removal, min_island_area_mm2=min_island_area_mm2,
        refill=refill, backup=backup, dry_run=dry_run, expected_revision=expected_revision,
    )


@mcp.tool(annotations=WRITE)
def add_keepout(
    pcb_path: str,
    layers: list[str],
    points: list[list[float]],
    tracks: bool = True,
    vias: bool = True,
    pads: bool = True,
    zone_fills: bool = True,
    footprints: bool = False,
    backup: bool = True,
    dry_run: bool = False,
    expected_revision: str | None = None,
) -> dict:
    """Add a rule-area keepout from a polygon (points in mm)."""
    return pcb_edit.add_keepout(
        _resolve(pcb_path), layers, points,
        tracks=tracks, vias=vias, pads=pads, zone_fills=zone_fills,
        footprints=footprints, backup=backup, dry_run=dry_run,
        expected_revision=expected_revision,
    )


@mcp.tool(annotations=WRITE)
def set_zone_priority(
    pcb_path: str,
    zone_index: int,
    priority: int,
    backup: bool = True,
    dry_run: bool = False,
    expected_revision: str | None = None,
) -> dict:
    """Set a zone's assigned priority."""
    return pcb_edit.set_zone_priority(
        _resolve(pcb_path), zone_index, priority,
        backup=backup, dry_run=dry_run, expected_revision=expected_revision,
    )


@mcp.tool(annotations=WRITE)
def remove_islands(
    pcb_path: str,
    net: str | None = None,
    mode: str = "always",
    min_area_mm2: float = 1.0,
    backup: bool = True,
    dry_run: bool = False,
    expected_revision: str | None = None,
    confirm: bool = False,
) -> dict:
    """Set island removal on matching zones and refill the board."""
    if not dry_run:
        _require_confirm(confirm, "remove_islands")
    return pcb_edit.remove_islands(
        _resolve(pcb_path), net=net, mode=mode, min_area_mm2=min_area_mm2,
        backup=backup, dry_run=dry_run, expected_revision=expected_revision,
    )


@mcp.tool(annotations=WRITE)
def delete_zone(
    pcb_path: str,
    zone_index: int,
    backup: bool = True,
    dry_run: bool = False,
    expected_revision: str | None = None,
    confirm: bool = False,
) -> dict:
    """Delete a zone by index."""
    if not dry_run:
        _require_confirm(confirm, "delete_zone")
    return pcb_edit.delete_zone(
        _resolve(pcb_path), zone_index,
        backup=backup, dry_run=dry_run, expected_revision=expected_revision,
    )


@mcp.tool(annotations=WRITE)
def add_tracks(
    pcb_path: str,
    points: list[list[float]],
    layer: str,
    width: float,
    net: str,
    backup: bool = True,
    expected_revision: str | None = None,
) -> dict:
    """Add track segments along a polyline to a PCB. points=[[x,y],...] in mm (at least 2 points), layer 'F.Cu' or 'B.Cu', width in mm, net by name. Saves in place with a timestamped .bak backup (default). Run refill_zones afterwards."""
    pcb = _resolve(pcb_path)
    if expected_revision and not paths.revision_matches(pcb, expected_revision):
        raise RuntimeError("revision mismatch: pcb changed since it was read")
    return pcb_edit.add_tracks(pcb, points, layer, width, net, backup=backup)


@mcp.tool(annotations=WRITE)
def add_via(
    pcb_path: str,
    x: float,
    y: float,
    net: str,
    diameter: float = 0.6,
    drill: float = 0.3,
    backup: bool = True,
    expected_revision: str | None = None,
) -> dict:
    """Add a through via at (x, y) mm with the given net (default 0.6 mm / 0.3 mm drill). Saves in place with a timestamped .bak backup (default)."""
    pcb = _resolve(pcb_path)
    if expected_revision and not paths.revision_matches(pcb, expected_revision):
        raise RuntimeError("revision mismatch: pcb changed since it was read")
    return pcb_edit.add_via(pcb, x, y, net, diameter=diameter, drill=drill, backup=backup)


@mcp.tool(annotations=WRITE)
def refill_zones(
    pcb_path: str,
    backup: bool = True,
    expected_revision: str | None = None,
) -> dict:
    """Re-fill all copper zones (ZONE_FILLER) and save. Run after any routing change and before DRC/exports. Writes a timestamped .bak backup by default."""
    pcb = _resolve(pcb_path)
    if expected_revision and not paths.revision_matches(pcb, expected_revision):
        raise RuntimeError("revision mismatch: pcb changed since it was read")
    return pcb_edit.refill_zones(pcb, backup=backup)


@mcp.tool
def find_isolated_islands(pcb_path: str, net: str = "GND") -> dict:
    """Analyze zone fill connectivity for a net: groups fill islands together with same-net pads/vias/tracks they touch and reports groups isolated from the main plane. Read-only; zone fills must be current (run refill_zones after routing changes)."""
    return pcb_edit.find_isolated_islands(_resolve(pcb_path), net)


@mcp.tool
def find_duplicate_tracks(pcb_path: str, net: str | None = None, max_gap_mm: float = 1.0,
                          min_overlap_mm: float = 2.0) -> dict:
    """Detect duplicate and redundant track geometry: exact duplicate segments, duplicate vias, collinear overlapping segments, parallel same-net pairs (gap <= max_gap_mm, projected overlap >= min_overlap_mm; classified loop vs series), zero-length segments, dangling track ends, single-layer/isolated vias. Read-only; run refill_zones first so zone fills are current."""
    return pcb_edit.find_duplicate_tracks(_resolve(pcb_path), net, max_gap_mm=max_gap_mm, min_overlap_mm=min_overlap_mm)


@mcp.tool(annotations=WRITE)
def delete_tracks(
    pcb_path: str,
    uuids: list[str],
    backup: bool = True,
    expected_revision: str | None = None,
    confirm: bool = False,
) -> dict:
    """Delete track and via items by UUID (batch). Saves in place with a timestamped .bak backup by default."""
    _require_confirm(confirm, "delete_tracks")
    pcb = _resolve(pcb_path)
    if expected_revision and not paths.revision_matches(pcb, expected_revision):
        raise RuntimeError("revision mismatch: pcb changed since it was read")
    return pcb_edit.delete_tracks(pcb, uuids, backup=backup)


@mcp.tool(annotations=WRITE)
def trim_track(
    pcb_path: str,
    uuid: str,
    from_mm: list[float],
    to_mm: list[float],
    backup: bool = True,
    expected_revision: str | None = None,
) -> dict:
    """Move the track endpoint nearest to from_mm to to_mm (mm), keeping the other end. Saves in place with a timestamped .bak backup by default."""
    pcb = _resolve(pcb_path)
    if expected_revision and not paths.revision_matches(pcb, expected_revision):
        raise RuntimeError("revision mismatch: pcb changed since it was read")
    return pcb_edit.trim_track(pcb, uuid, from_mm, to_mm, backup=backup)


@mcp.tool(annotations=WRITE)
def prune_dangling(
    pcb_path: str,
    backup: bool = True,
    protect_pads: bool = True,
    max_iterations: int = 200,
    expected_revision: str | None = None,
    confirm: bool = False,
) -> dict:
    """Remove dangling track segments (KiCad connectivity) one at a time until none remain. protect_pads (default) skips tracks connected to a pad so a pad's only connection is never removed. Saves in place with a timestamped .bak backup by default."""
    _require_confirm(confirm, "prune_dangling")
    pcb = _resolve(pcb_path)
    if expected_revision and not paths.revision_matches(pcb, expected_revision):
        raise RuntimeError("revision mismatch: pcb changed since it was read")
    return pcb_edit.prune_dangling(pcb, backup=backup, protect_pads=protect_pads, max_iterations=max_iterations)


@mcp.tool
def edit_begin(pcb_path: str) -> dict:
    """Copy a PCB to a fast local staging file for editing (useful for slow or network paths). Edit the staging copy, then apply it with edit_commit."""
    return pcb_edit.edit_begin(_resolve(pcb_path))


@mcp.tool(annotations=WRITE)
def edit_commit(
    staging_path: str,
    target_path: str | None = None,
    backup: bool = True,
    expected_revision: str | None = None,
    confirm: bool = False,
) -> dict:
    """Atomically copy an edited staging PCB back over its source/target with an optional backup."""
    _require_confirm(confirm, "edit_commit")
    return pcb_edit.edit_commit(
        staging_path,
        target_path,
        backup=backup,
        expected_revision=expected_revision,
    )


@mcp.tool
def ipc_open_board(pcb_path: str, timeout_seconds: float = 30.0) -> dict:
    """Open a PCB in the KiCad PCB editor (launches pcbnew.exe) and wait until the IPC document is available. Use before live IPC tools when no board is open."""
    return ipc.open_board(str(_resolve(pcb_path)), timeout_seconds=timeout_seconds)


@mcp.tool(annotations=READ_ONLY)
def ipc_board_info(timeout_seconds: float = 3.0) -> dict:
    """Live board summary from the open KiCad GUI via IPC: project, title block, layers and item counts. Requires KiCad GUI with the API server enabled and a PCB open."""
    return ipc.board_info(timeout_seconds)


@mcp.tool(annotations=READ_ONLY)
def ipc_get_schematic(timeout_seconds: float = 3.0) -> dict:
    """Live schematic summary from the open KiCad GUI via IPC."""
    return ipc.get_schematic(timeout_seconds)


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


@mcp.tool(annotations=WRITE)
def ipc_batch(
    operations: list[dict],
    dry_run: bool = False,
    message: str = "kicad-mcp batch",
    timeout_seconds: float = 10.0,
    confirm: bool = False,
) -> dict:
    """Apply live-board IPC operations as one undo step.

    Supported kinds: add_tracks, add_via, delete_items, set_pad_net,
    set_footprint_value. dry_run opens and drops the commit without changes.
    """
    if not dry_run:
        _require_confirm(confirm, "ipc_batch")
    return ipc.batch(
        operations,
        dry_run=dry_run,
        message=message,
        timeout_seconds=timeout_seconds,
    )


@mcp.tool(annotations=WRITE)
def ipc_delete_items(
    ids: list[str],
    timeout_seconds: float = 5.0,
    confirm: bool = False,
) -> dict:
    """Delete board items by their IPC IDs (from ipc_list_items or ipc_get_selection) on the open KiCad board."""
    _require_confirm(confirm, "ipc_delete_items")
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


@mcp.resource("kicad://board")
def live_board_resource() -> str:
    """Live board summary from the open KiCad GUI (IPC)."""
    try:
        return json.dumps(ipc.board_info(), ensure_ascii=False)
    except Exception as exc:
        return json.dumps(
            {"connected": False, "error": f"{type(exc).__name__}: {exc}"},
            ensure_ascii=False,
        )


@mcp.resource("kicad://schematic")
def live_schematic_resource() -> str:
    """Live schematic summary from the open KiCad GUI (IPC)."""
    try:
        return json.dumps(ipc.get_schematic(), ensure_ascii=False)
    except Exception as exc:
        return json.dumps(
            {"connected": False, "error": f"{type(exc).__name__}: {exc}"},
            ensure_ascii=False,
        )


@mcp.resource(
    "kicad://schematic-file/{path*}",
    security=ResourceSecurity(exempt_params={"path"}),
)
def schematic_resource(path: str) -> str:
    """Read-only schematic JSON for the given path."""
    return json.dumps(sch_parser.read_schematic(_resolve(path)), ensure_ascii=False)


@mcp.resource(
    "kicad://project/{path*}",
    security=ResourceSecurity(exempt_params={"path"}),
)
def project_resource(path: str) -> str:
    """PCB plus merged schematic net inventory for the given PCB path."""
    return json.dumps(
        board_parser.read_board(_resolve(path), include_schematic_nets=True),
        ensure_ascii=False,
    )


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
