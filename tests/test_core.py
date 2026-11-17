import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).parent))

from kicad_mcp import (  # noqa: E402
    board_parser,
    bom,
    diagnostics,
    kicad_cli,
    library,
    manufacturing,
    mechanical,
    paths,
    pcb_edit,
    sch_edit,
    sch_parser,
    sexpr,
)
from paths import PCB, SCH, TEST_DIR  # noqa: E402


def _copy_pcb(name: str) -> Path:
    target = TEST_DIR / name
    shutil.copy2(PCB, target)
    return target


def _copy_sch(name: str) -> Path:
    target = TEST_DIR / name
    shutil.copy2(SCH, target)
    return target


def test_sexpr_roundtrip():
    tree = sexpr.parse('(kicad_sch (version 20260101) (generator "eeschema") (uuid "x"))')[0]
    dumped = sexpr.dump(tree)
    again = sexpr.parse(dumped)[0]
    assert again[0] == "kicad_sch"
    assert again[1][0] == "version"


def test_board_parser_kicad10_nets():
    data = board_parser.read_board(PCB)
    assert data["net_count"] > 0
    assert data["nets"]
    assert data["board_thickness_mm"] is not None
    assert data["stats"]["footprints"] > 0
    assert data["revision"]["sha256"]


def test_netlist_freshness():
    from kicad_mcp import server

    sch = _copy_sch("freshness.kicad_sch")
    first = server.list_nets(str(sch), refresh=True)
    assert first["exported"] is True
    assert first["net_count"] > 0
    second = server.list_nets(str(sch), refresh=False)
    assert second["exported"] is False
    sch_edit.sch_add_wire(sch, [[10, 10], [20, 10]], backup=False)
    third = server.list_nets(str(sch), refresh=False)
    assert third["stale"] is True
    assert third["exported"] is True


def test_drc_pagination():
    result = kicad_cli.run_drc(PCB, TEST_DIR / "drc-page", limit=2)
    assert "summary" in result
    assert result["page"]["limit"] == 2
    assert result["page"]["returned"] <= 2
    for violation in result["violations"]:
        assert "id" in violation
        assert "severity" in violation
        assert "type" in violation
        assert "nets" in violation
        assert "items" in violation


def test_pcb_batch_dry_run_and_edit():
    pcb = _copy_pcb("batch.kicad_pcb")
    data = board_parser.read_board(pcb)
    net = data["nets"][0]
    ref = data["footprints"][0]["reference"]
    before = data["stats"]
    dry = pcb_edit.pcb_batch(
        pcb,
        [
            {"kind": "add_tracks", "points": [[10, 10], [20, 10]], "layer": "F.Cu", "width": 0.25, "net": net},
            {"kind": "add_via", "x": 20, "y": 10, "net": net},
            {"kind": "set_footprint_value", "ref": ref, "value": "test-value"},
        ],
        dry_run=True,
    )
    assert dry["ok"] and dry["dry_run"]
    assert board_parser.read_board(pcb)["stats"] == before
    edit = pcb_edit.pcb_batch(
        pcb,
        [
            {"kind": "add_tracks", "points": [[10, 10], [20, 10]], "layer": "F.Cu", "width": 0.25, "net": net},
            {"kind": "add_via", "x": 20, "y": 10, "net": net},
        ],
    )
    assert edit["ok"] and not edit["dry_run"]
    after = board_parser.read_board(pcb)
    assert after["stats"]["tracks"] == before["tracks"] + 1
    assert after["stats"]["vias"] == before["vias"] + 1
    assert edit["backup"] and Path(edit["backup"]).is_file()


def test_pad_net_and_zone_tools():
    pcb = _copy_pcb("zone.kicad_pcb")
    data = board_parser.read_board(pcb)
    ref = data["footprints"][0]["reference"]
    pad = None
    for candidate in board_parser.sexpr.get_all(board_parser.sexpr.load_file(pcb), "footprint"):
        if board_parser._footprint_reference(candidate) == ref:
            pads = board_parser.sexpr.get_all(candidate, "pad")
            if pads:
                pad = str(pads[0][1])
                break
    if pad is None:
        pytest.skip("demo footprint has no pads")
    net = data["nets"][0]
    result = pcb_edit.set_pad_net(pcb, ref, pad, net, dry_run=True)
    assert result["ok"]
    zone = pcb_edit.add_zone(
        pcb,
        net,
        ["F.Cu"],
        [[1, 1], [10, 1], [10, 10], [1, 10]],
        dry_run=True,
    )
    assert zone["ok"]
    keepout = pcb_edit.add_keepout(
        pcb,
        ["F.Cu", "B.Cu"],
        [[2, 2], [5, 2], [5, 5], [2, 5]],
        dry_run=True,
    )
    assert keepout["ok"]


