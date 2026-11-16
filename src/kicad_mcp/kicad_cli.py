import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

DEFAULT_KICAD_VERSIONS = ["10.0", "9.0", "8.0"]
_DEFAULT_TIMEOUT = 120.0


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
    p = Path(path).expanduser().resolve()
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
        items.append(
            {
                "description": it.get("description"),
                "pos_mm": [pos.get("x"), pos.get("y")] if pos else None,
                "uuid": it.get("uuid"),
            }
        )
    return items


def _parse_check_report(report_path: Path, command: str, source: Path, returncode: int, stderr: str) -> dict:
    result: dict = {"command": command, "source": str(source), "report": str(report_path), "returncode": returncode}
    if not report_path.is_file():
        result["stderr"] = stderr
        return result
    import json

    data = json.loads(report_path.read_text(encoding="utf-8"))
    violations = list(data.get("violations", []))
    unconnected = list(data.get("unconnected_items", []))
    for sheet in data.get("sheets", []):
        violations.extend(sheet.get("violations", []))
        unconnected.extend(sheet.get("unconnected_items", []))
    counts = {"error": 0, "warning": 0, "excluded": 0}
    for v in violations:
        sev = v.get("severity", "error")
        counts[sev] = counts.get(sev, 0) + 1
    result["counts"] = counts
    result["violations"] = [
        {
            "severity": v.get("severity"),
            "type": v.get("type"),
            "description": v.get("description"),
            "items": _check_items(v),
        }
        for v in violations
    ]
    result["unconnected_items"] = [
        {"description": u.get("description", ""), "items": _check_items(u)} for u in unconnected
    ]
    return result


def run_erc(schematic_path: str | Path, report_dir: str | Path | None = None, severities: str = "all") -> dict:
    sch = _require_file(schematic_path, ".kicad_sch")
    out_dir = Path(report_dir).expanduser().resolve() if report_dir else sch.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    report = out_dir / f"{sch.stem}-erc.json"
    args = ["sch", "erc", "--format", "json", "-o", str(report)]
    if severities == "all":
        args.append("--severity-all")
    else:
        for sev in severities.split(","):
            args.append(f"--severity-{sev.strip()}")
    proc = run_cli([*args, str(sch)])
    return _parse_check_report(report, "erc", sch, proc.returncode, proc.stderr)


def run_drc(pcb_path: str | Path, report_dir: str | Path | None = None, severities: str = "all", schematic_parity: bool = False) -> dict:
    pcb = _require_file(pcb_path, ".kicad_pcb")
    out_dir = Path(report_dir).expanduser().resolve() if report_dir else pcb.parent
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
    return _parse_check_report(report, "drc", pcb, proc.returncode, proc.stderr)


def export_netlist(schematic_path: str | Path, output_path: str | Path | None = None) -> dict:
    sch = _require_file(schematic_path, ".kicad_sch")
    out = Path(output_path).expanduser().resolve() if output_path else sch.parent / f"{sch.stem}-netlist.net"
    out.parent.mkdir(parents=True, exist_ok=True)
    proc = run_cli(["sch", "export", "netlist", "-o", str(out), str(sch)])
    return {
        "command": "export_netlist",
        "output": str(out),
        "returncode": proc.returncode,
        "stderr": proc.stderr or None,
    }


def _snapshot_dir(out_dir: Path) -> dict[str, float]:
    if not out_dir.is_dir():
        return {}
    return {f.name: f.stat().st_mtime for f in out_dir.iterdir() if f.is_file()}


def _diff_files(out_dir: Path, before: dict[str, float]) -> list[str]:
    after = _snapshot_dir(out_dir)
    return sorted(name for name, mtime in after.items() if before.get(name) != mtime)


def export_gerbers(pcb_path: str | Path, output_dir: str | Path | None = None, layers: str | None = None) -> dict:
    pcb = _require_file(pcb_path, ".kicad_pcb")
    out_dir = Path(output_dir).expanduser().resolve() if output_dir else pcb.parent / "gerbers"
    out_dir.mkdir(parents=True, exist_ok=True)
    before = _snapshot_dir(out_dir)
    args = ["pcb", "export", "gerbers", "-o", str(out_dir)]
    if layers:
        args.extend(["--layers", layers])
    proc = run_cli([*args, str(pcb)])
    files = _diff_files(out_dir, before)
    return {
        "command": "export_gerbers",
        "output_dir": str(out_dir),
        "files": files,
        "files_in_output_dir": sorted(_snapshot_dir(out_dir)),
        "returncode": proc.returncode,
        "stderr": proc.stderr or None,
    }


def export_drill(pcb_path: str | Path, output_dir: str | Path | None = None, generate_map: bool = False) -> dict:
    pcb = _require_file(pcb_path, ".kicad_pcb")
    out_dir = Path(output_dir).expanduser().resolve() if output_dir else pcb.parent / "gerbers"
    out_dir.mkdir(parents=True, exist_ok=True)
    before = _snapshot_dir(out_dir)
    args = ["pcb", "export", "drill", "-o", str(out_dir)]
    if generate_map:
        args.extend(["--generate-map", "--map-format", "gerber"])
    proc = run_cli([*args, str(pcb)])
    files = _diff_files(out_dir, before)
    return {
        "command": "export_drill",
        "output_dir": str(out_dir),
        "files": files,
        "files_in_output_dir": sorted(_snapshot_dir(out_dir)),
        "returncode": proc.returncode,
        "stderr": proc.stderr or None,
    }


def export_bom(schematic_path: str | Path, output_path: str | Path | None = None) -> dict:
    sch = _require_file(schematic_path, ".kicad_sch")
    out = Path(output_path).expanduser().resolve() if output_path else sch.parent / f"{sch.stem}-bom.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    args = [
        "sch",
        "export",
        "bom",
        "--fields",
        "Reference,Value,Footprint,${QUANTITY},${DNP}",
        "--labels",
        "Refs,Value,Footprint,Qty,DNP",
        "--group-by",
        "Value",
        "-o",
        str(out),
    ]
    proc = run_cli([*args, str(sch)])
    return {
        "command": "export_bom",
        "output": str(out),
        "returncode": proc.returncode,
        "stderr": proc.stderr or None,
    }


def export_pos(pcb_path: str | Path, output_path: str | Path | None = None) -> dict:
    pcb = _require_file(pcb_path, ".kicad_pcb")
    out = Path(output_path).expanduser().resolve() if output_path else pcb.parent / "gerbers" / f"{pcb.stem}-pos.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    proc = run_cli(["pcb", "export", "pos", "--format", "csv", "--units", "mm", "-o", str(out), str(pcb)])
    return {
        "command": "export_pos",
        "output": str(out),
        "returncode": proc.returncode,
        "stderr": proc.stderr or None,
    }


def render_pcb(pcb_path: str | Path, output_png: str | Path | None = None, width: int = 1280, height: int = 720) -> dict:
    pcb = _require_file(pcb_path, ".kicad_pcb")
    out = Path(output_png).expanduser().resolve() if output_png else pcb.parent / f"{pcb.stem}-render.png"
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
    }


BOARD_CREATE_SCRIPT = """
import sys
import pcbnew
board = pcbnew.NewBoard(sys.argv[1])
pcbnew.SaveBoard(sys.argv[1], board)
print("ok")
"""


def create_board_file(pcb_path: str | Path) -> dict:
    pcb = Path(pcb_path).expanduser().resolve()
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
