# 2026-11-16: KiCad MCP server v0.5.2 (`ipc_open_board` launcher)

## Summary

Added `ipc_open_board`: the KiCad IPC API has no open-document command, so this
tool launches `pcbnew.exe` with the PCB path and waits until the board appears
in IPC. If `pcbnew.exe` is already running it does not launch a second editor;
it waits for the requested board or reports that the board must be opened
manually. Server now exposes 38 tools.

## Files Changed

- `src/kicad_mcp/ipc.py`: `_find_pcbnew`, `_pcbnew_running`, `open_board`
- `src/kicad_mcp/server.py`: `ipc_open_board` tool (38 total)
- `src/kicad_mcp/__init__.py`, `pyproject.toml`: version 0.5.2
- `tests/stdio_test.py`: expected tool set (38 tools)
- `README.md`: Live IPC tool row

## Reason

User asked whether opencode can open the PCB editor document. The IPC command
set (`GetOpenDocuments`, `SaveDocument`, `RevertDocument`, `RunAction`, ...)
has no open-document request, so a client cannot open a board through IPC. A
PCB editor must be started with the file; `pcbnew.exe <board>` does that and
starts the API server for the new instance.

## Root Cause

1. `kipy`/KiCad IPC only works with documents that are already open; no
   `OpenDocument`/`OpenBoard` command exists in `editor_commands_pb2`.
2. Launching `pcbnew.exe` while another editor is running can replace/duplicate
   the editor, so `open_board` checks `tasklist` and refuses to launch when
   `pcbnew.exe` is already running.

## Validation

- `ipc.open_board(...)` with the board already open -> `already_open:true`, no
  second `pcbnew.exe` process.
- Manual launch (`pcbnew.exe` with the fixed board) -> IPC `connected:true`,
  `open_board=airgo_gate_main.kicad_pcb`.
- Live board: 42 footprints, 674 tracks, 72 vias, 132 pads, 2 zones, 28 nets;
  `XSHUT3_DRIVE` 33 tracks, LO `25e91a86` (61.305,33.605)->(50.75,33.605),
  diagonal `64934534` (54.8,27.1)->(61.305,33.605), MID/ladder UUIDs absent.
- `tests/stdio_test.py`: 38 tools, protocol tests passed.
- `tests/integration_test.py`: 19/19 passed.

# 2026-11-16: KiCad MCP server v0.5.1 (IPC tool import + stdout JSON parse fixes)

## Summary

Fixed two defects found when exercising v0.5.0: `server.py` did not import
`ipc`, so all `ipc_*` tools raised `NameError: name 'ipc' is not defined`; and
`pcb_edit._run_script` parsed only the last stdout line, so SWIG warnings printed
after the JSON made `delete_tracks` report `ok:false` even though the edit had
applied.

## Files Changed

- `src/kicad_mcp/server.py`: import `ipc`
- `src/kicad_mcp/pcb_edit.py`: `_run_script` searches stdout lines for the JSON
  object instead of assuming the last line
- `src/kicad_mcp/__init__.py`, `pyproject.toml`: version 0.5.1

## Reason

The v0.5.0 IPC tools were registered but unusable; `delete_tracks` returned a
false failure on the airgo_gate board cleanup.

## Root Cause

1. The new IPC tools reference the module-level `ipc` name, but only
   `_ipc_status_safe()` imported it locally.
2. KiCad's SWIG bindings emit warnings on stdout after the script's JSON line.

## Validation

- `server.ipc_board_info()` -> `airgo_gate_main.kicad_pcb`, counts OK.
- `delete_tracks`/`trim_track` applied; `find_duplicate_tracks` 0 pairs / 0
  dangling; DRC 0 errors/warnings/unconnected.

# 2026-11-16: KiCad MCP server v0.5.0 (live IPC design tools)

## Summary

