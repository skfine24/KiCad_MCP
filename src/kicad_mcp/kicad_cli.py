import csv
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from collections import Counter
from datetime import datetime
from pathlib import Path

from . import paths

DEFAULT_KICAD_VERSIONS = ["10.0", "9.0", "8.0"]
_DEFAULT_TIMEOUT = 120.0
STANDARD_GERBER_LAYERS = "F.Cu,B.Cu,F.SilkS,B.SilkS,F.Mask,B.Mask,F.Paste,B.Paste,Edge.Cuts"


def find_kicad_cli() -> Path:
    env_path = os.environ.get("KICAD_CLI")
    if env_path and Path(env_path).is_file():
        return Path(env_path)
    program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    for version in DEFAULT_KICAD_VERSIONS:
        candidate = Path(program_files) / "KiCad" / version / "bin" / "kicad-cli.exe"
        if candidate.is_file():
            return candidate
    which = shutil.which("kicad-cli")
    if which:
        return Path(which)
    raise FileNotFoundError("kicad-cli.exe not found; set KICAD_CLI env var")


def find_kicad_python() -> Path:
    program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    for version in DEFAULT_KICAD_VERSIONS:
        candidate = Path(program_files) / "KiCad" / version / "bin" / "python.exe"
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("KiCad bundled python.exe not found")


