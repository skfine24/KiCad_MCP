import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).parent))

from kicad_mcp import board_parser, kicad_cli, pcb_edit, sch_parser  # noqa: E402
from kicad_mcp import ipc  # noqa: E402
from paths import PCB, SCH, TEST_DIR  # noqa: E402

results = {}


def record(name, fn):
    try:
        out = fn()
        results[name] = {"ok": True, "summary": out}
        print(f"[PASS] {name}")
    except Exception as e:
        results[name] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        print(f"[FAIL] {name}: {type(e).__name__}: {e}")


def t_version():
    v = kicad_cli._version()
    assert "10" in v, v
    return v


def t_read_schematic():
    data = sch_parser.read_schematic(SCH)
    s = data["stats"]
    assert s["components"] > 0, s
    refs = [c["reference"] for c in data["components"]]
    assert "U2" in refs or any(r and r.startswith("U") for r in refs), refs[:5]
    return s


def t_list_components():
    comps = sch_parser.list_components(SCH, "pic")
    assert isinstance(comps, list)
    return f"filter 'pic' -> {len(comps)}"


def t_netlist_and_nets():
    out = kicad_cli.export_netlist(SCH, TEST_DIR / "test-netlist.net")
    assert Path(out["output"]).is_file(), out
    data = sch_parser.read_netlist(out["output"])
    assert data["nets"], "no nets"
    gnd = [n for n in data["nets"] if n["name"] and "GND" in n["name"].upper()]
    assert gnd, "GND net missing"
    return f"{len(data['nets'])} nets, {len(data['components'])} comps, GND pins: {len(gnd[0]['nodes'])}"


def t_erc():
    out = kicad_cli.run_erc(SCH, TEST_DIR)
    assert Path(out["report"]).is_file(), out
    return out.get("counts")


def t_drc():
    out = kicad_cli.run_drc(PCB, TEST_DIR)
    assert Path(out["report"]).is_file(), out
    for v in out["violations"]:
        assert "items" in v and isinstance(v["items"], list), v
    for u in out["unconnected_items"]:
        assert "description" in u and "items" in u, u
    return out.get("counts")


def t_board_info():
    data = board_parser.read_board(PCB)
    s = data["stats"]
    assert s["footprints"] > 0 and s["tracks"] > 0, s
    assert data["board_size"] and data["board_size"]["width_mm"] > 0
    return {**s, "size": data["board_size"], "copper_layers": data["copper_layers"]}


def t_gerbers():
    out = kicad_cli.export_gerbers(PCB, TEST_DIR / "out-gerbers")
    assert out["files"], out
    return f"{len(out['files'])} files"


def t_drill():
    out = kicad_cli.export_drill(PCB, TEST_DIR / "out-gerbers")
    assert out["files"], out
    assert all(f.endswith(".drl") for f in out["files"]), out["files"]
    return f"{len(out['files'])} written: {out['files']}"


def t_bom():
    out = kicad_cli.export_bom(SCH, TEST_DIR / "test-bom.csv")
    content = Path(out["output"]).read_text(encoding="utf-8")
    assert "Refs" in content or "Reference" in content, content[:200]
    return f"{len(content.splitlines())} lines"


def t_pos():
    out = kicad_cli.export_pos(PCB, TEST_DIR / "test-pos.csv")
    assert Path(out["output"]).is_file(), out
    return "csv created"


def t_render():
    out = kicad_cli.render_pcb(PCB, TEST_DIR / "test-render.png")
    assert out["exists"], out
    size = (TEST_DIR / "test-render.png").stat().st_size
    return f"png {size} bytes"


def t_create_project():
    proj_dir = TEST_DIR / "newproj"
    proj_dir.mkdir(exist_ok=True)
    stem = proj_dir / "blink"
    for f in ("blink.kicad_pro", "blink.kicad_sch", "blink.kicad_pcb"):
        p = proj_dir / f
        if p.exists():
            p.unlink()
    from kicad_mcp.server import create_project

    out = create_project(str(stem))
    for f in out["files"].values():
        assert Path(f).is_file(), f
    return json.dumps(out["files"])


def t_ipc():
    status = ipc.get_status()
    if status.get("connected") and status.get("open_board"):
        info = ipc.board_info()
        assert info.get("board") == status["open_board"], (status, info)
        nets = ipc.list_nets()
        assert nets["count"] >= 0, nets
        return {"status": status, "board": info["board"], "nets": nets["count"]}
    return {"status": status, "note": "no live board open; IPC board tools not exercised"}


def t_islands():
    out = pcb_edit.find_isolated_islands(PCB, "GND")
    assert out["ok"], out
    r = out["result"]
    assert "groups" in r and "isolated_group_count" in r, r
    return {k: r[k] for k in ("island_count", "group_count", "isolated_group_count")}


