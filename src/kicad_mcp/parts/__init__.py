"""Vendor part search and BOM purchase lists.

Adapters for DigiKey Korea (API), IC Mart, DeviceMart, and LCSC (JSON API), plus
purchase-list building for BOM items. Network access is opt-in: sessions are
created lazily and only tools that call the adapters perform requests.
"""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_STATE_DIR = Path.home() / ".config" / "opencode" / "parts-purchase" / "state"


def state_dir() -> Path:
    """Directory for guest-session cookies (keeps the DeviceMart cart)."""
    return Path(os.environ.get("KICAD_MCP_PARTS_STATE_DIR", str(DEFAULT_STATE_DIR)))
