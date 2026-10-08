import unittest

from model import WatchConfig, evaluate_round, parse_product, time_matches


PRODUCT = {
    "productId": 10367229, "productName": "화담숲",
    "productOptionGroups": [{
        "productOptionGroupId": 10480706,
        "productOptionGroupAttributeTypeCode": "HWADAMSUP",
        "reservationStartDate": "2026-10-23", "reservationEndDate": "2026-11-15",
        "quantityPerPerson": 6,
        "productOptions": [{
            "productOptionId": 10652041, "productOptionAttributeCode": "AGE",
            "productOptionItems": [
                {"productOptionItemId": 14253581, "productOptionAttributeDetailCode": "ADULT", "productOptionItemName": "성인"},
                {"productOptionItemId": 14253582, "productOptionAttributeDetailCode": "SENIOR", "productOptionItemName": "경로"},
                {"productOptionItemId": 14253584, "productOptionAttributeDetailCode": "CHILD", "productOptionItemName": "어린이"},
            ],
        }],
    }],
}


class AvailabilityTests(unittest.TestCase):
    def setUp(self):
        self.config = WatchConfig()
        self.group = parse_product(PRODUCT).groups["HWADAMSUP"]

    def items(self, status="IN_SALE"):
        return [{"productOptionItemId": value, "productOptionItemStatusCode": status,
                 "roundStatusCode": "IN_SALE", "inventoryQuantity": 1}
                for value in (14253581, 14253582, 14253584)]

    def test_one_shared_ticket_alerts_for_requested_senior_without_counting_three(self):
        result = evaluate_round({"time": "08:00", "roundStatusCode": "IN_SALE", "inventoryQuantity": 1},
                                self.items(), self.group, self.config, "entry")
        self.assertTrue(result.available)
        self.assertEqual(result.quantity, 1)
        self.assertEqual(result.eligible_ages, ("ADULT", "SENIOR", "CHILD"))
        self.assertFalse(result.full_target)

    def test_each_requested_age_can_trigger_a_partial_alert(self):
        for item_id, age in ((14253581, "ADULT"), (14253582, "SENIOR"), (14253584, "CHILD")):
            with self.subTest(age=age):
                items = [{"productOptionItemId": item_id, "productOptionItemStatusCode": "IN_SALE"}]
                result = evaluate_round({"time": "08:20", "roundStatusCode": "IN_SALE", "inventoryQuantity": 1},
                                        items, self.group, self.config, "entry")
                self.assertTrue(result.available)
                self.assertEqual(result.eligible_ages, (age,))

    def test_sold_out_zero_or_unknown_round_never_alerts(self):
        for raw in ({"time":"08:00", "roundStatusCode":"SOLD_OUT", "inventoryQuantity":8},
                    {"time":"08:00", "roundStatusCode":"IN_SALE", "inventoryQuantity":0},
                    {"time":"08:00", "inventoryQuantity":8},
                    {"time":"08:00", "roundStatusCode":"END_OF_SALE", "inventoryQuantity":8}):
            with self.subTest(raw=raw):
                self.assertFalse(evaluate_round(raw, self.items(), self.group, self.config, "entry").available)

    def test_unconfirmed_or_sold_out_age_never_alerts(self):
        for items in ([], [{"productOptionItemId":14253581}], self.items("SOLD_OUT")):
            self.assertFalse(evaluate_round({"time":"08:00", "roundStatusCode":"IN_SALE", "inventoryQuantity":6},
                                             items, self.group, self.config, "entry").available)

    def test_zero_goal_age_is_excluded(self):
        config = WatchConfig(counts={"ADULT":0,"SENIOR":2,"TEEN":0,"CHILD":0})
        result = evaluate_round({"time":"08:00","roundStatusCode":"IN_SALE","inventoryQuantity":1},
                                [{"productOptionItemId":14253581,"productOptionItemStatusCode":"IN_SALE"}],
                                self.group, config, "entry")
        self.assertFalse(result.available)

    def test_unknown_explicit_status_or_disabled_age_never_alerts(self):
        for extra in ({'productOptionItemStatusCode':'NEW_STATUS'},
                      {'roundStatusCode':'NEW_STATUS'}, {'isDisabled':True}):
            item={'productOptionItemId':14253581,'productOptionItemStatusCode':'IN_SALE','roundStatusCode':'IN_SALE',**extra}
            with self.subTest(extra=extra):
                result=evaluate_round({'time':'08:00','roundStatusCode':'IN_SALE','inventoryQuantity':6},
                                      [item],self.group,self.config,'entry')
                self.assertFalse(result.available)

    def test_inventory_not_published_does_not_claim_full_target(self):
        result = evaluate_round({"time":"08:00","roundStatusCode":"IN_SALE","inventoryQuantity":None},
                                self.items(), self.group, self.config, "entry")
        self.assertTrue(result.available)
        self.assertIsNone(result.quantity)
        self.assertFalse(result.full_target)

    def test_full_target_requires_all_selected_ages_and_six_shared_tickets(self):
        items = [{"productOptionItemId": value,"productOptionItemStatusCode":"IN_SALE"}
                 for value in (14253581,14253582,14253584)]
        result = evaluate_round({"time":"08:00","roundStatusCode":"IN_SALE","inventoryQuantity":6},
                                items,self.group,self.config,"entry")
        self.assertTrue(result.full_target)


class SettingsValidationTests(unittest.TestCase):
    def test_range_includes_both_endpoints_only(self):
        for time, expected in (("08:00",True),("08:20",True),("07:59",False),("08:21",False)):
            self.assertEqual(time_matches(time,("08:00~08:20",)),expected)

    def test_rejects_invalid_date_time_counts_and_non_yanolja_url(self):
        cases = ({"date":"2026-02-30"}, {"time_specs":("25:00",)},
                 {"time_specs":("09:00~08:00",)}, {"counts":{"ADULT":-1}},
                 {"counts":{"ADULT":0,"SENIOR":0,"CHILD":0}},
                 {"product_url":"https://example.com/leisure/10367229"},
                 {"interval":float("nan")})
        for values in cases:
            with self.subTest(values=values), self.assertRaises(ValueError):
                WatchConfig(**values).validate()

    def test_addon_requires_real_order_query_and_matching_yanolja_host(self):
        for url in ("", "https://example.com/?orderGroupId=abc", "https://leisure-web.yanolja.com/leisure/1"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                WatchConfig(mode="addon",addon_url=url).validate()

    def test_product_switch_uses_dynamic_option_ids_and_purchase_limit(self):
        product = dict(PRODUCT,productId=10323843,productOptionGroups=[dict(
            PRODUCT["productOptionGroups"][0],productOptionGroupId=10473187,quantityPerPerson=50,
            productOptions=[dict(PRODUCT["productOptionGroups"][0]["productOptions"][0],productOptionId=10643310)])])
        group = parse_product(product).groups["HWADAMSUP"]
        self.assertEqual((group.id,group.age_option_id,group.limit),(10473187,10643310,50))


if __name__ == "__main__":
    unittest.main()