def test_sch_edit_tools():
    sch = _copy_sch("edit.kicad_sch")
    before = sch_parser.read_schematic(sch)
    wire = sch_edit.sch_add_wire(sch, [[10, 10], [20, 10], [20, 20]], backup=False)
    assert wire["segments"] == 2
    label = sch_edit.sch_add_label(sch, "MCP_TEST", 20, 20, kind="local", backup=False)
    assert label["uuid"]
    nc = sch_edit.sch_add_no_connect(sch, 30, 30, backup=False)
    assert nc["uuid"]
    ref = before["components"][0]["reference"]
    value = sch_edit.sch_set_value(sch, ref, "mcp-value", backup=False)
    assert value["value"] == "mcp-value"
    symbol = sch_edit.sch_add_symbol(sch, "Device:R", "R900", "10k", 50, 50, backup=False)
    assert symbol["ref"] == "R900"
    after = sch_parser.read_schematic(sch)
    assert after["stats"]["wires"] == before["stats"]["wires"] + 2
    assert after["stats"]["components"] == before["stats"]["components"] + 1
    netlist = kicad_cli.export_netlist(sch, TEST_DIR / "edit-netlist.net")
    assert netlist["returncode"] == 0
    assert Path(netlist["output"]).is_file()


def test_library_search_and_pinout():
    result = library.search_symbols("Device:R", limit=3)
    assert result["count"] >= 1
    pinout = library.get_pinout("Device:R")
    assert pinout["pin_count"] >= 2
    footprints = library.search_footprints("R_0402", limit=3)
    assert footprints["count"] >= 1


def test_mechanical_checks():
    courtyards = mechanical.check_courtyard(PCB)
    assert courtyards["ok"]
    models = mechanical.check_3d_models(PCB)
    assert models["ok"]
    edge = mechanical.board_edge_clearance(PCB, 0.5)
    assert edge["ok"]
    full = mechanical.mechanical_check(PCB, 0.5)
    assert full["ok"]
    assert "summary" in full


def test_manufacturing_profile_and_verify():
    out = TEST_DIR / "mfg-profile"
    shutil.rmtree(out, ignore_errors=True)
    result = manufacturing.manufacturing_profile(
        PCB,
        SCH,
        out,
        include_step=False,
        include_pdf=False,
    )
    assert result["ok"]
    assert Path(result["manifest_path"]).is_file()
    assert result["files"]
    assert not result["checks"]["missing_layers"]
    verify = manufacturing.verify_manufacturing_output(out)
    assert verify["ok"]
    assert verify["checked"] == len(result["files"])


def test_bom_enrich_and_validate():
    out = TEST_DIR / "bom-enriched.csv"
    result = bom.bom_enrich(SCH, out)
    assert result["row_count"] > 0
    assert Path(result["output"]).is_file()
    validate = bom.bom_validate_mpn(out)
    assert validate["row_count"] == result["row_count"]
    assert "missing_mpn_count" in validate


def test_paths_revision_backup_and_lock():
    target = TEST_DIR / "revision.txt"
    paths.atomic_write_text(target, "one")
    revision = paths.file_revision(target)
    assert revision["exists"] and revision["sha256"]
    assert paths.revision_matches(target, revision["sha256"])
    backup = paths.backup_file(target)
    assert backup and Path(backup).is_file()
    with paths.locked(target):
        assert (target.with_name(target.name + ".kicad-mcp.lock")).is_file()
    assert not (target.with_name(target.name + ".kicad-mcp.lock")).exists()


def test_doctor():
    result = diagnostics.doctor()
    checks = {check["name"]: check for check in result["checks"]}
    assert checks["kicad_cli"]["ok"]
    assert checks["temp_dir"]["ok"]
    assert checks["sexpr_roundtrip"]["ok"]
