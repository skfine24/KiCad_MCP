from __future__ import annotations

import json

from kicad_mcp.parts import state_dir as parts_state_dir
from kicad_mcp.parts.sites import devicemart, digikey, icmart


def main() -> int:
    state_dir = parts_state_dir()
    ic_session = icmart.make_session()
    dm_session = devicemart.make_session(state_dir)
    dk_session = digikey.make_session()
    try:
        ic_search = icmart.search(ic_session, "100nF", limit=5)
        print("IC Mart search:", json.dumps(ic_search, ensure_ascii=False, indent=2)[:2000])

        ic_product = icmart.product(ic_session, "14681")
        print("IC Mart product:", json.dumps(ic_product, ensure_ascii=False, indent=2)[:2000])

        dm_search = devicemart.search(dm_session, "NE555", limit=5)
        print("DeviceMart search:", json.dumps(dm_search, ensure_ascii=False, indent=2)[:2000])

        dm_product = devicemart.product(dm_session, "15081912")
        print("DeviceMart product:", json.dumps(dm_product, ensure_ascii=False, indent=2)[:2000])

        dk_search = digikey.search(dk_session, "NE555", limit=5)
        print("DigiKey search:", json.dumps(dk_search, ensure_ascii=False, indent=2)[:2000])

        dk_product = digikey.product(dk_session, "NE555S-13")
        print("DigiKey product:", json.dumps(dk_product, ensure_ascii=False, indent=2)[:2000])

        if not ic_search.get("items") or not dm_search.get("items"):
            print("Smoke test failed: one or both searches returned no items")
            return 1
        if not ic_product.get("price") or dm_product.get("stock") is None:
            print("Smoke test failed: product details incomplete")
            return 1
        if not dk_search.get("items") or not dk_product.get("available"):
            print("Smoke test failed: DigiKey search or product unavailable")
            return 1
        print("Smoke test passed")
        return 0
    finally:
        ic_session.close()
        dm_session.close()
        dk_session.close()


if __name__ == "__main__":
    raise SystemExit(main())
