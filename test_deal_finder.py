import unittest

from deal_finder import (
    CONFIG_PATH,
    PRICES_PATH,
    build_alert_text,
    build_listing_analysis,
    dedupe_keys_for_listing,
    extract_apple_watch_model_key,
    extract_iphone_model_key,
    extract_samsung_model_key,
    extract_facebook_description_lines,
    load_config,
    load_price_catalog,
)
from generate_prices_csv import parse_deduction_notes


class DealFinderRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = load_config(CONFIG_PATH)
        cls.catalog = load_price_catalog(PRICES_PATH, cls.config["pricing"])
        cls.iphone_search = {
            "name": "Facebook Windsor iPhone",
            "url": "https://www.facebook.com/marketplace/windsor/search?query=iphone",
            "platform": "facebook",
        }
        cls.watch_search = {
            "name": "Facebook Windsor Apple Watch",
            "url": "https://www.facebook.com/marketplace/windsor/search?query=apple%20watch",
            "platform": "facebook",
        }

    def test_replacement_screen_is_rejected_as_part_listing(self):
        listing = {
            "platform": "facebook",
            "id": "replacement-screen-1",
            "url": "https://www.facebook.com/marketplace/item/replacement-screen-1/",
            "title": "iPhone 15 Pro replacement screen",
            "description": "OEM replacement screen only, not phone",
            "price": 120,
            "image_urls": [],
        }
        analysis = build_listing_analysis(listing, self.iphone_search, self.catalog, self.config, {"iphone"})
        self.assertIsNone(analysis)

    def test_bh_shorthand_triggers_battery_deduction(self):
        listing = {
            "platform": "facebook",
            "id": "battery-health-1",
            "url": "https://www.facebook.com/marketplace/item/battery-health-1/",
            "title": "iPhone 14 Pro 128GB bh 79 unlocked",
            "description": "Face id works good",
            "price": 500,
            "image_urls": [],
        }
        analysis = build_listing_analysis(listing, self.iphone_search, self.catalog, self.config, {"iphone"})
        self.assertIsNotNone(analysis)
        self.assertEqual(analysis["model"], "iphone 14 pro")
        self.assertEqual(analysis["battery_health"], 79)
        self.assertIn("battery health under 80 percent", analysis["defects"])
        self.assertEqual(analysis["condition_key"], "c")
        self.assertTrue(any("battery health under 80 percent" in item for item in analysis["pricing_adjustments"]))
        self.assertEqual(analysis["sell_price"], round(analysis["sheet_price_usd"] * 1.4, 2))
        self.assertEqual(analysis["max_buy_price"], round(analysis["sheet_price_usd"] * 1.12, 2))

    def test_component_failure_does_not_mark_phone_as_not_working(self):
        listing = {
            "platform": "facebook",
            "id": "component-failure-1",
            "url": "https://www.facebook.com/marketplace/item/component-failure-1/",
            "title": "iPhone 14 Pro Max",
            "description": "back glass cracked, bh 79, face id doesnt work",
            "price": 400,
            "image_urls": [],
        }
        analysis = build_listing_analysis(listing, self.iphone_search, self.catalog, self.config, {"iphone"})
        self.assertIsNotNone(analysis)
        self.assertEqual(analysis["model"], "iphone 14 pro max")
        self.assertEqual(analysis["battery_health"], 79)
        self.assertIn("cracked back", analysis["defects"])
        self.assertIn("bad Face ID", analysis["defects"])
        self.assertNotIn("not working", analysis["defects"])

    def test_example1_exact_battery_health_and_missing_sim_tray_are_kept_in_notes(self):
        listing = {
            "platform": "facebook",
            "id": "2132277774272934",
            "url": "https://www.facebook.com/marketplace/item/2132277774272934/",
            "title": "iPhone 14 - 128 gb - Unlocked",
            "description": (
                "Fully functional & Unlocked to all carriers\n"
                "Screen is in great shape and has protector, but back is cracked. Comes with case. "
                "Has 128 gb of storage, battery health is at 65%. Missing sim tray"
            ),
            "price": 0,
            "image_urls": [],
        }
        analysis = build_listing_analysis(listing, self.iphone_search, self.catalog, self.config, {"iphone"})
        self.assertIsNotNone(analysis)
        self.assertEqual(analysis["storage"], "128gb")
        self.assertEqual(analysis["battery_health"], 65)
        self.assertIn("cracked back", analysis["defects"])
        self.assertIn("missing SIM tray", analysis["defects"])
        self.assertEqual(
            analysis["condition_notes"],
            "cracked back, battery health 65%, missing SIM tray",
        )

    def test_example6_back_glass_only_listing_is_rejected(self):
        listing = {
            "platform": "facebook",
            "id": "889960894012628",
            "url": "https://www.facebook.com/marketplace/item/889960894012628/",
            "title": "Back Glass OEM iPhones",
            "description": "DM for price quotes",
            "price": 0,
            "image_urls": [],
        }
        analysis = build_listing_analysis(listing, self.iphone_search, self.catalog, self.config, {"iphone"})
        self.assertIsNone(analysis)

    def test_example7_back_crack_and_scratches_do_not_become_cracked_screen(self):
        listing = {
            "platform": "facebook",
            "id": "1615960369552427",
            "url": "https://www.facebook.com/marketplace/item/1615960369552427/",
            "title": "iPhone 15 Plus",
            "description": (
                "Back cracked\n"
                "Screen is scratched up, has line on side\n"
                "Camera is glitchy\n"
                "Frame is pretty dinged up"
            ),
            "price": 0,
            "image_urls": [],
        }
        analysis = build_listing_analysis(listing, self.iphone_search, self.catalog, self.config, {"iphone"})
        self.assertIsNotNone(analysis)
        self.assertEqual(analysis["model"], "iphone 15 plus")
        self.assertIn("cracked back", analysis["defects"])
        self.assertNotIn("cracked screen", analysis["defects"])
        self.assertTrue(any("cracked back" in item for item in analysis["pricing_adjustments"]))
        self.assertIn("cracked back", analysis["condition_notes"])
        self.assertTrue(
            "scratched" in analysis["condition_notes"]
        )
        alert_text = build_alert_text(self.iphone_search["name"], listing, analysis)
        self.assertIn("Condition/Notes -", alert_text)
        self.assertIn("deduction required", alert_text)

    def test_unsupported_apple_watch_series_is_rejected_instead_of_cross_matching(self):
        listing = {
            "platform": "facebook",
            "id": "757567504085941",
            "url": "https://www.facebook.com/marketplace/item/757567504085941/",
            "title": "Apple Watch Series 3 42mm",
            "description": "75% battery health as depicted in image",
            "price": 0,
            "image_urls": [],
        }
        analysis = build_listing_analysis(listing, self.watch_search, self.catalog, self.config, {"apple_watch"})
        self.assertIsNone(analysis)

    def test_supported_apple_watch_includes_exact_battery_health_note(self):
        listing = {
            "platform": "facebook",
            "id": "watch-battery-1",
            "url": "https://www.facebook.com/marketplace/item/watch-battery-1/",
            "title": "Apple Watch Series 10 42mm",
            "description": "75% battery health as depicted in image. Works great.",
            "price": 0,
            "image_urls": [],
        }
        analysis = build_listing_analysis(listing, self.watch_search, self.catalog, self.config, {"apple_watch"})
        self.assertIsNotNone(analysis)
        self.assertEqual(analysis["battery_health"], 75)
        self.assertIn("battery health 75%", analysis["condition_notes"])

    def test_parse_deduction_notes_keeps_flat_rules_and_condition_override(self):
        rules = parse_deduction_notes(
            "Cracked Back = $30 off / Cracked Lens = $20 off / "
            "Bad Face ID = Grade D / Bad Back Camera = $30 off - - - "
            "HEAVY SCRATCHING / DEGRADED BATTERY % / Repair Message(s) = EXTRA DEDUCTION"
        )
        self.assertEqual(rules["cracked_back"], {"kind": "flat_usd", "value": 30.0})
        self.assertEqual(rules["cracked_lens"], {"kind": "flat_usd", "value": 20.0})
        self.assertEqual(rules["bad_face_id"], {"kind": "condition_override", "value": "d"})
        self.assertEqual(rules["bad_back_camera"], {"kind": "flat_usd", "value": 30.0})

    def test_dedupe_prefers_stable_id_and_url(self):
        keys = dedupe_keys_for_listing(
            {
                "platform": "facebook",
                "id": "123456789",
                "url": "https://www.facebook.com/marketplace/item/123456789/?ref=search",
                "title": "iPhone 14 Pro",
                "description": "128GB unlocked",
            }
        )
        self.assertEqual(len(keys), 2)
        self.assertTrue(any(key.startswith("facebook::id::123456789") for key in keys))
        self.assertTrue(any(key.startswith("facebook::url::") for key in keys))
        self.assertFalse(any("::fp::" in key for key in keys))

    def test_case_listing_is_rejected_as_accessory(self):
        listing = {
            "platform": "kijiji",
            "id": "case-1",
            "url": "https://www.kijiji.ca/v-cell-phone/windsor-area-on/iphone-13-cover-brown-bear/1716445788",
            "title": "iPhone 13 Cover Brown Bear",
            "description": "Case only, fits iPhone 13",
            "price": 5,
            "image_urls": [],
        }
        analysis = build_listing_analysis(listing, self.iphone_search, self.catalog, self.config, {"iphone"})
        self.assertIsNone(analysis)

    def test_cover_with_holder_listing_is_rejected_as_accessory(self):
        listing = {
            "platform": "kijiji",
            "id": "case-2",
            "url": "https://www.kijiji.ca/v-cell-phone/windsor-area-on/iphone-13-cover-with-holder/1716445930",
            "title": "iPhone 13 COVER with Holder",
            "description": "Accessory only",
            "price": 5,
            "image_urls": [],
        }
        analysis = build_listing_analysis(listing, self.iphone_search, self.catalog, self.config, {"iphone"})
        self.assertIsNone(analysis)

    def test_supported_series_9_watch_without_size_uses_supported_floor_row_and_keeps_not_working(self):
        listing = {
            "platform": "facebook",
            "id": "watch-series-9-1",
            "url": "https://www.facebook.com/marketplace/item/1569681794287601/",
            "title": "Series 9 Apple Watch",
            "description": "not working",
            "price": 100,
            "image_urls": [],
        }
        analysis = build_listing_analysis(listing, self.watch_search, self.catalog, self.config, {"apple_watch"})
        self.assertIsNotNone(analysis)
        self.assertEqual(analysis["model"], "apple watch series 9 41 mm")
        self.assertEqual(analysis["sell_price"], 126.0)
        self.assertEqual(analysis["max_buy_price"], 100.8)
        self.assertEqual(analysis["condition_notes"], "not working")

    def test_supported_samsung_s22_matches_sheet_row(self):
        search = {
            "name": "Facebook Windsor Samsung Phone",
            "url": "https://www.facebook.com/marketplace/windsor/search?query=samsung%20phone",
            "platform": "facebook",
        }
        listing = {
            "platform": "facebook",
            "id": "s22-1",
            "url": "https://www.facebook.com/marketplace/item/1191180472966969/",
            "title": "Unlocked Phantom Black Samsung Galaxy S22 5G 128GB",
            "description": "",
            "price": 250,
            "image_urls": [],
        }
        analysis = build_listing_analysis(listing, search, self.catalog, self.config, {"samsung"})
        self.assertIsNotNone(analysis)
        self.assertEqual(analysis["matched_csv_row"], "galaxy_s_22__unlocked")
        self.assertEqual(analysis["sell_price"], 112.0)
        self.assertEqual(analysis["max_buy_price"], 89.6)

    def test_unsupported_samsung_a14_is_rejected(self):
        search = {
            "name": "Facebook Windsor Samsung Phone",
            "url": "https://www.facebook.com/marketplace/windsor/search?query=samsung%20phone",
            "platform": "facebook",
        }
        listing = {
            "platform": "facebook",
            "id": "a14-1",
            "url": "https://www.facebook.com/marketplace/item/2190124658058697/",
            "title": "Unlocked Samsung Galaxy A14 5G 64GB",
            "description": "",
            "price": 150,
            "image_urls": [],
        }
        self.assertEqual(extract_samsung_model_key(listing["title"]), "galaxy_a_14")
        self.assertNotIn("galaxy_a_14", self.catalog["supported_samsung_model_keys"])
        analysis = build_listing_analysis(listing, search, self.catalog, self.config, {"samsung"})
        self.assertIsNone(analysis)

    def test_generic_free_phone_post_is_rejected_without_supported_device_signal(self):
        listing = {
            "platform": "facebook",
            "id": "free-phone-1",
            "url": "https://www.facebook.com/marketplace/item/free-phone-1/",
            "title": "Roger is giving away new phone",
            "description": "Message for details",
            "price": 0,
            "image_urls": [],
        }
        analysis = build_listing_analysis(listing, self.watch_search, self.catalog, self.config, {"apple_watch"})
        self.assertIsNone(analysis)

    def test_samsung_fe_listing_does_not_cross_match_plain_model(self):
        search = {
            "name": "Facebook Windsor Samsung Phone",
            "url": "https://www.facebook.com/marketplace/windsor/search?query=samsung%20phone",
            "platform": "facebook",
        }
        listing = {
            "platform": "facebook",
            "id": "s24-fe-1",
            "url": "https://www.facebook.com/marketplace/item/s24-fe-1/",
            "title": "Samsung S24 FE",
            "description": "128GB unlocked",
            "price": 345,
            "image_urls": [],
        }
        self.assertEqual(extract_samsung_model_key(listing["title"]), "galaxy_s_24_fe")
        self.assertIn("galaxy_s_24", self.catalog["supported_samsung_model_keys"])
        self.assertNotIn("galaxy_s_24_fe", self.catalog["supported_samsung_model_keys"])
        analysis = build_listing_analysis(listing, search, self.catalog, self.config, {"samsung"})
        self.assertIsNone(analysis)

    def test_supported_samsung_fe_listing_matches_exact_fe_row(self):
        search = {
            "name": "Facebook Windsor Samsung Phone",
            "url": "https://www.facebook.com/marketplace/windsor/search?query=samsung%20phone",
            "platform": "facebook",
        }
        listing = {
            "platform": "facebook",
            "id": "s25-fe-1",
            "url": "https://www.facebook.com/marketplace/item/s25-fe-1/",
            "title": "Samsung S25 FE",
            "description": "Unlocked and in good condition",
            "price": 20,
            "image_urls": [],
        }
        self.assertEqual(extract_samsung_model_key(listing["title"]), "galaxy_s_25_fe")
        self.assertIn("galaxy_s_25_fe", self.catalog["supported_samsung_model_keys"])
        analysis = build_listing_analysis(listing, search, self.catalog, self.config, {"samsung"})
        self.assertIsNotNone(analysis)
        self.assertEqual(analysis["matched_csv_row"], "galaxy_s_25_fe__unlocked")
        self.assertEqual(analysis["matched_label"], "galaxy s 25 fe")
        self.assertEqual(analysis["sell_price"], 56.0)
        self.assertEqual(analysis["max_buy_price"], 44.8)

    def test_iphone_15_pro_with_case_bundle_still_matches_iphone_family(self):
        listing = {
            "platform": "facebook",
            "id": "iphone-15-pro-case-1",
            "url": "https://www.facebook.com/marketplace/item/986877207124531/",
            "title": "iPhone 15 pro + case",
            "description": "good condition",
            "price": 599,
            "image_urls": [],
        }
        self.assertEqual(extract_iphone_model_key(listing["title"]), "iphone_15_pro")
        analysis = build_listing_analysis(listing, self.iphone_search, self.catalog, self.config, {"iphone"})
        self.assertIsNotNone(analysis)
        self.assertEqual(analysis["matched_csv_row"], "iphone_15_pro__unlocked")
        self.assertEqual(analysis["matched_label"], "iphone 15 pro")
        self.assertEqual(analysis["storage"], "128gb")
        self.assertEqual(analysis["storage_source"], "unspecified")
        self.assertEqual(analysis["sell_price"], 504.0)
        self.assertEqual(analysis["max_buy_price"], 403.2)

    def test_iphone_listing_with_invalid_storage_falls_back_within_family(self):
        listing = {
            "platform": "facebook",
            "id": "iphone-15-pro-storage-1",
            "url": "https://www.facebook.com/marketplace/item/iphone-15-pro-storage-1/",
            "title": "iPhone 15 Pro",
            "description": "16gb good condition",
            "price": 500,
            "image_urls": [],
        }
        analysis = build_listing_analysis(listing, self.iphone_search, self.catalog, self.config, {"iphone"})
        self.assertIsNotNone(analysis)
        self.assertEqual(analysis["matched_csv_row"], "iphone_15_pro__unlocked")
        self.assertEqual(analysis["storage"], "128gb")
        self.assertEqual(analysis["storage_source"], "family_floor")

    def test_iphone12_normalizes_and_does_not_cross_match_pro_max(self):
        listing = {
            "platform": "facebook",
            "id": "iphone12-compact-1",
            "url": "https://www.facebook.com/marketplace/item/1207434570923992/",
            "title": "Iphone12",
            "description": "128gb good condition",
            "price": 150,
            "image_urls": [],
        }
        self.assertEqual(extract_iphone_model_key(listing["title"]), "iphone_12")
        analysis = build_listing_analysis(listing, self.iphone_search, self.catalog, self.config, {"iphone"})
        self.assertIsNotNone(analysis)
        self.assertEqual(analysis["matched_csv_row"], "iphone_12__unlocked")
        self.assertEqual(analysis["matched_label"], "iphone 12")
        self.assertEqual(analysis["sell_price"], 189.0)
        self.assertEqual(analysis["max_buy_price"], 151.2)

    def test_iphone11_does_not_fall_through_to_macbook_thresholds(self):
        listing = {
            "platform": "facebook",
            "id": "iphone11-basic-1",
            "url": "https://www.facebook.com/marketplace/item/951954941128641/",
            "title": "iPhone 11",
            "description": "good condition",
            "price": 175,
            "image_urls": [],
        }
        self.assertEqual(extract_iphone_model_key(listing["title"]), "iphone_11")
        analysis = build_listing_analysis(listing, self.iphone_search, self.catalog, self.config, {"iphone"})
        self.assertIsNotNone(analysis)
        self.assertEqual(analysis["matched_csv_row"], "iphone_11__unlocked")
        self.assertEqual(analysis["matched_label"], "iphone 11")
        self.assertEqual(analysis["storage"], "64gb")
        self.assertEqual(analysis["storage_source"], "unspecified")
        self.assertEqual(analysis["sell_price"], 147.0)
        self.assertEqual(analysis["max_buy_price"], 117.6)

    def test_unsupported_apple_watch_three_is_rejected_before_pricing(self):
        listing = {
            "platform": "facebook",
            "id": "watch-3-bundle-1",
            "url": "https://www.facebook.com/marketplace/item/772870018927378/",
            "title": "Apple Watch 3 with maroon band and charger",
            "description": "good condition",
            "price": 80,
            "image_urls": [],
        }
        self.assertEqual(extract_apple_watch_model_key(listing["title"]), "apple_watch_series_3")
        self.assertNotIn("apple_watch_series_3", self.catalog["supported_model_keys_by_category"]["apple_watch"])
        analysis = build_listing_analysis(listing, self.watch_search, self.catalog, self.config, {"apple_watch"})
        self.assertIsNone(analysis)

    def test_extract_facebook_description_lines_prefers_full_description_block(self):
        body_text = (
            "Marketplace\n"
            "$280\n"
            "iPhone 15 Plus\n"
            "Description\n"
            "Back cracked\n"
            "Screen is scratched up, has line on side\n"
            "Camera is glitchy\n"
            "Frame is pretty dinged up\n"
            "Seller information\n"
            "Member since 2020\n"
        )
        lines = extract_facebook_description_lines(
            body_text,
            title="iPhone 15 Plus",
            meta_description="iPhone 15 Plus listed for $280",
        )
        self.assertEqual(
            lines,
            [
                "Back cracked",
                "Screen is scratched up, has line on side",
                "Camera is glitchy",
                "Frame is pretty dinged up",
            ],
        )


if __name__ == "__main__":
    unittest.main()