Fixed IPC status reporting and added live IPC tools for designing against the
open KiCad board: `ipc_board_info`, `ipc_list_nets`, `ipc_list_items`,
`ipc_get_selection`, `ipc_items_at_point`, `ipc_add_track`, `ipc_add_via`,
`ipc_delete_items`, `ipc_save`, `ipc_refill_zones`, `ipc_set_active_layer`.
Server now exposes 37 tools.

## Files Changed

- `src/kicad_mcp/ipc.py`: `get_status` reports transport connected + api_version +
  open_board with a separate `board_error` (`Board.name` instead of the removed
  `get_file_name`); new `board_info`, `list_nets`, `list_items`, `get_selection`,
  `items_at_point`, `add_track`, `add_via`, `delete_items`, `save_board`,
  `refill_zones_live`, `set_active_layer`
- `src/kicad_mcp/server.py`: 11 IPC tools registered (37 total)
- `src/kicad_mcp/__init__.py`, `pyproject.toml`: version 0.5.0
- `tests/integration_test.py`: `t_ipc` exercises `board_info`/`list_nets` when a
  live board is open (19 checks)
- `tests/stdio_test.py`: expected tool set (37 tools)
- `README.md`: IPC tool row, status note

## Reason

The KiCad API server was enabled but `get_kicad_version` reported
`connected:false` because kipy 0.8.0's `Board` has no `get_file_name()`; the
file-only MCP also could not inspect or edit the live design.

## Root Cause

1. `ipc.get_status` used `board.get_file_name()`, removed in kipy 0.8.0;
   `Board.name` is the file name.
2. Document commands (`GetOpenDocuments`) require an open PCB editor; the status
   collapsed that into `connected:false`.
3. No live-board tools existed for design inspection/editing.

## Validation

- `tests/integration_test.py`: 19/19 passed; `t_ipc` -> `connected:true`,
  `open_board=airgo_gate_main.kicad_pcb`, `board_info`/`list_nets` OK.
- `tests/stdio_test.py`: 37 tools; `get_kicad_version` reports `connected:true`,
  KiCad 10.0.8 / API 10.0.6, `open_board`.
- Live board: `board_info` (42 footprints, 679 tracks, 72 vias, 132 pads,
  2 zones, 28 nets), SCL tracks 91, `items_at_point` at (47.2575, 10.4275) ->
  track + pad; `add_track`/`add_via` -> 680/73, `delete_items` -> 679/72
  (content restored).

# 2026-11-16: KiCad MCP server v0.4.0 (PCB delete/trim/prune + stronger validation)

## Summary

Added PCB edit tools needed by the redundant-copper cleanup: `delete_tracks`
(batch, tracks and vias by UUID), `trim_track` (move the endpoint nearest to
`from_mm` to `to_mm`), and `prune_dangling` (iterative dangling-track removal,
pad-safe by default). Enhanced `find_duplicate_tracks`: parallel pairs are
classified loop vs series via biconnected components, and single-layer/isolated
vias are reported using `CONNECTIVITY_DATA.IsConnectedOnLayer`. `run_drc` gained
`refill_first` to refill zones before DRC so stale fills do not produce false
clearance errors. Server now exposes 26 tools.

## Files Changed

- `src/kicad_mcp/pcb_edit.py`: `add_tracks`/`add_via` return UUIDs;
  `_DELETE_TRACKS_SCRIPT`, `_TRIM_TRACK_SCRIPT`, `_PRUNE_ONE_SCRIPT`;
  `delete_tracks`, `trim_track`, `prune_dangling`; `find_duplicate_tracks`
  classification + single-layer/isolated vias
- `src/kicad_mcp/server.py`: 3 new tools, `run_drc(refill_first)`, updated docs
  (26 tools)
- `src/kicad_mcp/__init__.py`, `pyproject.toml`: version 0.4.0
- `tests/integration_test.py`: `t_delete_trim`; duplicate test asserts
  single-layer/isolated vias and pair classification (19 checks)
