from bs4 import BeautifulSoup

from kicad_mcp.parts.sites import devicemart, digikey, icmart


def test_parse_icmart_discounts():
    node = BeautifulSoup(
        "<div>450원<br>10개 이상 430원(4% DC)<br>50개 이상 420원(7% DC)</div>",
        "html.parser",
    ).div
    assert icmart._parse_discounts(node) == [
        {"min_qty": 1, "price": 450, "discount_percent": 0},
        {"min_qty": 10, "price": 430, "discount_percent": 4},
        {"min_qty": 50, "price": 420, "discount_percent": 7},
    ]


def test_parse_icmart_discounts_split_lines():
    node = BeautifulSoup(
        "<div>450원<div>10개 이상</div><div>430원(4% DC)</div>"
        "<div>50개 이상</div><div>420원(7% DC)</div></div>",
        "html.parser",
    ).div
    assert icmart._parse_discounts(node) == [
        {"min_qty": 1, "price": 450, "discount_percent": 0},
        {"min_qty": 10, "price": 430, "discount_percent": 4},
        {"min_qty": 50, "price": 420, "discount_percent": 7},
    ]


def test_parse_icmart_no_image_card():
    anchor = BeautifulSoup(
        '<a class="premium-card" href="/products/16887">'
        '<div class="img-wrapper"><div class="no-image"><span>이미지 없음</span></div></div>'
        '<div class="info-wrapper"><h4 title="CD74HCT273M">CD74HCT273M</h4>'
        '<p class="code">11562636329</p><p class="price">0</p></div></a>',
        "html.parser",
    ).a
    item = icmart._parse_search_anchor(anchor)
    assert item["id"] == 16887
    assert item["name"] == "CD74HCT273M"
    assert item["code"] == "11562636329"
    assert item["price"] is None


def test_parse_icmart_product_id():
    assert icmart.parse_product_id("14681") == "14681"
    assert icmart.parse_product_id("https://icmart.co.kr/products/14681") == "14681"


def test_parse_devicemart_product_id():
    assert devicemart.parse_product_id("15081912") == "15081912"
    assert (
        devicemart.parse_product_id("https://www.devicemart.co.kr/goods/view?no=15081912")
        == "15081912"
    )


def test_normalize_devicemart_record():
    record = {
        "goods_seq": "15922763",
        "goods_name": "1N5824",
        "goods_code": "15922763",
        "goods_status": "runout",
        "sale_price": 209,
        "stock": "0",
        "min_purchase_ea": "5",
        "min_purchase_ea_multiple": "5",
        "manufacture": "SUNMATE Elec",
        "summary": "Diode 30V 5A Through Hole DO-201AD",
    }
    normalized = devicemart._normalize_record(record)
    assert normalized["id"] == "15922763"
    assert normalized["name"] == "1N5824"
    assert normalized["price"] == 209
    assert normalized["stock"] == 0
    assert normalized["available"] is False
    assert normalized["moq"] == 5
    assert normalized["url"].endswith("no=15922763")


def test_all_ints():
    assert devicemart._all_ints("190 원 (209원)") == [190, 209]


def test_parse_devicemart_quantity_discounts():
    text = (
        "수량 190 대량구매 혜택 "
        "50개 이상 ~ 150개 미만 : 1개당 160원 (VAT 포함 176원) "
        "150개 이상 ~ 1,000개 미만 : 1개당 140원 (VAT 포함 154원) "
        "1,000개 이상 ~ 2,000개 미만 : 1개당 120원 (VAT 포함 132원) "
        "2,000개 이상 : 1개당 110원 (VAT 포함 121원)"
    )
    assert devicemart._parse_quantity_discounts(text) == [
        {"min_qty": 50, "max_qty": 150, "price": 160},
        {"min_qty": 150, "max_qty": 1000, "price": 140},
        {"min_qty": 1000, "max_qty": 2000, "price": 120},
        {"min_qty": 2000, "max_qty": None, "price": 110},
    ]


def test_is_module():
    from kicad_mcp.parts import purchase as bom

    assert bom._is_module({"name": "Raspberry Pi Pico / Pico W"})
    assert bom._is_module({"name": "VL53L0X module"})
    assert bom._is_module({"name": "TTP223 module"})
    assert bom._is_module({"name": "WS2812B 40 LED strip"})
    assert not bom._is_module({"name": "100nF"})
    assert not bom._is_module({"name": "1N5824"})
    assert not bom._is_module({"name": "Connector 2x04"})


def test_bom_excludes_modules_by_default():
    from kicad_mcp.parts import purchase as bom

    result = bom.build_purchase_list(
        [{"name": "VL53L0X module", "qty": 6}],
        None,
        None,
        "both",
        5,
        False,
    )
    assert result["items"] == []
    assert result["excluded"] == [
        {
            "name": "VL53L0X module",
            "qty": 6,
            "reason": "Module excluded from purchase BOM",
        }
    ]
    assert result["summary"]["total_items"] == 1
    assert result["summary"]["excluded_items"] == 1
    assert result["summary"]["processed_items"] == 0


