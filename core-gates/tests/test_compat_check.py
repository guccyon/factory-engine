"""compat_check の規則を、手で作った小さな契約で確かめる。

  python3 -m unittest discover -s core-gates/tests
"""
import copy
import importlib.util
import unittest
from datetime import date
from pathlib import Path

spec = importlib.util.spec_from_file_location("compat_check", Path(__file__).resolve().parent.parent / "compat_check.py")
cc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cc)

TODAY = date(2026, 10, 28)

OPENAPI = {
    "openapi": "3.1.0",
    "paths": {
        "/bookings": {
            "post": {
                "requestBody": {"required": True, "content": {"application/json": {"schema": {"$ref": "#/components/schemas/CreateBooking"}}}},
                "responses": {"201": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/Booking"}}}}},
            },
            "get": {
                "parameters": [{"name": "status", "in": "query", "schema": {"type": "string", "enum": ["confirmed", "cancelled"]}}],
                "responses": {"200": {"content": {"application/json": {"schema": {"type": "array", "items": {"$ref": "#/components/schemas/Booking"}}}}}},
            },
        },
        "/old": {"get": {"responses": {"200": {"description": "ok"}}}},
    },
    "components": {"schemas": {
        "CreateBooking": {"type": "object", "required": ["slot_id"], "properties": {"slot_id": {"type": "string"}, "note": {"type": "string"}}},
        "Booking": {"type": "object", "required": ["id", "status"], "properties": {
            "id": {"type": "string"}, "status": {"type": "string", "enum": ["confirmed", "cancelled"]}, "note": {"type": ["string", "null"]}}},
    }},
}

ASYNCAPI = {
    "asyncapi": "3.0.0",
    "channels": {"BookingConfirmed": {"messages": {"BookingConfirmed": {"$ref": "#/components/messages/BookingConfirmed"}}}},
    "components": {"messages": {"BookingConfirmed": {"payload": {"type": "object", "required": ["booking_id"], "properties": {
        "booking_id": {"type": "string"}, "occurred_at": {"type": "string"}}}}}},
}


def run_openapi(mutate):
    head = copy.deepcopy(OPENAPI)
    mutate(head)
    ck = cc.Checker(TODAY, 90)
    ck.openapi("api.yaml", OPENAPI, head)
    return {v["rule"] for v in ck.violations}, {w["rule"] for w in ck.warnings}


def run_asyncapi(mutate):
    head = copy.deepcopy(ASYNCAPI)
    mutate(head)
    ck = cc.Checker(TODAY, 90)
    ck.asyncapi("events.yaml", ASYNCAPI, head)
    return {v["rule"] for v in ck.violations}, {w["rule"] for w in ck.warnings}


def schemas(d):
    return d["components"]["schemas"]


class OpenApiRules(unittest.TestCase):
    def test_unchanged(self):
        self.assertEqual(run_openapi(lambda d: None), (set(), set()))

    def test_additive_changes_pass(self):
        def m(d):
            schemas(d)["Booking"]["properties"]["coach_id"] = {"type": "string"}          # レスポンスに項目を足す
            schemas(d)["CreateBooking"]["properties"]["source"] = {"type": "string"}      # リクエストに任意の項目を足す
            d["paths"]["/reviews"] = {"post": {"responses": {"201": {"description": "ok"}}}}   # 操作を足す
            d["paths"]["/bookings"]["get"]["parameters"].append({"name": "page", "in": "query", "schema": {"type": "integer"}})
        self.assertEqual(run_openapi(m), (set(), set()))

    def test_response_property_removed(self):
        self.assertIn("property_removed", run_openapi(lambda d: schemas(d)["Booking"]["properties"].pop("note"))[0])

    def test_request_required_added(self):
        self.assertIn("required_added", run_openapi(lambda d: schemas(d)["CreateBooking"]["required"].append("note"))[0])

    def test_response_required_removed(self):
        self.assertIn("required_removed", run_openapi(lambda d: schemas(d)["Booking"]["required"].remove("status"))[0])

    def test_type_changed(self):
        self.assertIn("type_changed", run_openapi(lambda d: schemas(d)["Booking"]["properties"]["id"].update(type="integer"))[0])

    def test_response_null_added_is_breaking_but_removed_is_not(self):
        self.assertIn("type_changed", run_openapi(lambda d: schemas(d)["Booking"]["properties"]["id"].update(type=["string", "null"]))[0])
        self.assertEqual(run_openapi(lambda d: schemas(d)["Booking"]["properties"]["note"].update(type="string"))[0], set())

    def test_operation_removed(self):
        self.assertIn("operation_removed", run_openapi(lambda d: d["paths"].pop("/old"))[0])

    def test_deprecated_operation_removed_after_period(self):
        base = copy.deepcopy(OPENAPI)
        for at, expect in (("2026-07-01", set()), ("2026-10-01", {"operation_removed"})):
            base["paths"]["/old"]["get"].update({"deprecated": True, "x-deprecated-at": at})
            head = copy.deepcopy(base)
            head["paths"].pop("/old")
            ck = cc.Checker(TODAY, 90)
            ck.openapi("api.yaml", base, head)
            self.assertEqual({v["rule"] for v in ck.violations}, expect, at)

    def test_request_enum_value_removed(self):
        self.assertIn("enum_value_removed", run_openapi(lambda d: d["paths"]["/bookings"]["get"]["parameters"][0]["schema"]["enum"].remove("cancelled"))[0])

    def test_response_enum_value_added_is_warning(self):
        v, w = run_openapi(lambda d: schemas(d)["Booking"]["properties"]["status"]["enum"].append("no_show"))
        self.assertEqual(v, set())
        self.assertIn("enum_value_added", w)

    def test_required_param_added(self):
        self.assertIn("required_param_added", run_openapi(lambda d: d["paths"]["/bookings"]["get"]["parameters"].append(
            {"name": "coach_id", "in": "query", "required": True, "schema": {"type": "string"}}))[0])

    def test_success_response_removed(self):
        self.assertIn("success_response_removed", run_openapi(lambda d: d["paths"]["/bookings"]["post"]["responses"].pop("201"))[0])

    def test_array_items_followed(self):
        # GET /bookings は Booking の配列を返す。items の先の項目の削除も拾う
        v, _ = run_openapi(lambda d: d["paths"]["/bookings"]["get"]["responses"]["200"]["content"]["application/json"]["schema"].update(
            items={"type": "object", "properties": {"id": {"type": "string"}}}))
        self.assertIn("property_removed", v)

    def test_deprecated_without_date_is_warning(self):
        _, w = run_openapi(lambda d: d["paths"]["/old"]["get"].update(deprecated=True))
        self.assertIn("deprecated_without_date", w)


class AsyncApiRules(unittest.TestCase):
    def test_unchanged(self):
        self.assertEqual(run_asyncapi(lambda d: None), (set(), set()))

    def test_payload_property_removed(self):
        self.assertIn("property_removed", run_asyncapi(lambda d: d["components"]["messages"]["BookingConfirmed"]["payload"]["properties"].pop("occurred_at"))[0])

    def test_channel_removed(self):
        self.assertIn("channel_removed", run_asyncapi(lambda d: d["channels"].pop("BookingConfirmed"))[0])

    def test_message_added_passes(self):
        def m(d):
            d["channels"]["BookingCancelled"] = {"messages": {}}
            d["components"]["messages"]["BookingCancelled"] = {"payload": {"type": "object"}}
        self.assertEqual(run_asyncapi(m), (set(), set()))


if __name__ == "__main__":
    unittest.main()
