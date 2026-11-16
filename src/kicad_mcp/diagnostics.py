"""Environment checks and a small end-to-end self test."""

from __future__ import annotations

import os
import shutil
import tempfile
import uuid
from pathlib import Path

from . import board_parser, kicad_cli, paths, sexpr


def _check(name: str, fn) -> dict:
    try:
        value = fn()
        return {"name": name, "ok": True, "value": value}
    except Exception as exc:
        return {"name": name, "ok": False, "error": f"{type(exc).__name__}: {exc}"}


def _library_dirs() -> dict:
    program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    root = None
    for version in kicad_cli.DEFAULT_KICAD_VERSIONS:
        candidate = Path(program_files) / "KiCad" / version / "share" / "kicad"
        if candidate.is_dir():
            root = candidate
            break
    if root is None:
        return {"symbols": None, "footprints": None, "3dmodels": None}
    return {
        "symbols": str(root / "symbols"),
        "footprints": str(root / "footprints"),
        "3dmodels": str(root / "3dmodels"),
    }


def doctor() -> dict:
    checks = []
    checks.append(_check("kicad_cli", lambda: kicad_cli._version()))
    checks.append(
        _check(
            "kicad_python",
            lambda: _kicad_python_check(),
        )
    )
    checks.append(
        _check(
            "kicad_python_bindings",
            lambda: {
                "kipy": __import__("kipy").__version__
                if hasattr(__import__("kipy"), "__version__")
                else "installed",
            },
        )
    )
    checks.append(
        _check(
            "ipc",
            lambda: __import__("kicad_mcp.ipc", fromlist=["get_status"]).get_status(),
        )
    )
    libs = _library_dirs()
    checks.append(
        _check(
            "libraries",
            lambda: {
                "paths": libs,
                "symbol_files": len(list(Path(libs["symbols"]).glob("*.kicad_sym")))
                if libs.get("symbols") and Path(libs["symbols"]).is_dir()
                else 0,
                "footprint_libs": len(list(Path(libs["footprints"]).glob("*.pretty")))
                if libs.get("footprints") and Path(libs["footprints"]).is_dir()
                else 0,
            },
        )
    )
    checks.append(
        _check(
            "temp_dir",
            lambda: _temp_writable(),
        )
    )
    checks.append(
        _check(
            "path_normalization",
            lambda: str(paths.normalize_path(Path(__file__).parent)),
        )
    )
    checks.append(
        _check(
            "sexpr_roundtrip",
            lambda: _sexpr_roundtrip(),
        )
    )
    return {
        "ok": all(check["ok"] for check in checks),
        "checks": checks,
        "versions": {
            "kicad_cli": next(
                (c["value"] for c in checks if c["name"] == "kicad_cli" and c["ok"]), None
            ),
            "fastmcp": __import__("fastmcp").__version__
            if hasattr(__import__("fastmcp"), "__version__")
            else "installed",
        },
        "paths": libs,
    }


def _kicad_python_check() -> dict:
    import subprocess

    python = kicad_cli.find_kicad_python()
    proc = subprocess.run(
        [str(python), "-c", "import pcbnew; print(pcbnew.Version())"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"returncode {proc.returncode}")
    return {"path": str(python), "pcbnew": proc.stdout.strip()}


def _temp_writable() -> dict:
    root = Path(tempfile.gettempdir()) / "kicad-mcp"
    root.mkdir(parents=True, exist_ok=True)
    probe = root / f"doctor-{uuid.uuid4().hex}.txt"
    paths.atomic_write_text(probe, "ok")
    content = probe.read_text(encoding="utf-8")
    probe.unlink()
    if content != "ok":
        raise RuntimeError("atomic write probe returned unexpected content")
    return str(root)


def _sexpr_roundtrip() -> dict:
    text = '(kicad_sch (version 20260101) (generator "eeschema") (uuid "x"))'
    tree = sexpr.parse(text)[0]
    dumped = sexpr.dump(tree)
    again = sexpr.parse(dumped)[0]
    if again[0] != "kicad_sch" or again[1][0] != "version":
        raise RuntimeError("round-trip changed the root")
    return dumped


def _demo_dir() -> Path | None:
    env = os.environ.get("KICAD_DEMO_DIR")
    candidates = []
    if env:
        candidates.append(Path(env))
    program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    for version in kicad_cli.DEFAULT_KICAD_VERSIONS:
        candidates.append(
            Path(program_files) / "KiCad" / version / "share" / "kicad" / "demos" / "pic_programmer"
        )
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return None


def self_test() -> dict:
    work = Path(tempfile.gettempdir()) / "kicad-mcp-selftest" / uuid.uuid4().hex
    work.mkdir(parents=True, exist_ok=True)
    steps = []

    def step(name: str, fn) -> None:
        try:
            value = fn()
            steps.append({"name": name, "ok": True, "value": value})
        except Exception as exc:
            steps.append({"name": name, "ok": False, "error": f"{type(exc).__name__}: {exc}"})

    demo = _demo_dir()
    if demo is None:
        return {
            "ok": False,
            "reason": "KiCad demo project not found; set KICAD_DEMO_DIR",
            "work_dir": str(work),
            "steps": steps,
        }
    project = work / "pic_programmer"
    shutil.copytree(demo, project)
    sch = project / "pic_programmer.kicad_sch"
    pcb = project / "pic_programmer.kicad_pcb"

    step("read_board", lambda: board_parser.read_board(pcb)["stats"])
    step(
        "netlist",
        lambda: kicad_cli.export_netlist(sch, project / "selftest-netlist.net"),
    )
    step(
        "drc",
        lambda: kicad_cli.run_drc(pcb, work / "drc", limit=5)["summary"],
    )
    step(
        "erc",
        lambda: kicad_cli.run_erc(sch, work / "erc", limit=5)["summary"],
    )
    step(
        "gerbers",
        lambda: kicad_cli.export_gerbers(pcb, work / "gerbers", profile="standard"),
    )
    step(
        "drill",
        lambda: kicad_cli.export_drill(pcb, work / "gerbers", separate_th=True),
    )
    step(
        "sch_roundtrip",
        lambda: _sexpr_file_roundtrip(sch, work / "roundtrip.kicad_sch"),
    )
    ok = all(item["ok"] for item in steps)
    return {
        "ok": ok,
        "work_dir": str(work),
        "project": str(project),
        "steps": steps,
    }


def _sexpr_file_roundtrip(source: Path, target: Path) -> dict:
    tree = sexpr.load_file(source)
    dumped = sexpr.dump(tree)
    paths.atomic_write_text(target, dumped)
    again = sexpr.load_file(target)
    if again[0] != "kicad_sch":
        raise RuntimeError("round-trip root changed")
    return {
        "source_bytes": source.stat().st_size,
        "target_bytes": target.stat().st_size,
        "root": again[0],
    }
