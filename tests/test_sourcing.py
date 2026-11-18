import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).parent))

from kicad_mcp import sch_edit, sourcing  # noqa: E402
from paths import SCH, TEST_DIR  # noqa: E402


def _component(ref, value, footprint, lib_id="Device:C", properties=None):
    return {
        "reference": ref,
        "value": value,
        "footprint": footprint,
        "lib_id": lib_id,
        "properties": properties or {},
    }


def test_package_from_footprint():
    assert sourcing._package_from_footprint("Capacitor_SMD:C_0805_2012Metric") == "0805"
    assert sourcing._package_from_footprint("Resistor_SMD:R_1210_3225Metric") == "1210"
    assert sourcing._package_from_footprint("Package_TO_SOT_SMD:SOT-23-6") == "SOT-23-6"
    assert sourcing._package_from_footprint("Buzzer_Beeper:Buzzer_12x9.5RM7.6") == "12x9.5"
    assert sourcing._package_from_footprint("AIRGO:YK16S") == "14P"
    assert sourcing._package_from_footprint("Connector_JST:JST_XH_B2B-XH-A_1x02_P2.50mm_Vertical") == "JST XH 2.5mm"


def test_group_components():
    components = [
        _component("R1", "5.1k", "Resistor_SMD:R_0805_2012Metric"),
        _component("R2", "5.1k", "Resistor_SMD:R_0805_2012Metric"),
        _component(
            "U1",
            "ESP32-S3-WROOM-1U",
            "RF_Module:ESP32-S3-WROOM-1U",
            properties={"LCSC": "C3013946"},
        ),
    ]
    groups = sourcing._group_components(components)
    assert len(groups) == 2
    resistor = next(group for group in groups if group["refs"] == ["R1", "R2"])
    assert resistor["package"] == "0805"
    assert len(resistor["refs"]) == 2
    esp = next(group for group in groups if group["refs"] == ["U1"])
    assert esp["lcsc_code"] == "C3013946"


def test_query_for_group():
    assert (
        sourcing._query_for_group(
            {
                "lcsc_code": None,
                "mpn": None,
                "value": "5.1k",
                "package": "0805",
                "lib_ids": [],
            }
        )
        == "5.1k 0805"
    )
    assert (
        sourcing._query_for_group(
            {
                "lcsc_code": "C3013946",
                "mpn": None,
                "value": "ESP32-S3-WROOM-1U",
                "package": None,
                "lib_ids": [],
            }
        )
        == "C3013946"
    )
    assert (
        sourcing._query_for_group(
            {
                "lcsc_code": None,
                "mpn": "PS-5850A-6PL",
                "value": "POWER_SW",
                "package": "5.8x5.8 6P",
                "lib_ids": [],
            }
        )
        == "PS-5850A-6PL"
    )
    assert (
        sourcing._query_for_group(
            {
                "lcsc_code": None,
                "mpn": None,
                "value": "YK16S_L",
                "package": "14P",
                "lib_ids": [],
            }
        )
        == "YK16S"
    )


def test_select_candidate_statuses():
    group = {
        "refs": ["R1", "R2"],
        "lcsc_code": "C25804",
        "mpn": None,
        "package": "0805",
        "value": "5.1k",
        "footprint": "Resistor_SMD:R_0805_2012Metric",
    }
    candidates = [{"code": "C25804", "model": "0805W8F1001T5E", "stock": 100}]
    selected, status, _ = sourcing._select_candidate(candidates, group)
    assert selected["code"] == "C25804"
    assert status == "orderable"

    candidates = [{"code": "C25804", "model": "0805W8F1001T5E", "stock": 1}]
    selected, status, _ = sourcing._select_candidate(candidates, group)
    assert status == "low_stock"

    candidates = [{"code": "C25804", "model": "0805W8F1001T5E", "stock": 0}]
    selected, status, _ = sourcing._select_candidate(candidates, group)
    assert status == "not_orderable"

    group = {
        "refs": ["U1"],
        "lcsc_code": None,
        "mpn": "ESP32-S3-WROOM-1U",
        "package": None,
        "value": "ESP32-S3-WROOM-1U",
        "footprint": "RF_Module:ESP32-S3-WROOM-1U",
    }
    candidates = [
        {"code": "C3013946", "model": "ESP32-S3-WROOM-1U-N16R8", "stock": 100}
    ]
    selected, status, _ = sourcing._select_candidate(candidates, group)
    assert status == "orderable"

    candidates = [{"code": "C111916", "model": "AOZ1282CI", "stock": 100}]
    selected, status, _ = sourcing._select_candidate(candidates, group)
    assert status == "needs_review"