def run_cli(args: list[str], timeout: float = _DEFAULT_TIMEOUT) -> subprocess.CompletedProcess:
    cli = find_kicad_cli()
    proc = subprocess.run(
        [str(cli), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    return proc


def _require_file(path: str | Path, suffix: str) -> Path:
    p = paths.normalize_path(path)
    if not p.is_file():
        raise FileNotFoundError(f"file not found: {p}")
    allowed = (suffix, ".bak") if suffix == ".kicad_pcb" else (suffix,)
    if p.suffix.lower() not in allowed:
        raise ValueError(f"expected {suffix} file, got: {p.name}")
    return p


def _version() -> str:
    return run_cli(["version"]).stdout.strip()


def _check_items(violation: dict) -> list[dict]:
    items = []
    for it in violation.get("items", []) or []:
        pos = it.get("pos") or {}
        description = it.get("description")
        net_match = re.search(r"\[([^\]]+)\]", description or "")
        items.append(
            {
                "description": description,
                "net": net_match.group(1) if net_match else None,
                "pos_mm": [pos.get("x"), pos.get("y")] if pos else None,
                "uuid": it.get("uuid"),
            }
        )
    return items


def _violation_id(violation: dict, items: list[dict]) -> str:
    import hashlib

    payload = json.dumps(
        [
            violation.get("severity"),
            violation.get("type"),
            violation.get("description"),
            sorted(
                (item.get("uuid") or "", item.get("pos_mm") or [], item.get("net") or "")
                for item in items
            ),
        ],
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def _parse_check_report(
    report_path: Path,
    command: str,
    source: Path,
    returncode: int,
    stderr: str,
) -> dict:
    result: dict = {
        "command": command,
        "source": str(source),
        "source_revision": paths.file_revision(source),
        "report": str(report_path),
        "returncode": returncode,
    }
    if not report_path.is_file():
        result["stderr"] = stderr
        return result
    try:
        data = json.loads(report_path.read_text(encoding="utf-8"))
    except Exception as exc:
        result["stderr"] = stderr
        result["parse_error"] = f"{type(exc).__name__}: {exc}"
        return result

    result.update(
        {
            "schema": data.get("$schema"),
            "coordinate_units": data.get("coordinate_units"),
            "date": data.get("date"),
            "kicad_version": data.get("kicad_version"),
            "schematic_parity": data.get("schematic_parity"),
            "included_severities": data.get("included_severities"),
            "ignored_checks": data.get("ignored_checks"),
            "report_revision": paths.file_revision(report_path),
            "stderr": stderr or None,
        }
    )

    raw_violations = list(data.get("violations", []))
    raw_unconnected = list(data.get("unconnected_items", []))
    sheets = []
    for sheet in data.get("sheets", []):
        sheets.append(
            {
                "path": sheet.get("path"),
                "name": sheet.get("name"),
                "violations": len(sheet.get("violations", [])),
                "unconnected_items": len(sheet.get("unconnected_items", [])),
            }
        )
        raw_violations.extend(sheet.get("violations", []))
        raw_unconnected.extend(sheet.get("unconnected_items", []))

    violations = []
    counts = Counter()
    by_type = Counter()
    by_net = Counter()
    for violation in raw_violations:
        items = _check_items(violation)
        nets = sorted({item["net"] for item in items if item.get("net")})
        severity = violation.get("severity", "error")
        vtype = violation.get("type")
        counts[severity] += 1
        by_type[vtype] += 1
        for net in nets:
            by_net[net] += 1
        violations.append(
            {
                "id": _violation_id(violation, items),
                "severity": severity,
                "type": vtype,
                "description": violation.get("description"),
                "nets": nets,
                "item_count": len(items),
                "items": items,
            }
        )

    unconnected = []
    for item in raw_unconnected:
        items = _check_items(item)
        nets = sorted({i["net"] for i in items if i.get("net")})
        unconnected.append(
            {
                "description": item.get("description", ""),
                "severity": item.get("severity"),
                "type": item.get("type"),
                "nets": nets,
                "item_count": len(items),
                "items": items,
            }
        )

    result.update(
        {
            "counts": {
                "error": counts.get("error", 0),
                "warning": counts.get("warning", 0),
                "excluded": counts.get("excluded", 0),
                **{key: value for key, value in counts.items() if key not in ("error", "warning", "excluded")},
            },
            "summary": {
                "violation_total": len(violations),
                "unconnected_total": len(unconnected),
                "by_severity": dict(sorted(counts.items())),
                "by_type": dict(sorted(by_type.items(), key=lambda kv: (-kv[1], str(kv[0])))),
                "by_net": dict(sorted(by_net.items(), key=lambda kv: (-kv[1], str(kv[0])))),
            },
            "sheets": sheets,
            "violations": violations,
            "unconnected_items": unconnected,
        }
    )
    return result


def _filter_violations(
    violations: list[dict],
    severity: str | None,
    type_filter: str | None,
    net: str | None,
) -> list[dict]:
    rows = violations
    if severity:
        wanted = {value.strip().lower() for value in severity.split(",") if value.strip()}
        rows = [v for v in rows if str(v.get("severity")).lower() in wanted]
    if type_filter:
        wanted = {value.strip().lower() for value in type_filter.split(",") if value.strip()}
        rows = [v for v in rows if str(v.get("type")).lower() in wanted]
    if net:
        needle = net.lower()
        rows = [
            v
            for v in rows
            if any(needle in (name or "").lower() for name in v.get("nets", []))
        ]
    return rows


def _page(rows: list[dict], offset: int, limit: int | None) -> tuple[list[dict], dict]:
    total = len(rows)
    start = max(0, int(offset))
    if limit is None or limit <= 0:
        page = rows[start:]
        page_limit = total
    else:
        page_limit = int(limit)
        page = rows[start : start + page_limit]
    returned = len(page)
    return page, {
        "offset": start,
        "limit": page_limit,
        "returned": returned,
        "total": total,
        "has_more": start + returned < total,
        "next_offset": start + returned if start + returned < total else None,
    }


def paginate_check_report(
    result: dict,
    offset: int = 0,
    limit: int | None = None,
    severity: str | None = None,
    type_filter: str | None = None,
    net: str | None = None,
    include_items: bool = True,
    summary_only: bool = False,
) -> dict:
    if "violations" not in result:
        return result
    filtered = _filter_violations(result["violations"], severity, type_filter, net)
    page, page_meta = _page(filtered, offset, limit)

    filtered_unconnected = result.get("unconnected_items", [])
    if net:
        needle = net.lower()
        filtered_unconnected = [
            item
            for item in filtered_unconnected
            if any(needle in (name or "").lower() for name in item.get("nets", []))
        ]
    unconnected_page, unconnected_meta = _page(filtered_unconnected, offset, limit)

    if not include_items:
        page = [
            {key: value for key, value in row.items() if key != "items"}
            for row in page
        ]
        unconnected_page = [
            {key: value for key, value in row.items() if key != "items"}
            for row in unconnected_page
        ]

    out = dict(result)
    out["summary"] = dict(result.get("summary", {}))
    out["summary"]["filtered_violation_total"] = len(filtered)
    out["summary"]["filtered_unconnected_total"] = len(filtered_unconnected)
    out["page"] = page_meta
    out["unconnected_page"] = unconnected_meta
    out["violations"] = page
    out["unconnected_items"] = unconnected_page
    if summary_only:
        out.pop("violations", None)
        out.pop("unconnected_items", None)
    return out


def run_erc(
    schematic_path: str | Path,
    report_dir: str | Path | None = None,
    severities: str = "all",
    offset: int = 0,
    limit: int | None = None,
    severity: str | None = None,
    type_filter: str | None = None,
    net: str | None = None,
    include_items: bool = True,
    summary_only: bool = False,
) -> dict:
    sch = _require_file(schematic_path, ".kicad_sch")
    out_dir = paths.normalize_path(report_dir) if report_dir else sch.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    report = out_dir / f"{sch.stem}-erc.json"
    args = ["sch", "erc", "--format", "json", "-o", str(report)]
    if severities == "all":
        args.append("--severity-all")
    else:
        for sev in severities.split(","):
            args.append(f"--severity-{sev.strip()}")
    proc = run_cli([*args, str(sch)])
    result = _parse_check_report(report, "erc", sch, proc.returncode, proc.stderr)
    return paginate_check_report(
        result,
        offset=offset,
        limit=limit,
        severity=severity,
        type_filter=type_filter,
        net=net,
        include_items=include_items,
        summary_only=summary_only,
    )


def run_drc(
    pcb_path: str | Path,
    report_dir: str | Path | None = None,
    severities: str = "all",
    schematic_parity: bool = False,
    offset: int = 0,
    limit: int | None = None,
    severity: str | None = None,
    type_filter: str | None = None,
    net: str | None = None,
    include_items: bool = True,
    summary_only: bool = False,
) -> dict:
    pcb = _require_file(pcb_path, ".kicad_pcb")
    out_dir = paths.normalize_path(report_dir) if report_dir else pcb.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    report = out_dir / f"{pcb.stem}-drc.json"
    run_path = pcb
    tmp_copy = None
    if pcb.suffix.lower() != ".kicad_pcb":
        # kicad-cli rejects other suffixes (e.g. .bak backups); analyze a temp copy
        tmp_dir = Path(tempfile.gettempdir()) / "kicad-mcp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        tmp_copy = tmp_dir / f"{uuid.uuid4().hex}{pcb.stem}.kicad_pcb"
        shutil.copy2(pcb, tmp_copy)
        run_path = tmp_copy
    try:
        args = ["pcb", "drc", "--format", "json", "-o", str(report)]
        if schematic_parity:
            args.append("--schematic-parity")
        if severities == "all":
            args.append("--severity-all")
        else:
            for sev in severities.split(","):
                args.append(f"--severity-{sev.strip()}")
        proc = run_cli([*args, str(run_path)])
    finally:
        if tmp_copy is not None:
            try:
                tmp_copy.unlink()
            except OSError:
                pass
    result = _parse_check_report(report, "drc", pcb, proc.returncode, proc.stderr)
    return paginate_check_report(
        result,
        offset=offset,
        limit=limit,
        severity=severity,
        type_filter=type_filter,
        net=net,
        include_items=include_items,
        summary_only=summary_only,
    )


def export_netlist(
    schematic_path: str | Path,
    output_path: str | Path | None = None,
    refresh: bool = True,
) -> dict:
    sch = _require_file(schematic_path, ".kicad_sch")
    out = paths.normalize_path(output_path) if output_path else sch.parent / f"{sch.stem}-netlist.net"
    out.parent.mkdir(parents=True, exist_ok=True)
    if not refresh and out.is_file() and out.stat().st_mtime_ns >= sch.stat().st_mtime_ns:
        return {
            "command": "export_netlist",
            "output": str(out),
            "exported": False,
            "returncode": 0,
            "stderr": None,
            "source_revision": paths.file_revision(sch),
            "revision": paths.file_revision(out),
            "meta_path": str(out.with_name(out.name + ".meta.json")),
        }
    proc = run_cli(["sch", "export", "netlist", "-o", str(out), str(sch)])
    source_rev = paths.file_revision(sch)
    output_rev = paths.file_revision(out)
    meta = {
        "source": str(sch),
        "output": str(out),
        "source_revision": source_rev,
        "output_revision": output_rev,
        "kicad_cli": _version(),
        "exported_at": datetime.now().isoformat(timespec="seconds"),
    }
    meta_path = out.with_name(out.name + ".meta.json")
    paths.atomic_write_text(meta_path, json.dumps(meta, indent=2, ensure_ascii=False))
    return {
        "command": "export_netlist",
        "output": str(out),
        "exported": True,
        "returncode": proc.returncode,
        "stderr": proc.stderr or None,
        "source_revision": source_rev,
        "revision": output_rev,
        "meta_path": str(meta_path),
        "meta": meta,
    }


def _snapshot_dir(out_dir: Path) -> dict[str, float]:
    if not out_dir.is_dir():
        return {}
    return {f.name: f.stat().st_mtime for f in out_dir.iterdir() if f.is_file()}


def _diff_files(out_dir: Path, before: dict[str, float]) -> list[str]:
    after = _snapshot_dir(out_dir)
    return sorted(name for name, mtime in after.items() if before.get(name) != mtime)


def export_gerbers(
    pcb_path: str | Path,
    output_dir: str | Path | None = None,
    layers: str | None = None,
    profile: str | None = None,
    board_plot_params: bool = False,
    check_zones: bool = False,
) -> dict:
    pcb = _require_file(pcb_path, ".kicad_pcb")
    out_dir = paths.normalize_path(output_dir) if output_dir else pcb.parent / "gerbers"
    out_dir.mkdir(parents=True, exist_ok=True)
    before = _snapshot_dir(out_dir)
    if layers is None and profile == "standard":
        layers = STANDARD_GERBER_LAYERS
    args = ["pcb", "export", "gerbers", "-o", str(out_dir)]
    if layers:
        args.extend(["--layers", layers])
    if board_plot_params:
        args.append("--board-plot-params")
    if check_zones:
        args.append("--check-zones")
    proc = run_cli([*args, str(pcb)])
    files = _diff_files(out_dir, before)
    return {
        "command": "export_gerbers",
        "output_dir": str(out_dir),
        "layers_used": layers,
        "profile": profile,
        "files": files,
        "files_in_output_dir": sorted(_snapshot_dir(out_dir)),
        "returncode": proc.returncode,
        "stderr": proc.stderr or None,
        "source_revision": paths.file_revision(pcb),
    }


def export_drill(
    pcb_path: str | Path,
    output_dir: str | Path | None = None,
    generate_map: bool = False,
    map_format: str = "gerber",
    separate_th: bool = False,
    generate_report: bool = False,
    report_path: str | Path | None = None,
) -> dict:
    pcb = _require_file(pcb_path, ".kicad_pcb")
    out_dir = paths.normalize_path(output_dir) if output_dir else pcb.parent / "gerbers"
    out_dir.mkdir(parents=True, exist_ok=True)
    before = _snapshot_dir(out_dir)
    args = ["pcb", "export", "drill", "-o", str(out_dir)]
    if generate_map:
        args.extend(["--generate-map", "--map-format", map_format])
    if separate_th:
        args.append("--excellon-separate-th")
    if generate_report:
        args.append("--generate-report")
        if report_path:
            args.extend(["--report-path", str(paths.normalize_path(report_path))])
    proc = run_cli([*args, str(pcb)])
    files = _diff_files(out_dir, before)
    result = {
        "command": "export_drill",
        "output_dir": str(out_dir),
        "files": files,
        "files_in_output_dir": sorted(_snapshot_dir(out_dir)),
        "returncode": proc.returncode,
        "stderr": proc.stderr or None,
        "source_revision": paths.file_revision(pcb),
    }
    if report_path:
        result["report"] = str(paths.normalize_path(report_path))
        result["report_exists"] = paths.normalize_path(report_path).is_file()
    return result


def _filter_bom_csv(
    path: Path,
    exclude_power: bool,
    exclude_dnp: bool,
) -> dict:
    if not path.is_file():
        return {"removed_power": 0, "removed_dnp": 0}
    text = path.read_text(encoding="utf-8-sig")
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        return {"removed_power": 0, "removed_dnp": 0}
    header = rows[0]
    try:
        value_index = header.index("Value")
    except ValueError:
        value_index = 1 if len(header) > 1 else None
    try:
        refs_index = header.index("Refs")
    except ValueError:
        refs_index = 0
    try:
        dnp_index = header.index("DNP")
    except ValueError:
        dnp_index = None

    kept = [header]
    removed_power = 0
    removed_dnp = 0
    for row in rows[1:]:
        if not row:
            continue
        is_power = False
        if exclude_power and value_index is not None and value_index < len(row):
            value = (row[value_index] or "").strip()
            refs = (row[refs_index] or "").strip() if refs_index < len(row) else ""
            is_power = value == "PWR_FLAG" or (
                "PWR_FLAG" in value and refs.replace(" ", "").startswith("PWR")
            )
        if is_power:
            removed_power += 1
            continue
        is_dnp = False
        if exclude_dnp and dnp_index is not None and dnp_index < len(row):
            is_dnp = (row[dnp_index] or "").strip().upper() in ("DNP", "YES", "TRUE", "1")
        if is_dnp:
            removed_dnp += 1
            continue
        kept.append(row)

    if removed_power or removed_dnp:
        output = io.StringIO()
        writer = csv.writer(output, lineterminator="\n")
        writer.writerows(kept)
        paths.atomic_write_text(path, output.getvalue(), newline="")
    return {"removed_power": removed_power, "removed_dnp": removed_dnp}


def export_bom(
    schematic_path: str | Path,
    output_path: str | Path | None = None,
    exclude_power: bool = True,
    exclude_dnp: bool = False,
    fields: str | None = None,
    labels: str | None = None,
    group_by: str = "Value",
) -> dict:
    sch = _require_file(schematic_path, ".kicad_sch")
    out = paths.normalize_path(output_path) if output_path else sch.parent / f"{sch.stem}-bom.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    fields = fields or "Reference,Value,Footprint,${QUANTITY},${DNP}"
    labels = labels or "Refs,Value,Footprint,Qty,DNP"
    args = [
        "sch",
        "export",
        "bom",
        "--fields",
        fields,
        "--labels",
        labels,
        "--group-by",
        group_by,
        "-o",
        str(out),
    ]
    if exclude_dnp:
        args.append("--exclude-dnp")
    proc = run_cli([*args, str(sch)])
    filtered = _filter_bom_csv(out, exclude_power=exclude_power, exclude_dnp=False)
    return {
        "command": "export_bom",
        "output": str(out),
        "returncode": proc.returncode,
        "stderr": proc.stderr or None,
        "exclude_power": exclude_power,
        "exclude_dnp": exclude_dnp,
        "removed_power": filtered["removed_power"],
        "revision": paths.file_revision(out),
        "source_revision": paths.file_revision(sch),
    }


def export_pos(
    pcb_path: str | Path,
    output_path: str | Path | None = None,
    format: str = "csv",
    units: str = "mm",
) -> dict:
    pcb = _require_file(pcb_path, ".kicad_pcb")
    out = paths.normalize_path(output_path) if output_path else pcb.parent / "gerbers" / f"{pcb.stem}-pos.{format}"
    out.parent.mkdir(parents=True, exist_ok=True)
    args = ["pcb", "export", "pos", "--format", format, "--units", units, "-o", str(out), str(pcb)]
    proc = run_cli(args)
    return {
        "command": "export_pos",
        "output": str(out),
        "exists": out.is_file(),
        "returncode": proc.returncode,
        "stderr": proc.stderr or None,
        "revision": paths.file_revision(out),
        "source_revision": paths.file_revision(pcb),
    }


def export_step(
    pcb_path: str | Path,
    output_path: str | Path | None = None,
    subst_models: bool = True,
    force: bool = True,
    no_dnp: bool = False,
    include_tracks: bool = False,
    include_zones: bool = False,
) -> dict:
    pcb = _require_file(pcb_path, ".kicad_pcb")
    out = paths.normalize_path(output_path) if output_path else pcb.parent / f"{pcb.stem}.step"
    out.parent.mkdir(parents=True, exist_ok=True)
    args = ["pcb", "export", "step", "-o", str(out)]
    if force:
        args.append("--force")
    if subst_models:
        args.append("--subst-models")
    if no_dnp:
        args.append("--no-dnp")
    if include_tracks:
        args.append("--include-tracks")
    if include_zones:
        args.append("--include-zones")
    proc = run_cli([*args, str(pcb)], timeout=600.0)
    return {
        "command": "export_step",
        "output": str(out),
        "exists": out.is_file(),
        "returncode": proc.returncode,
        "stderr": proc.stderr or None,
        "revision": paths.file_revision(out),
        "source_revision": paths.file_revision(pcb),
    }


def export_pdf(
    source_path: str | Path,
    output_path: str | Path | None = None,
    layers: str | None = None,
    mode: str = "single",
    black_and_white: bool = False,
    exclude_refdes: bool = False,
    exclude_value: bool = False,
) -> dict:
    source = paths.normalize_path(source_path)
    if not source.is_file():
        raise FileNotFoundError(f"file not found: {source}")
    suffix = source.suffix.lower()
    if suffix == ".kicad_pcb":
        out = paths.normalize_path(output_path) if output_path else source.parent / f"{source.stem}.pdf"
        args = ["pcb", "export", "pdf", "-o", str(out), "--mode-single"]
        if layers:
            args.extend(["--layers", layers])
        if black_and_white:
            args.append("--black-and-white")
        if exclude_refdes:
            args.append("--exclude-refdes")
        if exclude_value:
            args.append("--exclude-value")
        proc = run_cli([*args, str(source)], timeout=300.0)
        return {
            "command": "export_pdf",
            "kind": "pcb",
            "output": str(out),
            "exists": out.is_file(),
            "returncode": proc.returncode,
            "stderr": proc.stderr or None,
            "revision": paths.file_revision(out),
            "source_revision": paths.file_revision(source),
        }
    if suffix == ".kicad_sch":
        out = paths.normalize_path(output_path) if output_path else source.parent / f"{source.stem}.pdf"
        args = ["sch", "export", "pdf", "-o", str(out)]
        if black_and_white:
            args.append("--black-and-white")
        proc = run_cli([*args, str(source)], timeout=300.0)
        return {
            "command": "export_pdf",
            "kind": "schematic",
            "output": str(out),
            "exists": out.is_file(),
            "returncode": proc.returncode,
            "stderr": proc.stderr or None,
            "revision": paths.file_revision(out),
            "source_revision": paths.file_revision(source),
        }
    raise ValueError(f"expected .kicad_pcb or .kicad_sch file, got: {source.name}")


def render_pcb(
    pcb_path: str | Path,
    output_png: str | Path | None = None,
    width: int = 1280,
    height: int = 720,
) -> dict:
    pcb = _require_file(pcb_path, ".kicad_pcb")
    out = paths.normalize_path(output_png) if output_png else pcb.parent / f"{pcb.stem}-render.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    proc = run_cli(
        ["pcb", "render", "-o", str(out), "--width", str(width), "--height", str(height), str(pcb)],
        timeout=300.0,
    )
    return {
        "command": "render_pcb",
        "output": str(out),
        "exists": out.is_file(),
        "returncode": proc.returncode,
        "stderr": proc.stderr or None,
        "revision": paths.file_revision(out),
        "source_revision": paths.file_revision(pcb),
    }


BOARD_CREATE_SCRIPT = """
import sys
import pcbnew
board = pcbnew.NewBoard(sys.argv[1])
pcbnew.SaveBoard(sys.argv[1], board)
print("ok")
"""


def create_board_file(pcb_path: str | Path) -> dict:
    pcb = paths.normalize_path(pcb_path)
    pcb.parent.mkdir(parents=True, exist_ok=True)
    kpython = find_kicad_python()
    proc = subprocess.run(
        [str(kpython), "-c", BOARD_CREATE_SCRIPT, str(pcb)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60.0,
    )
    return {
        "output": str(pcb),
        "created": pcb.is_file(),
        "returncode": proc.returncode,
        "stderr": proc.stderr or None,
    }
