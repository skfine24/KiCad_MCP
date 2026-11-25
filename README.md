# KiCad_MCP

MCP (Model Context Protocol) server for KiCad 10, built on the official
`kicad-python` bindings and `kicad-cli`. One server covers the whole
design-to-order loop: schematic analysis, netlist connectivity, ERC/DRC
validation, transactional PCB and schematic editing, library search, mechanical
and manufacturing checks, live IPC editing of the open board, and **vendor part
search / BOM purchase lists (DigiKey Korea, IC Mart, DeviceMart, LCSC)**.

Repository: https://github.com/skfine24/KiCad_MCP

Current release: **v0.9.4** — multi-layer robustness fixes on top of v0.9.1.
v0.9.2 fixes bottom-side footprint placement, inner-layer track editing,
project-library resolution, manufacturing layer selection and staged refills.
v0.9.3 adds physical body-overlap detection from 3D models, and v0.9.4 adds a
silkscreen overlap / duplicate / close-pair check.

## What's new in v0.9.4

- **Silkscreen check**: `check_silk_overlaps(pcb_path, min_gap_mm=0.0,
  include_text=True)` reports `silk_overlaps` (partial intersections),
  `silk_duplicates` (coincident items such as stacked labels), `silk_close_pairs`
  (gap below `min_gap_mm`) and `silk_over_pads` (silk crossing a pad) for the
  same board side. It covers footprint silk graphics, visible text fields
  (Reference, Value and custom properties such as LCSC/MPN) and board-level
  silk texts. Rectangles are decomposed into their four outline edges, and text
  uses the effective glyph shape (`GetEffectiveTextShape`) instead of the
  padded text box. Graphics of the same footprint are skipped (their outline
  meets at corners) while text-text pairs are still compared, so stacked
  property labels are caught.
- **3D-model orientation fix** in `check_component_overlaps`: KiCad 3D models
  are Y-up, so the model Y is negated before mapping to board coordinates.
  Asymmetric bodies (e.g. Molex connectors) are no longer shifted.
- **PCB net creation**: `find_net` now creates a missing net on the board
  instead of failing, so tracks and vias can be added for a net that is not yet
  present in the `.kicad_pcb`.

## What's new in v0.9.3

- **Physical body overlaps**: `check_component_overlaps(pcb_path,
  min_gap_mm=0.0)` builds each footprint's body box from its 3D-model vertex
  bounding box (leads included) when consistent with the fab outline, otherwise
  from the fab outline, then the pads. Same-side intersections are reported as
  `body_overlaps`, pairs nearer than `min_gap_mm` as `close_pairs`, and courtyard
  overlaps where the bodies do not touch are returned separately as cosmetic.

## What's new in v0.9.2

- **Bottom-side footprints are flipped correctly**: `place_footprint(layer="B.Cu")`
  and `replace_footprint` now move pads, silkscreen, courtyard and fab items to
  B.Cu/B.* instead of only changing the footprint layer.
- **Inner copper layers**: `add_tracks` and `pcb_batch` accept
  `In1.Cu`..`In30.Cu`.
- **Project footprint libraries**: `Library:Footprint` resolves through the
  project's `fp-lib-table` (including `${KIPRJMOD}`); absolute/UNC `.kicad_mod`
  paths and board-relative paths work, with a file-name fallback.
- **Manufacturing layers**: `manufacturing_profile` detects the board's copper
  count and always exports inner layers (In1/In2 on 4-layer boards).
- **Staged refills keep project rules**: staging copies now include the sibling
  `.kicad_pro`, so `refill_first` uses the project netclasses instead of the
  built-in 0.2 mm defaults (which re-fragmented GND planes).
- **New tools**: `delete_footprint`, `set_track_width`, `export_dsn`,
  `import_ses`.
- **`sch_delete_item`** accepts `uuid` as an alias for `item_uuid`.
- **Atomic writes** retry `os.replace` on transient Windows/NAS sharing
  violations.
- **`pcb_batch`** documents every supported operation and its parameters.

## What's new in v0.9.1

- **PCB-side LCSC sourcing**: `apply_lcsc_bom_to_pcb` writes LCSC/MPN/
  Manufacturer/Supplier/Supplier Part/Stock fields to PCB footprints in one
  pcbnew batch save; fields are hidden by default so sourcing metadata is not
  printed on the silkscreen. `check_orderable_pcb_bom` reports the PCB BOM as
  `orderable`, `needs_review`, `low_stock`, `not_found`, or `not_orderable`.
- **LCSC alternatives**: `get_lcsc_alternatives` returns LCSC's alternative-part
  list for a product code (useful for out-of-stock parts such as AP63203WU-7).