- `tests/stdio_test.py`: expected tool set (26 tools)
- `README.md`: tool table, test count

## Reason

The airgo_gate redundant-copper cleanup needed 17 segment + 2 via deletions and 6
endpoint trims; the MCP had no delete/trim tools, so one-off pcbnew scripts were
used. The cleanup also exposed false positives (series jogs flagged as parallel
pairs), single-layer vias only DRC caught, and a stale-zone-fill false clearance
error after trims.

## Root Cause

1. PCB editing was add-only; deletions/trims had no tool.
2. Parallel-pair detection is geometric only; series jogs overlap in projection.
3. Via connectivity on one layer was only checked by DRC.
4. DRC runs against the current fill; a trim can leave the fill stale.

## Validation

- `tests/integration_test.py`: 19/19 passed; delete/trim/prune round-trip on a
  demo copy, duplicate test covers exact/parallel/duplicate-via plus
  single-layer and isolated vias and pair classification.
- `tests/stdio_test.py`: 26 tools over real stdio; `find_duplicate_tracks` call OK.
- airgo_gate main board: `find_duplicate_tracks` -> 679 segments / 72 vias, 0
  duplicates/overlaps/dangling/single-layer/isolated; 1 parallel pair
  (XSHUT3_DRIVE) classified `loop` (left as-is: removal would orphan a branch).
- `run_drc(refill_first=True)` on a board copy: refill OK, 0 errors, 0
  unconnected (3 `lib_footprint_mismatch` warnings only because the copy is
  outside the project library context).

# 2026-11-16: KiCad MCP server v0.3.0 (duplicate/redundant track validation)

## Summary

Added `find_duplicate_tracks`: exact duplicate segments, duplicate vias,
collinear overlapping segments, parallel same-net pairs (perpendicular gap and
projected overlap), zero-length segments and dangling track ends (KiCad
connectivity). Server now exposes 23 tools. Motivated by a redundant SCL loop on
the airgo_gate main board that exact-duplicate and DRC checks missed.

## Files Changed

- `src/kicad_mcp/pcb_edit.py`: `_DUPLICATE_TRACKS_SCRIPT` + `find_duplicate_tracks`
- `src/kicad_mcp/server.py`: tool registered (23 total)
- `src/kicad_mcp/__init__.py`, `pyproject.toml`: version 0.3.0
- `tests/integration_test.py`: `t_duplicate_tracks` (18 checks)
- `tests/stdio_test.py`: expected tool set includes `find_duplicate_tracks`
- `README.md`: tool table, test count

## Reason

The airgo_gate main board routed SCL as two parallel F.Cu traces (0.635 mm
apart, 70.485 mm overlap) joined at both ends. The existing checks only looked
for identical endpoints/overlaps; DRC does not flag redundant same-net loops.

## Root Cause

Redundant autorouter loops are geometrically distinct (parallel, not
overlapping) and electrically connected at both ends, so neither DRC
(clearance/unconnected) nor exact-duplicate scans surface them. The new tool
compares parallel same-net segments by perpendicular gap and projected overlap,
and cross-checks dangling ends with KiCad's connectivity engine
(`TestTrackEndpointDangling`).

## Validation

- `tests/integration_test.py`: 18/18 passed; the duplicate test adds identical
  (reversed) segments, a 0.5 mm parallel pair and duplicate vias and asserts all
  are reported.
- `tests/stdio_test.py`: 23 tools over real stdio.
- airgo_gate main board (before fix): SCL pair gap 0.635 mm / overlap 70.485 mm;
  LED_DRIVE (B.Cu), NANO_5V, XSHUT4_DRIVE and XSHUT3_DRIVE (2 pairs) also
  reported; 2 dangling ends on XSHUT1_DRIVE/XSHUT2_DRIVE.
- airgo_gate main board (after SCL fix): SCL pair gone, segment/via counts
  694/74, DRC 0 errors/warnings/unconnected, connectivity 0 unconnected.
