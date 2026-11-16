import os
import shutil
import tempfile
from pathlib import Path

TEST_DIR = Path(
    os.environ.get("KICAD_MCP_TEST_DIR", Path(tempfile.gettempdir()) / "kicad-mcp-test")
)
DEMO_NAME = "pic_programmer"
DEMO_SRC = Path(
    os.environ.get(
        "KICAD_DEMO_DIR",
        r"C:\Program Files\KiCad\10.0\share\kicad\demos\pic_programmer",
    )
)


def ensure_demo() -> Path:
    TEST_DIR.mkdir(parents=True, exist_ok=True)
    dest = TEST_DIR / DEMO_NAME
    if not dest.is_dir():
        if not DEMO_SRC.is_dir():
            raise FileNotFoundError(
                f"KiCad demo project not found: {DEMO_SRC}; set KICAD_DEMO_DIR"
            )
        shutil.copytree(DEMO_SRC, dest)
    return dest


DEMO_DIR = ensure_demo()
SCH = DEMO_DIR / "pic_programmer.kicad_sch"
PCB = DEMO_DIR / "pic_programmer.kicad_pcb"