- **MOQ/subtotal metadata**: sourcing reports now include `moq`, `order_qty`,
  `subtotal`, and currency for each group.

## What's new in v0.9.0

- **LCSC vendor search**: `search_lcsc`, `search_lcsc_bom`, `get_lcsc_part`.
  LCSC is also available through `search_parts(site="lcsc")`,
  `get_part(site="lcsc")`, and `compare_parts`. Results include the LCSC part
  number, MPN, brand, package, USD price tiers, stock, and product URL.
- **Orderable-BOM workflow**: `check_orderable_bom` reads a schematic, groups
  components by value/footprint/MPN/LCSC, searches LCSC, and reports each group
  as `orderable`, `needs_review`, `low_stock`, `not_found`, or `not_orderable`
  against the BOM quantity. `prepare_orderable_bom` optionally writes LCSC/MPN/
  Manufacturer/Supplier/Supplier Part/Stock/Datasheet properties back to the
  schematic in one save. `strict=True` blocks writes unless every group is
  exactly orderable; generic-value matches stay `needs_review` unless
  `allow_generic=True`. `apply_lcsc_to_schematic` sets one symbol from an LCSC
  code.
- **Design flow**: run `check_orderable_bom` after schematic capture, resolve
  any `needs_review`/`low_stock`/`not_found`/`not_orderable` groups, then run
  `prepare_orderable_bom(apply=True)` so the PCB is built from orderable parts.

## What's new in v0.8.0

The former standalone `parts-purchase` MCP server is merged into this package.
There is no second process and no second opencode entry; part search and BOM
purchasing are regular tools here.

- **Vendor search**: `search_parts`, `search_digikey`, `search_icmart`,
  `search_devicemart`, `compare_parts`, `get_part` and per-site product lookups.
  DigiKey Korea returns KRW price tiers, stock, MOQ/order multiple and the exact
  DigiKey product number (Product Information V4 API).
- **BOM purchasing**: `bom_purchase_list` prices BOM items against
  DigiKey/IC Mart/DeviceMart and recommends the cheapest available option;
  `check_digikey_bom` strictly requires an exact MPN or DigiKey product number
  for every non-module item.
- **DeviceMart guest cart**: `add_to_cart_devicemart`,
  `get_devicemart_cart`, `remove_from_cart_devicemart`. No login, no checkout
  automation; the cart survives restarts via a persisted cookie file.
- Modules (Pico/Pico W, VL53L0X/L1X/L5CX, TTP223 module, WS2812B strip) are
  excluded from the purchase BOM by default (`include_modules=True` to include
  them). KiCad BOM files are never modified by these tools.
- Network tools are annotated `openWorldHint`; no request is made until a tool
  is called.

Everything from v0.6.0/v0.7.0 is unchanged: fresh netlists, structured DRC/ERC,
transactional `pcb_batch`, schematic text editing, library search, mechanical
checks and manufacturing manifests.

## Requirements

- KiCad 10.0.x (tested with 10.0.8, API 10.0.6)
- Python 3.11+
- `uv`/`uvx` for the one-command install (or `pip`)
- For live IPC: KiCad GUI running with **Preferences > Plugins > Enable KiCad
  API server** on, and a PCB editor document open
- For DigiKey search: an API credential file (see below). IC Mart and
  DeviceMart need no credentials.

## Installation

### opencode (recommended)

Add this to `~/.config/opencode/opencode.jsonc`:

```jsonc
{
  "mcp": {
    "servers": {
      "kicad": {
        "type": "local",
        "command": [
          "uvx",
          "--from",
          "git+https://github.com/skfine24/KiCad_MCP.git@v0.9.4",
          "kicad-mcp"
        ]
      }
    }
  }
}
```

Restart opencode. `uvx` builds the package from the tagged GitHub source and
caches the environment; no local clone or virtualenv is required. Use `@main`
instead of `@v0.9.4` to track the latest commits.

If you previously had a separate `parts` MCP entry, remove it — v0.8.1 replaces
it.

### Install as a tool

```powershell
uv tool install git+https://github.com/skfine24/KiCad_MCP.git@v0.9.4
kicad-mcp
```

### pip

```powershell
pip install "kicad-mcp @ git+https://github.com/skfine24/KiCad_MCP.git@v0.9.4"
```

### Local development

```powershell
git clone https://github.com/skfine24/KiCad_MCP.git
cd KiCad_MCP
uv venv
uv pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m kicad_mcp
```

Windows note: an editable install on a network share whose path contains
non-ASCII characters can make Python fail while reading the generated `.pth`
file (`UnicodeDecodeError: 'cp949' codec ...`). Clone to a local ASCII path for
development; the `uvx` install is unaffected because it builds a wheel.