- airgo_gate main board (after full redundant-copper cleanup): 679 segments /
  72 vias, 0 duplicates/overlaps/dangling, 1 parallel pair (XSHUT3_DRIVE; later
  classified loop in v0.4.0), DRC 0 errors/warnings/unconnected, connectivity
  0 unconnected.

# 2026-11-16: KiCad MCP server v0.2.0 (PCB editing + connectivity analysis)

## Summary

Added PCB editing and copper-connectivity analysis tools to kicad-mcp, driven by
needs found while repairing the airgo_gate main board (isolated GND fill islands
on J1.2/A1.4 that DRC barely surfaced). Server now exposes 22 tools. Source and
venv moved to `Y:\Dev_APP\pc_app\mcp-cad` (C:\Dev_APP deprecated by user).

## Files Changed

- `src/kicad_mcp/pcb_edit.py` (new): add_tracks, add_via, refill_zones,
  find_isolated_islands, edit_begin, edit_commit; pcbnew scripts run via the
  KiCad bundled python with a params-JSON protocol (one JSON line on stdout)
- `src/kicad_mcp/kicad_cli.py`: `_parse_check_report` now includes violation and
  unconnected items with description, position (mm) and uuid; `export_gerbers` /
  `export_drill` return only newly written files via mtime diff plus
  `files_in_output_dir`; `run_drc` accepts `.bak` backups via a temp
  `.kicad_pcb` copy; `_require_file` accepts `.bak` for PCB tools
- `src/kicad_mcp/server.py`: 6 new tools registered (22 total)
- `src/kicad_mcp/__init__.py`, `pyproject.toml`: version 0.2.0
- `tests/integration_test.py`: islands, add_edit_tools, edit_begin_commit tests;
  t_drc asserts items structure; t_drill asserts only .drl files are reported
- `tests/stdio_test.py`: expected tool set extended (22 tools over real stdio)
- `README.md`: tool table, Y:\ paths, test counts
- `.venv/`: copied from C:\Dev_APP and retargeted
  (`__editable__.kicad_mcp-0.1.0.pth` -> `Y:\Dev_APP\pc_app\mcp-cad\src`)

## Reason

During the airgo_gate main-board repair (J1.2/A1.4 GND island defects) the
agent had to hand-write pcbnew scripts for every edit, DRC output lacked
positions, and exports returned stale directory listings. These tools encode
that workflow.

## Root Cause (issues found and fixed during build)

1. `pcbnew.LoadBoard()` returns None for any suffix other than `.kicad_pcb`
   (e.g. `.bak` backups) -> wrappers run such files on a temp `.kicad_pcb`
   copy; edit results are copied back on success.
2. `PCB_TRACK.GetWidth()` without a layer argument asserts on vias and hangs
   the process (wx assert dialog) -> vias use `GetWidth(pcbnew.F_Cu)`.
3. `zone.GetFilledPolysList()` asserts when the zone has no fill entry for the
   requested layer and can hang -> only called for layers the zone is on.
4. Iterating zone layers with `range(GetCopperLayerCount())` yields {0,1} and
   misses `B_Cu` (31) on 2-layer boards -> B.Cu zone islands were never
   enumerated, reporting 9 false isolated groups; iterate
   `[l for l in range(32) if IsCopperLayer(l)]` instead.
5. Fill keeps a clearance slot around tracks, so island contact detection must
   sample track endpoints (pad/via centers), not the centerline.
6. Thermal spokes span the clearance annulus; pads/vias are detected by ring
   samples at `hw*0.99` and `hw+0.1` (inside the 0.2 mm clearance gap, so
   unconnected items never false-hit).

## Validation