def test_bom_price_and_order_qty():
    from kicad_mcp.parts import purchase as bom

    discounts = [
        {"min_qty": 50, "max_qty": 150, "price": 160},
        {"min_qty": 150, "max_qty": 1000, "price": 140},
    ]
    assert bom._price_for_qty(discounts, 190, 50) == 160
    assert bom._price_for_qty(discounts, 190, 149) == 160
    assert bom._price_for_qty(discounts, 190, 150) == 140
    assert bom._order_qty(2, 5, 5) == 5
    assert bom._order_qty(12, 5, 5) == 15


def test_parse_digikey_product_number():
    assert digikey.parse_product_number("NE555S-13") == "NE555S-13"
    assert (
        digikey.parse_product_number(
            "https://www.digikey.kr/ko/products/detail/"
            "diodes-incorporated/NE555S-13/2921391"
        )
        == "NE555S-13"
    )


def test_normalize_digikey_product_selects_cut_tape():
    product = {
        "ManufacturerProductNumber": "NE555S-13",
        "Manufacturer": {"Name": "Diodes Incorporated"},
        "Description": {"ProductDescription": "IC OSC SNGL TIMER 500KHZ 8-SOIC"},
        "UnitPrice": 404.0,
        "QuantityAvailable": 45799,
        "ProductStatus": {"Status": "active"},
        "ProductUrl": "https://www.digikey.kr/ko/products/detail/example",
        "Discontinued": False,
        "EndOfLife": False,
        "ProductVariations": [
            {
                "DigiKeyProductNumber": "NE555S-13DITR-ND",
                "PackageType": {"Name": "TR"},
                "MinimumOrderQuantity": 2500,
                "StandardPackage": 2500,
                "QuantityAvailableforPackageType": 45000,
                "DigiReelFee": 0.0,
                "StandardPricing": [
                    {"BreakQuantity": 2500, "UnitPrice": 166.5},
                ],
            },
            {
                "DigiKeyProductNumber": "NE555S-13DICT-ND",
                "PackageType": {"Name": "CT"},
                "MinimumOrderQuantity": 1,
                "StandardPackage": 1,
                "QuantityAvailableforPackageType": 45799,
                "DigiReelFee": 0.0,
                "StandardPricing": [
                    {"BreakQuantity": 1, "UnitPrice": 404.0},
                    {"BreakQuantity": 10, "UnitPrice": 278.7},
                ],
            },
        ],
    }
    normalized = digikey._normalize_product(product)
    assert normalized["id"] == "NE555S-13"
    assert normalized["available"] is True
    assert normalized["stock"] == 45799
    assert normalized["digikey_product_number"] == "NE555S-13DICT-ND"
    assert normalized["moq"] == 1
    assert normalized["order_multiple"] == 1
    assert normalized["price_tiers"][0]["unit_price"] == 404.0
    assert digikey._price_for_qty(normalized["selected_variation"], 10) == 278.7


def test_normalize_digikey_bulk_moq_one_has_no_pack_multiple():
    product = {
        "ManufacturerProductNumber": "ECE-A1CKS101",
        "Manufacturer": {"Name": "Panasonic Industry"},
        "Description": {"ProductDescription": "CAP ALUM 100UF 20% 16V RADIAL TH"},
        "UnitPrice": 725.0,
        "QuantityAvailable": 2992,
        "ProductStatus": {"Status": "active"},
        "Discontinued": False,
        "EndOfLife": False,
        "ProductVariations": [
            {
                "DigiKeyProductNumber": "P970-ND",
                "PackageType": {"Name": "Bulk"},
                "MinimumOrderQuantity": 1,
                "StandardPackage": 200,
                "QuantityAvailableforPackageType": 2992,
                "StandardPricing": [
                    {"BreakQuantity": 1, "UnitPrice": 725.0},
                    {"BreakQuantity": 10, "UnitPrice": 452.9},
                ],
            },
        ],
    }
    normalized = digikey._normalize_product(product)
    assert normalized["moq"] == 1
    assert normalized["order_multiple"] == 1


def test_normalize_digikey_product_unavailable():
    product = {
        "ManufacturerProductNumber": "1N5824",
        "Manufacturer": {"Name": "Microchip Technology"},
        "Description": {"ProductDescription": "DIODE SCHOTTKY 30V 5A AXIAL"},
        "UnitPrice": 67380.7,
        "QuantityAvailable": None,
        "ProductStatus": {"Status": "discontinued"},
        "Discontinued": False,
        "EndOfLife": False,
        "ProductVariations": [
            {
                "DigiKeyProductNumber": "1N5824-ND",
                "PackageType": {"Name": "Bulk"},
                "MinimumOrderQuantity": 100,
                "StandardPackage": 1,
                "QuantityAvailableforPackageType": 0,
                "StandardPricing": [
                    {"BreakQuantity": 100, "UnitPrice": 67380.7},
                ],
            },
        ],
    }
    normalized = digikey._normalize_product(product)
    assert normalized["available"] is False
    assert normalized["stock"] == 0
    assert normalized["moq"] == 100