Source encoding: all source, test and documentation files are UTF-8 without BOM.
`.editorconfig` sets `charset = utf-8`, and `tests/test_encoding.py` fails the
suite if any file is not valid UTF-8.

## Vendor part search setup

DigiKey credentials are read from
`C:\Users\skfin\.config\opencode\parts-purchase\digikey.json` by default
(outside the repository):

```json
{
  "client_id": "...",
  "client_secret": "...",
  "customer_id": "..."
}
```

Override the path with `PARTS_MCP_DIGIKEY_CREDENTIALS`. The file must not be
committed.

DeviceMart guest-cart cookies are persisted in
`C:\Users\skfin\.config\opencode\parts-purchase\state\devicemart_cookies.json`
so the cart survives server restarts. Override the directory with
`KICAD_MCP_PARTS_STATE_DIR`.

## Tools (107)

| Category | Tools |
|---|---|
| Project | `create_project`, `list_projects` |
| Schematic analysis | `read_schematic`, `list_components`, `sch_revision` |
| Connectivity | `export_netlist`, `list_nets`, `trace_net` (fresh export by default; `refresh`, `expected_revision`, mtime/hash metadata) |
| Validation | `run_erc`, `run_drc` (structured severity/type/net/items, `offset`/`limit`, filters, report SHA-256), `find_duplicate_tracks`, `find_isolated_islands` |
| PCB inspection | `get_board_info` (KiCad 10 pad-level nets, thickness, stackup, netclasses, unconnected pads, optional schematic net merge), `pcb_revision`, `render_pcb` |
| PCB editing | `add_tracks`, `add_via`, `delete_tracks`, `trim_track`, `prune_dangling`, `refill_zones`, `pcb_batch` (one transaction, dry-run, rollback, expected revision, local staging + atomic save), `set_pad_net`, `set_footprint_value`, `replace_footprint`, `place_footprint`, `delete_footprint`, `set_track_width`, `set_zone_connection`, `add_zone`, `add_keepout`, `set_zone_priority`, `remove_islands`, `delete_zone` |
| Schematic editing | `sch_add_wire`, `sch_add_label`, `sch_add_no_connect`, `sch_set_value`, `sch_set_property`, `sch_add_symbol` (embeds/flattens lib_symbols, supports `extends`), `sch_delete_item` |
| Library | `search_symbols`, `search_footprints`, `get_pinout`, `get_footprint_info`, `list_libraries` |
| Mechanical | `mechanical_check`, `check_courtyard`, `check_component_overlaps` (3D-model/fab body boxes: overlaps, near-touches, courtyard-only), `check_silk_overlaps` (silk overlaps, coincident duplicates, close pairs, silk over pads), `check_3d_models`, `board_edge_clearance` |
| Manufacturing | `export_gerbers` (optional standard profile), `export_drill`, `export_bom`, `export_pos`, `export_step`, `export_pdf`, `export_dsn` (Specctra DSN), `import_ses` (routed session import), `manufacturing_profile` (standard layers + drill/POS/BOM/STEP/PDF + SHA-256 manifest), `verify_manufacturing_output` |
| BOM / sourcing | `bom_enrich` (MPN/Manufacturer/Supplier/Stock/Alternate + database merge), `bom_validate_mpn`, `check_orderable_bom` (LCSC per-group orderability), `prepare_orderable_bom` (optionally writes LCSC fields back), `apply_lcsc_to_schematic`, `apply_lcsc_bom_to_schematic`, `apply_lcsc_bom_to_pcb`, `check_orderable_pcb_bom` |
| Vendor part search | `search_parts` (`digikey` default, `icmart`, `devicemart`, `lcsc`, `both`, `all`), `search_digikey`, `search_icmart`, `search_devicemart`, `search_lcsc`, `search_lcsc_bom`, `compare_parts`, `get_part`, `get_digikey_product`, `get_icmart_product`, `get_devicemart_product`, `get_lcsc_part`, `get_lcsc_alternatives` |
| BOM purchasing | `bom_purchase_list` (per-item options, recommended option, availability, estimated KRW total), `check_digikey_bom` (strict MPN/stock check) |
| DeviceMart cart | `add_to_cart_devicemart` (confirm=True), `get_devicemart_cart`, `remove_from_cart_devicemart` (confirm=True) |
| Staged editing | `edit_begin`, `edit_commit` (atomic commit with backup/revision) |
| Live IPC | `ipc_open_board`, `ipc_board_info`, `ipc_get_schematic`, `ipc_list_nets`, `ipc_list_items`, `ipc_get_selection`, `ipc_items_at_point`, `ipc_add_track`, `ipc_add_via`, `ipc_batch` (one undo step via begin/push/drop commit), `ipc_delete_items`, `ipc_save`, `ipc_refill_zones`, `ipc_set_active_layer` |
| Status | `get_kicad_version`, `doctor`, `self_test` |

