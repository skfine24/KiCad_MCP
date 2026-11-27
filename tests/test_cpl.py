import csv
from pathlib import Path

import pytest

from kicad_mcp import cpl

PCB_TEXT = """(kicad_pcb
  (version 20260206)
  (generator "pcbnew")
  (generator_version "10.0")
  (general (thickness 1.6))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (36 "B.SilkS" user "B.Silkscreen")
    (37 "F.SilkS" user "F.Silkscreen")
    (44 "Edge.Cuts" user)
  )
  (setup (aux_axis_origin 0 0))
  (gr_rect (start 0 0) (end 40 30)
    (stroke (width 0.1) (type default)) (fill none) (layer "Edge.Cuts"))
  (footprint "Diode_SMD:D_SOD-123F"
    (layer "B.Cu")
    (at 10 10 180)
    (property "Reference" "D2" (at 0 0 0) (layer "B.SilkS")
      (effects (font (size 1 1))))
    (property "Value" "PMEG3020ER" (at 0 0 0) (layer "B.Fab")
      (effects (font (size 1 1))))
    (attr smd)
    (pad "1" smd roundrect (at -1.4 0 180) (size 1.1 1.1)
      (layers "B.Cu" "B.Mask" "B.Paste") (net "VIN"))
    (pad "2" smd roundrect (at 1.4 0 180) (size 1.1 1.1)
      (layers "B.Cu" "B.Mask" "B.Paste") (net "VBAT"))
  )
  (footprint "LED_SMD:LED_0603_1608Metric"
    (layer "F.Cu")
    (at 20 10)
    (property "Reference" "LED1" (at 0 0 0) (layer "F.SilkS")
      (effects (font (size 1 1))))
    (property "Value" "Blue" (at 0 0 0) (layer "F.Fab")
      (effects (font (size 1 1))))
    (attr smd)
    (pad "1" smd roundrect (at -0.7875 0) (size 0.8 0.9)
      (layers "F.Cu" "F.Mask" "F.Paste") (net "GND"))
    (pad "2" smd roundrect (at 0.7875 0) (size 0.8 0.9)
      (layers "F.Cu" "F.Mask" "F.Paste") (net "LED_A"))
  )
  (footprint "Package_TO_SOT_SMD:SOT-23-3"
    (layer "B.Cu")
    (at 30 10)
    (property "Reference" "Q1" (at 0 0 0) (layer "B.SilkS")
      (effects (font (size 1 1))))
    (property "Value" "AO3400A" (at 0 0 0) (layer "B.Fab")
      (effects (font (size 1 1))))
    (attr smd)
    (pad "1" smd roundrect (at -1.1375 0.95) (size 0.8 0.9)
      (layers "B.Cu" "B.Mask" "B.Paste") (net "GND"))
    (pad "2" smd roundrect (at -1.1375 -0.95) (size 0.8 0.9)
      (layers "B.Cu" "B.Mask" "B.Paste") (net "VBAT"))
    (pad "3" smd roundrect (at 1.1375 0) (size 0.8 0.9)
      (layers "B.Cu" "B.Mask" "B.Paste") (net "VIN"))
  )
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (at 5 5)
    (property "Reference" "R1" (at 0 0 0) (layer "F.SilkS")
      (effects (font (size 1 1))))
    (property "Value" "10k" (at 0 0 0) (layer "F.Fab")
      (effects (font (size 1 1))))
    (attr smd)
    (pad "1" smd roundrect (at -0.5 0) (size 0.6 0.7)
      (layers "F.Cu" "F.Mask" "F.Paste") (net "VIN"))
    (pad "2" smd roundrect (at 0.5 0) (size 0.6 0.7)
      (layers "F.Cu" "F.Mask" "F.Paste") (net "GND"))
  )
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "B.Cu")
    (at 35 20 90)
    (property "Reference" "R2" (at 0 0 0) (layer "B.SilkS")
      (effects (font (size 1 1))))
    (property "Value" "10k" (at 0 0 0) (layer "B.Fab")
      (effects (font (size 1 1))))
    (attr smd)
    (pad "1" smd roundrect (at -0.5 0 90) (size 0.6 0.7)
      (layers "B.Cu" "B.Mask" "B.Paste") (net "VIN"))
    (pad "2" smd roundrect (at 0.5 0 90) (size 0.6 0.7)
      (layers "B.Cu" "B.Mask" "B.Paste") (net "GND"))
  )
  (footprint "Connector_Molex:Molex_PicoBlade_53047-0210_1x02_P1.25mm_Vertical"
    (layer "F.Cu")
    (at 2 2)
    (property "Reference" "J1" (at 0 0 0) (layer "F.SilkS")
      (effects (font (size 1 1))))
    (property "Value" "53047-0210" (at 0 0 0) (layer "F.Fab")
      (effects (font (size 1 1))))
    (attr through_hole)
    (pad "1" thru_hole circle (at 0 -0.625) (size 1 1)
      (layers "*.Cu" "*.Mask") (net "VBAT"))
    (pad "2" thru_hole circle (at 0 0.625) (size 1 1)
      (layers "*.Cu" "*.Mask") (net "GND"))
  )
  (footprint "Diode_SMD:D_SOD-123F"
    (layer "B.Cu")
    (at 10 25)
    (dnp yes)
    (property "Reference" "D3" (at 0 0 0) (layer "B.SilkS")
      (effects (font (size 1 1))))
    (property "Value" "PMEG3020ER" (at 0 0 0) (layer "B.Fab")
      (effects (font (size 1 1))))
    (attr smd)
    (pad "1" smd roundrect (at -1.4 0) (size 1.1 1.1)
      (layers "B.Cu" "B.Mask" "B.Paste") (net "VIN"))
    (pad "2" smd roundrect (at 1.4 0) (size 1.1 1.1)
      (layers "B.Cu" "B.Mask" "B.Paste") (net "VBAT"))
  )
)
"""