def t_duplicate_tracks():
    work = TEST_DIR / "dup-test.kicad_pcb"
    shutil.copy2(PCB, work)
    pcb_edit.add_tracks(work, [[10.0, 10.0], [20.0, 10.0]], "F.Cu", 0.25, "GND", backup=False)
    pcb_edit.add_tracks(work, [[20.0, 10.0], [10.0, 10.0]], "F.Cu", 0.25, "GND", backup=False)
    pcb_edit.add_tracks(work, [[10.0, 10.5], [20.0, 10.5]], "F.Cu", 0.25, "GND", backup=False)
    pcb_edit.add_via(work, 15.0, 15.0, "GND", backup=False)
    pcb_edit.add_via(work, 15.0, 15.0, "GND", backup=False)
    pcb_edit.add_via(work, 300.0, 300.0, "GND", backup=False)
    pcb_edit.add_via(work, 300.0, 310.0, "GND", backup=False)
    pcb_edit.add_tracks(work, [[295.0, 310.0], [300.0, 310.0]], "F.Cu", 0.25, "GND", backup=False)
    out = pcb_edit.find_duplicate_tracks(work)
    assert out["ok"], out
    s = out["result"]["summary"]
    assert s["exact_duplicate_groups"] >= 1, s
    assert s["parallel_pair_count"] >= 1, s
    assert s["duplicate_via_groups"] >= 1, s
    assert s["single_layer_via_count"] >= 1, s
    assert s["isolated_via_count"] >= 1, s
    for pair in out["result"]["parallel_pairs"]:
        assert pair.get("classification") in ("loop", "series", "unknown"), pair
    return s


def t_delete_trim():
    work = TEST_DIR / "delete-test.kicad_pcb"
    shutil.copy2(PCB, work)
    before = board_parser.read_board(work)["stats"]
    tr = pcb_edit.add_tracks(work, [[10.0, 10.0], [20.0, 10.0]], "F.Cu", 0.25, "GND", backup=False)
    assert tr["ok"] and tr["result"]["added_segments"] == 1, tr
    uuids = tr["result"]["uuids"]
    assert len(uuids) == 1, tr
    tm = pcb_edit.trim_track(work, uuids[0], [20.0, 10.0], [20.0, 12.0], backup=False)
    assert tm["ok"] and tm["result"]["trimmed"], tm
    dl = pcb_edit.delete_tracks(work, uuids, backup=False)
    assert dl["ok"] and dl["result"]["removed_count"] == 1, dl
    after = board_parser.read_board(work)["stats"]
    assert after["tracks"] == before["tracks"], (before, after)
    fl = pcb_edit.add_tracks(work, [[200.0, 200.0], [210.0, 200.0]], "F.Cu", 0.25, "GND", backup=False)
    assert fl["ok"], fl
    n0 = board_parser.read_board(work)["stats"]["tracks"]
    pr = pcb_edit.prune_dangling(work, backup=False, protect_pads=True)
    assert pr["ok"] and pr["result"]["pruned_count"] >= 1, pr
    n1 = board_parser.read_board(work)["stats"]["tracks"]
    assert n1 < n0, (n0, n1)
    return {"delete": True, "trim": True, "prune_removed": pr["result"]["pruned_count"]}


def t_add_edit_tools():
    work = TEST_DIR / "edit-test.kicad_pcb"
    shutil.copy2(PCB, work)
    before = board_parser.read_board(work)["stats"]
    tr = pcb_edit.add_tracks(work, [[10.0, 10.0], [12.0, 10.0], [12.0, 12.0]], "F.Cu", 0.25, "GND", backup=False)
    assert tr["ok"] and tr["result"]["added_segments"] == 2, tr
    after = board_parser.read_board(work)["stats"]
    assert after["tracks"] == before["tracks"] + 2, (before, after)
    vi = pcb_edit.add_via(work, 12.0, 12.0, "GND", backup=False)
    assert vi["ok"] and vi["result"]["via_added"], vi
    rf = pcb_edit.refill_zones(work, backup=False)
    assert rf["ok"], rf
    bak = pcb_edit._backup(work)
    assert Path(bak).is_file(), bak
    return {"tracks_added": 2, "via_added": True, "refill_ok": True}


def t_edit_begin_commit():
    staging = pcb_edit.edit_begin(PCB)["staging"]
    assert Path(staging).is_file()
    out = pcb_edit.edit_commit(staging)
    assert out["committed"] and not Path(staging).is_file(), out
    return out["target"]


record("version", t_version)
record("read_schematic", t_read_schematic)
record("list_components", t_list_components)
record("export_netlist+list_nets", t_netlist_and_nets)
record("run_erc", t_erc)
record("run_drc", t_drc)
record("islands", t_islands)
record("duplicate_tracks", t_duplicate_tracks)
record("delete_trim", t_delete_trim)
record("add_edit_tools", t_add_edit_tools)
record("edit_begin_commit", t_edit_begin_commit)
record("get_board_info", t_board_info)
record("export_gerbers", t_gerbers)
record("export_drill", t_drill)
record("export_bom", t_bom)
record("export_pos", t_pos)
record("render_pcb", t_render)
record("create_project", t_create_project)
record("ipc_status", t_ipc)

passed = sum(1 for r in results.values() if r["ok"])
print(f"\n{passed}/{len(results)} passed")
