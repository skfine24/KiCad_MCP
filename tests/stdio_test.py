import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from paths import PCB, SCH  # noqa: E402

PYTHON = sys.executable


def send(proc, payload: dict) -> None:
    line = json.dumps(payload) + "\n"
    proc.stdin.write(line)
    proc.stdin.flush()


def recv(proc) -> dict:
    line = proc.stdout.readline()
    if not line:
        raise RuntimeError("no response from server")
    return json.loads(line)


def main() -> int:
    proc = subprocess.Popen(
        [PYTHON, "-m", "kicad_mcp"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    try:
        send(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "stdio-test", "version": "0.0.1"},
                },
            },
        )
        init = recv(proc)
        assert init["id"] == 1, init
        server_name = init["result"]["serverInfo"]["name"]
        print(f"[PASS] initialize -> server: {server_name}")

        send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        send(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tools = recv(proc)
        names = sorted(t["name"] for t in tools["result"]["tools"])
        expected = {
            "create_project",
            "list_projects",
            "read_schematic",
            "list_components",
            "export_netlist",
            "list_nets",
            "trace_net",
            "run_erc",
            "run_drc",
            "export_gerbers",
            "export_drill",
            "export_bom",
            "export_pos",
            "render_pcb",
            "get_board_info",
            "get_kicad_version",
            "add_tracks",
            "add_via",
            "refill_zones",
            "find_isolated_islands",
            "find_duplicate_tracks",
            "delete_tracks",
            "trim_track",
            "prune_dangling",
            "ipc_open_board",
            "ipc_board_info",
            "ipc_get_schematic",
            "ipc_batch",
            "ipc_list_nets",
            "ipc_list_items",
            "ipc_get_selection",
            "ipc_items_at_point",
            "ipc_add_track",
            "ipc_add_via",
            "ipc_delete_items",
            "ipc_save",
            "ipc_refill_zones",
            "ipc_set_active_layer",
            "edit_begin",
            "edit_commit",
            "pcb_revision",
            "pcb_batch",
            "set_pad_net",
            "set_footprint_value",
            "replace_footprint",
            "place_footprint",
            "set_zone_connection",
            "add_zone",
            "add_keepout",
            "set_zone_priority",
            "remove_islands",
            "delete_zone",
            "sch_revision",
            "sch_add_wire",
            "sch_add_label",
            "sch_add_no_connect",
            "sch_set_value",
            "sch_set_property",
            "sch_add_symbol",
            "sch_delete_item",
            "search_symbols",
            "search_footprints",
            "get_pinout",
            "get_footprint_info",
            "list_libraries",
            "mechanical_check",
            "check_courtyard",
            "check_3d_models",
            "board_edge_clearance",
            "export_step",
            "export_pdf",
            "bom_enrich",
            "bom_validate_mpn",
            "manufacturing_profile",
            "verify_manufacturing_output",
            "doctor",
            "self_test",
        }
        missing = expected - set(names)
        assert not missing, f"missing tools: {missing}"
        print(f"[PASS] tools/list -> {len(names)} tools: {', '.join(names)}")

        send(proc, {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "get_kicad_version", "arguments": {}}})
        ver = recv(proc)
        text = ver["result"]["content"][0]["text"]
        assert "10.0" in text, text
        print(f"[PASS] tools/call get_kicad_version -> {text[:120]}")

        send(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {
                    "name": "read_schematic",
                    "arguments": {"schematic_path": str(SCH)},
                },
            },
        )
        sch = recv(proc)
        assert sch["id"] == 4, sch
        print("[PASS] tools/call read_schematic -> OK")

        send(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {
                    "name": "find_duplicate_tracks",
                    "arguments": {"pcb_path": str(PCB)},
                },
            },
        )
        dup = recv(proc)
        assert dup["id"] == 5, dup
        dup_text = dup["result"]["content"][0]["text"]
        assert "summary" in dup_text, dup_text[:300]
        print("[PASS] tools/call find_duplicate_tracks -> OK")

        print("\nALL MCP PROTOCOL TESTS PASSED")
        return 0
    finally:
        proc.terminate()
        err = proc.stderr.read()
        if err.strip():
            print("--- server stderr ---")
            print(err[:1000])


if __name__ == "__main__":
    sys.exit(main())