BOM_TEXT = """\"Refs\",\"Value\",\"Footprint\",\"Qty\",\"DNP\"
\"D2\",\"PMEG3020ER\",\"D_SOD-123F\",\"1\",\"\"
\"LED1\",\"Blue\",\"LED_0603\",\"1\",\"\"
\"Q1\",\"AO3400A\",\"SOT-23-3\",\"1\",\"\"
\"R1\",\"10k\",\"R_0402\",\"1\",\"\"
\"R2\",\"10k\",\"R_0402\",\"1\",\"\"
"""


def _fixtures(tmp_path: Path) -> tuple[Path, Path, Path]:
    pcb = tmp_path / "test.kicad_pcb"
    pcb.write_text(PCB_TEXT, encoding="utf-8")
    bom = tmp_path / "test-bom.csv"
    bom.write_text(BOM_TEXT, encoding="utf-8")
    cpl_path = tmp_path / "test-cpl.csv"
    return pcb, bom, cpl_path


def _read_rows(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def test_export_cpl_rows_and_polarity_audit(tmp_path):
    pcb, bom, cpl_path = _fixtures(tmp_path)
    result = cpl.export_cpl(pcb, cpl_path, bom_path=bom)
    assert result["ok"], result["errors"]
    assert result["count"] == 5

    rows = {row["Designator"]: row for row in _read_rows(cpl_path)}
    assert rows["D2"]["Layer"] == "bottom"
    assert float(rows["D2"]["Rotation"]) == 0
    assert float(rows["D2"]["Mid X"]) == 10
    assert float(rows["D2"]["Mid Y"]) == -10

    assert rows["LED1"]["Layer"] == "top"
    assert float(rows["LED1"]["Rotation"]) == 0

    assert rows["Q1"]["Layer"] == "bottom"
    assert float(rows["Q1"]["Rotation"]) == 0

    assert rows["R2"]["Layer"] == "bottom"
    assert float(rows["R2"]["Rotation"]) == 90
    assert float(rows["R2"]["Mid Y"]) == -20

    assert "J1" not in rows  # THT
    assert "D3" not in rows  # DNP
    excluded = {item["ref"]: item["reason"] for item in result["report"]["excluded"]}
    assert excluded["J1"] == "through_hole"
    assert excluded["D3"] == "dnp"

    audit = {item["ref"]: item for item in result["report"]["polarity_audit"]}
    assert audit["D2"]["kind"] == "diode"
    assert audit["D2"]["pin1_name"] == "cathode (pin 1)"
    assert audit["D2"]["pad1_net"] == "VIN"
    assert audit["D2"]["pad2_net"] == "VBAT"
    assert audit["D2"]["cpl_rotation"] == 0
    assert audit["D2"]["needs_review"] is True  # no D_SOD-123F entry in the DB
    assert audit["D2"]["pad1_view_xy_mm"] == [28.6, 10]

    assert audit["LED1"]["kind"] == "led"
    assert audit["LED1"]["pad1_net"] == "GND"
    assert audit["LED1"]["pad2_net"] == "LED_A"

    assert audit["Q1"]["kind"] == "transistor"
    assert audit["Q1"]["correction_pattern"] == "^SOT-23"
    assert audit["Q1"]["needs_review"] is False


def test_verify_cpl_ok(tmp_path):
    pcb, bom, cpl_path = _fixtures(tmp_path)
    cpl.export_cpl(pcb, cpl_path, bom_path=bom)
    result = cpl.verify_cpl(pcb, cpl_path, bom_path=bom)
    assert result["ok"], result["errors"]
    assert result["origin"] == "aux"
    assert result["summary"]["matched"] == 5
    assert result["summary"]["polarity_items"] >= 3


def test_verify_cpl_rotation_mismatch(tmp_path):
    pcb, bom, cpl_path = _fixtures(tmp_path)
    cpl.export_cpl(pcb, cpl_path, bom_path=bom)
    rows = _read_rows(cpl_path)
    for row in rows:
        if row["Designator"] == "D2":
            row["Rotation"] = "180"
    with cpl_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    result = cpl.verify_cpl(pcb, cpl_path, bom_path=bom)
    assert not result["ok"]
    assert any(
        error["type"] == "rotation_mismatch" and error["ref"] == "D2"
        for error in result["errors"]
    )


def test_verify_cpl_position_and_layer_mismatch(tmp_path):
    pcb, bom, cpl_path = _fixtures(tmp_path)
    cpl.export_cpl(pcb, cpl_path, bom_path=bom)
    rows = _read_rows(cpl_path)
    for row in rows:
        if row["Designator"] == "LED1":
            row["Mid X"] = "21"
            row["Layer"] = "bottom"
    with cpl_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    result = cpl.verify_cpl(pcb, cpl_path, bom_path=bom)
    assert not result["ok"]
    types = {(error["type"], error["ref"]) for error in result["errors"]}
    assert ("position_mismatch", "LED1") in types
    assert ("layer_mismatch", "LED1") in types


def test_verify_cpl_missing_extra_duplicate(tmp_path):
    pcb, bom, cpl_path = _fixtures(tmp_path)
    cpl.export_cpl(pcb, cpl_path, bom_path=bom)
    rows = _read_rows(cpl_path)
    rows = [row for row in rows if row["Designator"] != "R1"]
    rows.append({"Designator": "X9", "Mid X": "1", "Mid Y": "1", "Rotation": "0", "Layer": "top"})
    rows.append(dict(rows[0]))
    with cpl_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    result = cpl.verify_cpl(pcb, cpl_path, bom_path=bom)
    assert not result["ok"]
    types = {(error["type"], error["ref"]) for error in result["errors"]}
    assert ("missing_in_cpl", "R1") in types
    assert ("extra_in_cpl", "X9") in types
    assert any(error["type"] == "duplicate_ref" for error in result["errors"])


def test_verify_cpl_bom_mismatch(tmp_path):
    pcb, bom, cpl_path = _fixtures(tmp_path)
    cpl.export_cpl(pcb, cpl_path, bom_path=bom)
    bom_text = "\n".join(line for line in BOM_TEXT.splitlines() if "R1" not in line) + "\n"
    bom2 = tmp_path / "test-bom-no-r1.csv"
    bom2.write_text(bom_text, encoding="utf-8")
    result = cpl.verify_cpl(pcb, cpl_path, bom_path=bom2)
    assert not result["ok"]
    assert any(
        error["type"] == "missing_in_bom" and error["ref"] == "R1"
        for error in result["errors"]
    )


def test_verify_cpl_lower_left_origin_auto(tmp_path):
    pcb, bom, cpl_path = _fixtures(tmp_path)
    cpl.export_cpl(pcb, cpl_path, bom_path=bom, origin="lower-left")
    result = cpl.verify_cpl(pcb, cpl_path, bom_path=bom, origin="auto")
    assert result["ok"], result["errors"]
    assert result["origin"] == "lower-left"


def test_verify_cpl_kicad_pos_format(tmp_path):
    pcb, _bom, cpl_path = _fixtures(tmp_path)
    pos_text = (
        "Ref,Val,Package,PosX,PosY,Rot,Side\n"
        "D2,PMEG3020ER,D_SOD-123F,10,-10,180,bottom\n"
        "LED1,Blue,LED_0603,20,-10,0,top\n"
        "Q1,AO3400A,SOT-23-3,30,-10,0,bottom\n"
        "R1,10k,R_0402,5,-5,0,top\n"
        "R2,10k,R_0402,35,-20,90,bottom\n"
    )
    cpl_path.write_text(pos_text, encoding="utf-8")
    result = cpl.verify_cpl(pcb, cpl_path, cpl_format="kicad_pos")
    assert result["ok"], result["errors"]
    assert result["format"] == "kicad_pos"
    assert result["origin"] == "aux"


def test_export_cpl_strict_raises_on_bom_mismatch(tmp_path):
    pcb, _bom, cpl_path = _fixtures(tmp_path)
    bad_bom = tmp_path / "bad-bom.csv"
    bad_bom.write_text(BOM_TEXT + "\"R9\",\"10k\",\"R_0402\",\"1\",\"\"\n", encoding="utf-8")
    with pytest.raises(RuntimeError):
        cpl.export_cpl(pcb, cpl_path, bom_path=bad_bom, strict=True)
