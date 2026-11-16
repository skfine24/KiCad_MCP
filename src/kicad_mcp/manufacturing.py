"""Manufacturing profile export and checksum manifest."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from . import kicad_cli, paths, pcb_edit

_GERBER_EXTENSIONS = {
    "F.Cu": (".gtl",),
    "B.Cu": (".gbl",),
    "F.SilkS": (".gto",),
    "B.SilkS": (".gbo",),
    "F.Mask": (".gts",),
    "B.Mask": (".gbs",),
    "F.Paste": (".gtp",),
    "B.Paste": (".gbp",),
    "Edge.Cuts": (".gm1",),
}


def _collect_files(root: Path, exclude: set[str] | None = None) -> list[dict]:
    exclude = exclude or set()
    rows = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if relative in exclude:
            continue
        rows.append(
            {
                "path": relative,
                "size": path.stat().st_size,
                "sha256": paths.sha256_file(path),
            }
        )
    return rows


def _gerber_check(root: Path, layers: list[str]) -> dict:
    files = [path.name.lower() for path in root.rglob("*") if path.is_file()]
    present = {}
    missing = []
    for layer in layers:
        extensions = _GERBER_EXTENSIONS.get(layer)
        if not extensions:
            continue
        found = any(name.endswith(extensions) for name in files)
        present[layer] = found
        if not found:
            missing.append(layer)
    drill = [name for name in files if name.endswith(".drl")]
    return {
        "layers": present,
        "missing_layers": missing,
        "drill_files": drill,
        "drill_present": bool(drill),
    }


def _write_manifest(
    out_dir: Path,
    profile: str,
    layers: list[str],
    pcb: Path,
    sch: Path | None,
    exports: dict,
) -> dict:
    files = _collect_files(out_dir, exclude={"manifest.json"})
    checks = _gerber_check(out_dir, layers)
    manifest = {
        "schema": "kicad-mcp-manufacturing-manifest/v1",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "profile": profile,
        "kicad_cli": kicad_cli._version(),
        "layers": layers,
        "source": {
            "pcb": str(pcb),
            "pcb_revision": paths.file_revision(pcb),
            "schematic": str(sch) if sch else None,
            "schematic_revision": paths.file_revision(sch) if sch else None,
        },
        "files": files,
        "checks": checks,
        "exports": {
            key: {
                "returncode": value.get("returncode"),
                "output": value.get("output") or value.get("output_dir"),
                "stderr": value.get("stderr"),
            }
            for key, value in exports.items()
            if isinstance(value, dict)
        },
    }
    manifest_path = out_dir / "manifest.json"
    paths.atomic_write_text(manifest_path, json.dumps(manifest, indent=2, ensure_ascii=False))
    return {
        "manifest_path": str(manifest_path),
        "manifest": manifest,
        "checks": checks,
        "files": files,
    }


def manufacturing_profile(
    pcb_path: str | Path,
    schematic_path: str | Path | None = None,
    output_dir: str | Path | None = None,
    profile: str = "generic",
    layers: list[str] | None = None,
    include_step: bool = True,
    include_pdf: bool = True,
    include_bom: bool = True,
    refill_first: bool = False,
) -> dict:
    pcb = paths.normalize_path(pcb_path)
    if not pcb.is_file():
        raise FileNotFoundError(f"pcb not found: {pcb}")
    sch = None
    if schematic_path:
        sch = paths.normalize_path(schematic_path)
        if not sch.is_file():
            raise FileNotFoundError(f"schematic not found: {sch}")
    else:
        candidate = pcb.with_suffix(".kicad_sch")
        if candidate.is_file():
            sch = candidate

    out_dir = paths.normalize_path(output_dir) if output_dir else pcb.parent / "manufacturing"
    out_dir.mkdir(parents=True, exist_ok=True)
    layers = layers or kicad_cli.STANDARD_GERBER_LAYERS.split(",")
    layer_text = ",".join(layers)
    stem = pcb.stem
    exports = {}

    if refill_first:
        exports["refill"] = pcb_edit.refill_zones(pcb, backup=True)

    exports["gerbers"] = kicad_cli.export_gerbers(
        pcb,
        out_dir / "gerbers",
        layers=layer_text,
        profile="standard",
    )
    exports["drill"] = kicad_cli.export_drill(
        pcb,
        out_dir / "gerbers",
        separate_th=True,
        generate_report=True,
        report_path=out_dir / "drill-report.txt",
    )
    exports["pos"] = kicad_cli.export_pos(pcb, out_dir / f"{stem}-pos.csv")
    if include_bom and sch is not None:
        exports["bom"] = kicad_cli.export_bom(
            sch,
            out_dir / f"{stem}-bom.csv",
            exclude_power=True,
            exclude_dnp=True,
        )
    if include_step:
        exports["step"] = kicad_cli.export_step(pcb, out_dir / f"{stem}.step")
    if include_pdf:
        exports["pcb_pdf"] = kicad_cli.export_pdf(
            pcb,
            out_dir / f"{stem}-pcb.pdf",
            layers=layer_text,
        )
        if sch is not None:
            exports["sch_pdf"] = kicad_cli.export_pdf(sch, out_dir / f"{stem}-sch.pdf")

    manifest = _write_manifest(out_dir, profile, layers, pcb, sch, exports)
    return {
        "ok": True,
        "output_dir": str(out_dir),
        "manifest_path": manifest["manifest_path"],
        "manifest": manifest["manifest"],
        "checks": manifest["checks"],
        "files": manifest["files"],
        "exports": exports,
    }


def verify_manufacturing_output(
    output_dir: str | Path,
    manifest_path: str | Path | None = None,
) -> dict:
    root = paths.normalize_path(output_dir)
    manifest_file = (
        paths.normalize_path(manifest_path)
        if manifest_path
        else root / "manifest.json"
    )
    if not manifest_file.is_file():
        raise FileNotFoundError(f"manifest not found: {manifest_file}")
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    missing = []
    mismatched = []
    checked = 0
    for row in manifest.get("files", []):
        relative = row.get("path")
        if not relative:
            continue
        target = root / relative
        checked += 1
        if not target.is_file():
            missing.append(relative)
            continue
        actual = paths.sha256_file(target)
        if actual != row.get("sha256"):
            mismatched.append({"path": relative, "expected": row.get("sha256"), "actual": actual})
    listed = {row.get("path") for row in manifest.get("files", [])}
    listed.add(manifest_file.relative_to(root).as_posix())
    extra = [
        row["path"]
        for row in _collect_files(root)
        if row["path"] not in listed
    ]
    return {
        "ok": not missing and not mismatched,
        "manifest": str(manifest_file),
        "output_dir": str(root),
        "checked": checked,
        "missing": missing,
        "mismatched": mismatched,
        "extra": extra,
        "checks": manifest.get("checks"),
    }