- `tests/integration_test.py`: 17/17 passed (KiCad 10.0.6 demo project).
- `tests/stdio_test.py`: MCP handshake + tools/list -> 22 tools, passed.
- Real-data check on airgo_gate main board (UNC path, Korean directory names):
  pre-fix backup -> 3 groups, 2 isolated (J1.2: 3 islands/60.3 mm2,
  A1.4: 2 islands/23.7 mm2) exactly matching the known defects and DRC's
  2 unconnected items; repaired board -> 1 group, 0 isolated, matching DRC
  0 unconnected (24 F.Cu + 11 B.Cu islands all merged).
- DRC items detail verified on the backup (positions in mm + uuid per item).
- `.bak` DRC works via temp copy; edit round-trip (backup file creation,
  add_tracks/add_via/refill_zones on a temp copy) verified in tests.

# 2026-10-21: KiCad MCP server v0.1.0 (Phase 1)

## Summary

Built a from-scratch MCP server for KiCad 10 in `C:\Dev_APP\pc_app\mcp-cad`:
16 tools covering project creation, schematic analysis, netlist connectivity,
ERC/DRC validation, and manufacturing exports (Gerber/drill/BOM/pos/render),
registered in opencode's global config.

## Files Changed

- `pyproject.toml`, `README.md`, `log.md` (new)
- `src/kicad_mcp/__init__.py`, `__main__.py`, `py.typed` (new)
- `src/kicad_mcp/server.py` (new, FastMCP server, 16 tools)
- `src/kicad_mcp/kicad_cli.py` (new, kicad-cli subprocess wrapper)
- `src/kicad_mcp/sexpr.py` (new, minimal S-expression parser)
- `src/kicad_mcp/sch_parser.py` (new, read-only schematic + netlist reader)
- `src/kicad_mcp/board_parser.py` (new, read-only PCB reader)
- `src/kicad_mcp/ipc.py` (new, kipy live connection status)
- `tests/integration_test.py`, `tests/stdio_test.py` (new)
- `C:\Users\skfin\.config\opencode\opencode.jsonc` (added `mcp.kicad` block)

## Reason

User requested (1) a self-built KiCad MCP server and (2) an ESP32-S3 + VL53L0X
IoT board design. Phase 1 delivers the MCP server so Phase 2 design work can
use it (create project, run ERC/DRC, export manufacturing files).

## Root Cause (issues found and fixed during build)

1. `sexpr.parse()` returned the outer wrapper list; readers expected the root
   node -> `load_file()` now returns `tree[0]`.
2. Netlist `net`/`comp` nodes are nested inside `(nets ...)`/`(components ...)`
   sections, not direct root children -> `read_netlist()` now searches within
   the sections.
3. kipy 0.8.0 has no `KiCad.create()`/`KiCad.connect()`; correct entry is
   `KiCadClient(socket_path, name, token, timeout)` + `KiCad.from_client()`.
   Windows default socket: `ipc://%TEMP%\kicad\api.sock`.
4. `kicad-cli api-server` (headless IPC) does not exist in KiCad 10.0.6 stable
   -> live IPC requires the KiCad GUI with API server enabled.
5. KiCad 10 `.kicad_pcb` footprints store reference/value as
   `(property "Reference" ...)` (KiCad 8+), not `(fp_text reference ...)`;
   board_parser checks both for compatibility.

## Validation

- `tests/integration_test.py`: 14/14 passed against KiCad 10.0.6 demo project
  (pic_programmer): version, read_schematic, list_components,
  export_netlist+list_nets (111 nets), run_erc, run_drc, get_board_info,
  export_gerbers, export_drill, export_bom, export_pos, render_pcb,
  create_project, ipc_status.
- `tests/stdio_test.py`: MCP handshake (initialize, tools/list -> 16 tools,
  tools/call get_kicad_version and read_schematic) passed over real stdio.
- opencode config JSON validated with node JSON.parse; server restart required
  for the MCP registration to load (user must restart opencode).
- IPC `get_status` fails gracefully (ConnectionError) when KiCad GUI is not
  running; full IPC round-trip still unverified until KiCad GUI is started
  with API server enabled.