def test_select_candidate_passive_generic():
    group = {
        "refs": ["R1"],
        "lcsc_code": None,
        "mpn": None,
        "package": "0805",
        "value": "5.1k",
        "footprint": "Resistor_SMD:R_0805_2012Metric",
    }
    candidates = [
        {
            "code": "C27834",
            "model": "0805W8F5101T5E",
            "stock": 100,
            "package": "0805",
            "description": "5.1kΩ ±1% 125mW 0805 Thick Film Resistor",
            "catalog": "Chip Resistor - Surface Mount",
        }
    ]
    selected, status, _ = sourcing._select_candidate(candidates, group)
    assert selected["code"] == "C27834"
    assert status == "orderable"

    candidates = [
        {
            "code": "C17414",
            "model": "0805W8F1002T5E",
            "stock": 100,
            "package": "0805",
            "description": "10kΩ ±1% 125mW 0805 Thick Film Resistor",
            "catalog": "Chip Resistor - Surface Mount",
        }
    ]
    selected, status, _ = sourcing._select_candidate(candidates, group)
    assert status == "needs_review"


def test_select_candidate_non_passive_generic_needs_review():
    group = {
        "refs": ["J1"],
        "lcsc_code": None,
        "mpn": None,
        "package": "USB-C",
        "value": "USB-C",
        "footprint": "Connector_USB:USB_C_Receptacle_G-Switch_GT-USB-7010ASV",
    }
    candidates = [
        {
            "code": "C165948",
            "model": "TYPE-C-31-M-12",
            "stock": 100,
            "package": "SMD",
            "description": "USB-C Receptacle Connector 16 Position",
            "catalog": "USB Connectors",
        }
    ]
    selected, status, _ = sourcing._select_candidate(candidates, group)
    assert status == "needs_review"


def test_sch_set_properties_roundtrip():
    target = TEST_DIR / "sourcing-properties.kicad_sch"
    shutil.copy2(SCH, target)
    _, components = sourcing._read_components(target)
    ref = components[0]["reference"]
    result = sch_edit.sch_set_properties(
        target,
        [{"ref": ref, "properties": {"LCSC": "C25804", "MPN": "TEST-MPN"}}],
        backup=False,
    )
    assert result["updated"] == 1
    _, updated = sourcing._read_components(target)
    component = next(item for item in updated if item["reference"] == ref)
    assert component["properties"]["LCSC"] == "C25804"
    assert component["properties"]["MPN"] == "TEST-MPN"


def test_apply_lcsc_bom_to_schematic():
    target = TEST_DIR / "sourcing-bom.kicad_sch"
    shutil.copy2(SCH, target)
    _, components = sourcing._read_components(target)
    ref = components[0]["reference"]
    bom = TEST_DIR / "sourcing-bom.csv"
    bom.write_text(
        "Designator,LCSC Part #,MPN,Manufacturer\n"
        f"{ref},C25804,0805W8F1001T5E,UNI-ROYAL\n",
        encoding="utf-8",
    )
    result = sourcing.apply_lcsc_bom_to_schematic(target, bom, backup=False)
    assert result["updated"] == 1
    _, updated = sourcing._read_components(target)
    component = next(item for item in updated if item["reference"] == ref)
    assert component["properties"]["LCSC"] == "C25804"
    assert component["properties"]["MPN"] == "0805W8F1001T5E"
    assert component["properties"]["Manufacturer"] == "UNI-ROYAL"
