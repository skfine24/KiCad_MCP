# KiCad_MCP

MCP (Model Context Protocol) server for KiCad 10, built on the official
`kicad-python` bindings and `kicad-cli`. It provides schematic analysis,
netlist connectivity, ERC/DRC validation, manufacturing exports, PCB
inspection/editing, and live IPC editing of the board open in KiCad.

Repository: https://github.com/skfine24/KiCad_MCP

## Requirements

- KiCad 10.0.x (tested with 10.0.8, API 10.0.6)
- Python 3.11+
- `uv`/`uvx` for the one-command install (or `pip`)
- For live IPC: KiCad GUI running with **Preferences > Plugins > Enable KiCad
  API server** on, and a PCB editor document open

## Quick start (opencode)

Add this to `~/.config/opencode/opencode.jsonc`:

```jsonc
{
  "mcp": {
    "kicad": {
      "type": "local",
      "command": [
        "uvx",
        "--from",
        "git+https://github.com/skfine24/KiCad_MCP.git@v0.5.2",
        "kicad-mcp"
      ],
      "enabled": true
    }
  }
}
```

Restart opencode. `uvx` builds the package from the tagged GitHub source and
caches the environment; no local clone or virtualenv is required.

To use the latest `main` instead of the tag, replace `@v0.5.2` with `@main`.

## Install as a tool

```powershell
uv tool install git+https://github.com/skfine24/KiCad_MCP.git@v0.5.2
kicad-mcp
```

## Local development

```powershell
git clone https://github.com/skfine24/KiCad_MCP.git
cd KiCad_MCP
uv venv
uv pip install -e ".[dev]"
```

Run the server directly over stdio:

```powershell
.\.venv\Scripts\python.exe -m kicad_mcp
```

## Tools

| Category | Tools |
|---|---|
| Project | `create_project`, `list_projects` |
| Schematic analysis | `read_schematic`, `list_components` |
| Connectivity | `export_netlist`, `list_nets`, `trace_net` (pin-level, netlist-based) |
| Validation | `run_erc`, `run_drc` (violations include per-item positions; optional `refill_first`), `find_duplicate_tracks` (exact/overlapping/parallel-redundant segments, duplicate vias, zero-length segments, dangling ends, single-layer/isolated vias; parallel pairs classified loop vs series) |
| Manufacturing | `export_gerbers`, `export_drill`, `export_bom`, `export_pos`, `render_pcb` (exports report only the files they wrote) |
| PCB inspection | `get_board_info`, `find_isolated_islands` (zone fill connectivity groups per net) |
| PCB editing | `add_tracks`, `add_via`, `delete_tracks`, `trim_track`, `prune_dangling`, `refill_zones` (save in place, timestamped `.bak` backup by default; `.bak` files are analyzed on a temp copy) |
| Staged editing | `edit_begin`, `edit_commit` (edit a fast local copy, then write back - useful for network paths) |
| Live IPC | `ipc_open_board` (launch `pcbnew.exe` and wait for the document), `ipc_board_info`, `ipc_list_nets`, `ipc_list_items`, `ipc_get_selection`, `ipc_items_at_point`, `ipc_add_track`, `ipc_add_via`, `ipc_delete_items`, `ipc_save`, `ipc_refill_zones`, `ipc_set_active_layer` (open KiCad board via IPC; requires KiCad GUI + API server enabled) |
| Status | `get_kicad_version` (kicad-cli version + live IPC status; reports transport connection and any document-level error separately) |

## Live IPC connection (optional)

`get_kicad_version` reports a live IPC connection when:

1. KiCad GUI is running
2. Preferences > Plugins > "Enable KiCad API server" is on
3. A PCB editor document is open

The IPC path enables live PCB inspection and editing of the board open in the
GUI. Without it, all file-based tools still work headless via `kicad-cli`.

The KiCad IPC API has no open-document command. `ipc_open_board` therefore
launches `pcbnew.exe` with the PCB path when no PCB editor is running, and waits
until the document appears in IPC. If `pcbnew.exe` is already running it does
not launch a second editor; open the board manually in that case.

Note: `kicad-cli api-server` (headless IPC) is not available in KiCad 10.0.6
stable; it exists in kipy for newer builds.

## Tests

```powershell
python tests/integration_test.py   # 19 tool-level checks against a real KiCad demo project
python tests/stdio_test.py          # MCP stdio protocol handshake + tool calls
```

The tests copy the KiCad `pic_programmer` demo project from
`C:\Program Files\KiCad\10.0\share\kicad\demos` into a temp directory. Override
`KICAD_DEMO_DIR` and `KICAD_MCP_TEST_DIR` if KiCad is installed elsewhere.

## Known limits

- Schematic editing has no official API in KiCad; this server is read-only for
  schematics by design
- `get_board_info` parses `.kicad_pcb` directly (fast); for design-rule-precise
  data prefer `run_drc`
- Live IPC tools require the KiCad GUI, the API server, and an open PCB document

## License

MIT