Resources: `kicad://board` (live IPC), `kicad://schematic` (live IPC),
`kicad://schematic-file/{path*}`, `kicad://project/{path*}`.

## BOM item shape for purchasing

`bom_purchase_list` accepts a list of objects:

```json
[
  {"name": "Schottky diode", "mpn": "SB540-E3/54", "digikey_pn": "SB540-E3/54GICT-ND", "qty": 2},
  {"name": "100nF", "mpn": "K104K15X7RF5TH5", "qty": 9},
  {"name": "10k", "mpn": "CFR-25JB-52-10K", "qty": 10}
]
```

`mpn`, `digikey_product_number`, `digikey_pn`, `digikey_mpn`, `icmart_id` and
`devicemart_no` are optional for `bom_purchase_list`; when absent the server
searches the selected site. Use exact MPNs or known product ids for
deterministic results. `check_digikey_bom` requires an exact MPN or DigiKey
product number for every non-module item; generic value-only items are
rejected so a fuzzy match is never reported as orderable.

## Safety

- `KICAD_MCP_SAFE_MODE=1` requires `confirm=True` on destructive tools.
- `dry_run` is available on PCB/schematic edit tools; `pcb_batch` rolls back on
  failure and checks `expected_revision`.
- Cart writes always require `confirm=True`; checkout/payment is not automated.
- Credentials and cookies live outside the repository.

## Live IPC connection (optional)

`get_kicad_version` reports a live IPC connection when:

1. KiCad GUI is running
2. Preferences > Plugins > "Enable KiCad API server" is on
3. A PCB editor document is open

Without it, all file-based tools still work headless via `kicad-cli`.

The KiCad IPC API has no open-document command. `ipc_open_board` therefore
launches `pcbnew.exe` with the PCB path when no PCB editor is running, and waits
until the document appears in IPC. If `pcbnew.exe` is already running it does
not launch a second editor; open the board manually in that case.

## Tests

```powershell
uv run --with pytest pytest -q            # 40 unit tests (core + vendor parsers + UTF-8 guard)
python tests/integration_test.py          # 19 tool-level checks against a real KiCad demo project
python tests/stdio_test.py                # MCP stdio handshake, tools/list (107), sample calls
python tests/smoke_live_parts.py          # live read-only DigiKey/IC Mart/DeviceMart/LCSC smoke test
```

The unit tests need no network and no KiCad install. `integration_test.py` copies
the KiCad `pic_programmer` demo project into a temp directory; override
`KICAD_DEMO_DIR` and `KICAD_MCP_TEST_DIR` if KiCad is installed elsewhere.
`smoke_live_parts.py` performs read-only searches and product lookups; DigiKey
requires the credential file above.

## Known limits

- Schematic editing is text-level (`.kicad_sch` S-expression editing); KiCad has
  no official schematic API. Re-run ERC after edits.
- `get_board_info` parses `.kicad_pcb` directly (fast); for design-rule-precise
  data prefer `run_drc`.
- Live IPC tools require the KiCad GUI, the API server, and an open PCB document.
- IC Mart and DeviceMart adapters parse server-rendered HTML/SSE; a site redesign
  can break them. DigiKey uses a stable JSON API.
- DigiKey API quotas apply; requests are rate-limited and tokens cached.
- Module parts are excluded from purchase BOMs by default and are not orderable
  through these tools.
- No login, checkout or payment automation for any vendor.

## Version history

| Version | Change |
|---|---|
| 0.9.4 | Silkscreen check `check_silk_overlaps` (overlaps, coincident duplicates, close pairs, silk over pads); 3D-model Y-axis fix in body overlap check; `find_net` creates missing nets |
| 0.9.3 | Physical body overlap check `check_component_overlaps` from 3D models / fab outlines / pads |
| 0.9.2 | MCP fixes: bottom-side flip, inner-layer tracks, project libraries, 4-layer manufacturing, staged refills, delete_footprint/set_track_width/DSN-SES |
| 0.8.1 | Maintenance: UTF-8 enforcement, dead-code cleanup, standalone parts server removed |
| 0.8.0 | Vendor part search + BOM purchase lists + DeviceMart cart merged in (14 tools) |
| 0.7.0 | Version bump only (release marker for the DigiKey BOM workflow) |
| 0.6.0 | Fresh netlists, structured DRC/ERC, transactional PCB/schematic editing, library/mechanical/manufacturing/BOM tools |
| 0.5.2 | Deployable package, portable tests, opencode setup |
| 0.5.1 | IPC tool import fix, stdout JSON parse fix |

## License

MIT
