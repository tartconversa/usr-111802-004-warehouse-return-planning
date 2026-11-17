"""核验补货口径：退货分阶段核算、建议版本化、固化与重审。"""
import tempfile
import unittest
from pathlib import Path
from analytics import AnalyticsService

OCCURRED = "2026-11-10T10:00:00+08:00"


def event(source, event_id, kind, entity, payload, revision=1, observed="2026-11-10T11:00:00+08:00"):
    return {"source": source, "event_id": event_id, "revision": revision,
            "kind": kind, "entity": entity, "occurred_at": OCCURRED,
            "observed_at": observed, "payload": payload}


class ReplenishmentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = AnalyticsService(Path(self.tmp.name) / "data.sqlite3")
        self.service.register("entity-1", "主体一", "group-1")
        self.service.link("wms", "wms-1", "entity-1")
        self.service.link("qc", "qc-1", "entity-1")
        self.service.domain.set_available("wh-1", "sku-1", 3)
        self.service.ingest(event("wms", "sale-1", "sale", "wms-1",
                                  {"warehouse": "wh-1", "sku": "sku-1", "quantity": 10}))
        self.service.ingest(event("wms", "ret-1", "return", "wms-1",
                                  {"return_id": "r-1", "warehouse": "wh-1",
                                   "sku": "sku-1", "quantity": 4, "status": "requested"}))

    def tearDown(self):
        self.service.close()
        self.tmp.cleanup()

    def line(self, snapshot, sku="sku-1"):
        return next(item for item in snapshot["lines"]
                    if item["warehouse"] == "wh-1" and item["sku"] == sku)

    def test_pending_returns_never_offset_demand(self):
        snapshot = self.service.replenishment()
        line = self.line(snapshot)
        self.assertEqual(line["sales_quantity"], 10)
        self.assertEqual(line["return_requested"], 4)
        self.assertEqual(line["return_in_transit"], 0)
        self.assertEqual(line["resellable_returns"], 0)
        self.assertEqual(line["pending_returns"], 4)
        # 申请退货不抵扣：净需求仍为 10，建议采购 10-3=7，同时提示 4 件预期回流
        self.assertEqual(line["net_demand"], 10)
        self.assertEqual(line["suggested_quantity"], 7)
        self.assertIn("未抵扣", line["basis"])
        self.assertTrue(snapshot["data_version"])

        # 状态推进到在途，依然不抵扣
        self.service.ingest(event("wms", "ret-1", "return", "wms-1",
                                  {"return_id": "r-1", "warehouse": "wh-1",
                                   "sku": "sku-1", "quantity": 4, "status": "in_transit"},
                                  revision=2, observed="2026-11-11T11:00:00+08:00"))
        line = self.line(self.service.replenishment())
        self.assertEqual(line["return_in_transit"], 4)
        self.assertEqual(line["return_requested"], 0)
        self.assertEqual(line["suggested_quantity"], 7)

    def test_inspection_result_adjusts_current_suggestion(self):
        self.service.ingest(event("qc", "insp-1", "inspection", "qc-1",
                                  {"return_ref": "r-1", "warehouse": "wh-1",
                                   "sku": "sku-1", "quantity": 3, "conclusion": "resellable"},
                                  observed="2026-11-12T11:00:00+08:00"))
        line = self.line(self.service.replenishment())
        self.assertEqual(line["resellable_returns"], 3)
        self.assertEqual(line["pending_returns"], 1)
        self.assertEqual(line["net_demand"], 7)
        self.assertEqual(line["suggested_quantity"], 4)

        # 剩余 1 件质检不合格，不产生回流
        self.service.ingest(event("qc", "insp-2", "inspection", "qc-1",
                                  {"return_ref": "r-1", "warehouse": "wh-1",
                                   "sku": "sku-1", "quantity": 1, "conclusion": "unresellable"},
                                  observed="2026-11-13T11:00:00+08:00"))
        line = self.line(self.service.replenishment())
        self.assertEqual(line["resellable_returns"], 3)
        self.assertEqual(line["unresellable_returns"], 1)
        self.assertEqual(line["pending_returns"], 0)

    def test_as_of_rebuilds_historical_version(self):
        self.service.ingest(event("qc", "insp-1", "inspection", "qc-1",
                                  {"return_ref": "r-1", "warehouse": "wh-1",
                                   "sku": "sku-1", "quantity": 3, "conclusion": "resellable"},
                                  observed="2026-11-12T11:00:00+08:00"))
        before = self.service.replenishment("2026-11-11T12:00:00+08:00")
        self.assertEqual(self.line(before)["resellable_returns"], 0)
        self.assertEqual(self.line(before)["suggested_quantity"], 7)
        after = self.service.replenishment("2026-11-12T12:00:00+08:00")
        self.assertEqual(self.line(after)["suggested_quantity"], 4)

    def test_published_report_is_frozen_and_flagged_for_review(self):
        published = self.service.publish_replenishment(
            "rep-1", "2026-11-10", "首批补货建议")
        self.assertEqual(self.line(published["body"])["suggested_quantity"], 7)
        version = published["body"]["data_version"]
        manifest_revisions = {item["event_id"]: item["revision"]
                              for item in published["body"]["manifest"]["events"]}
        self.assertEqual(manifest_revisions["ret-1"], 1)

        # 退货进入在途、质检通过，当前建议随之调整
        self.service.ingest(event("wms", "ret-1", "return", "wms-1",
                                  {"return_id": "r-1", "warehouse": "wh-1",
                                   "sku": "sku-1", "quantity": 4, "status": "in_transit"},
                                  revision=2, observed="2026-11-11T11:00:00+08:00"))
        self.service.ingest(event("qc", "insp-1", "inspection", "qc-1",
                                  {"return_ref": "r-1", "warehouse": "wh-1",
                                   "sku": "sku-1", "quantity": 3, "conclusion": "resellable"},
                                  observed="2026-11-12T11:00:00+08:00"))

        # 已发布报告原样保留：同编号不同内容被拒绝，读取仍是旧版
        with self.assertRaises(ValueError):
            self.service.publish_replenishment("rep-1", "2026-11-12", "首批补货建议")
        frozen = self.service.decisions.get("rep-1")
        self.assertEqual(frozen["body"]["data_version"], version)
        self.assertEqual(self.line(frozen["body"])["suggested_quantity"], 7)

        flags = self.service.review_replenishment_reports()
        self.assertEqual(len(flags), 1)
        flag = flags[0]
        self.assertEqual(flag["report_id"], "rep-1")
        kinds = {reason["type"] for reason in flag["reasons"]}
        self.assertEqual(kinds, {"event_revised", "event_added"})
        revised = next(r for r in flag["reasons"] if r["type"] == "event_revised")
        self.assertEqual((revised["from_revision"], revised["to_revision"]), (1, 2))
        self.assertIn("在途", revised["change"])
        impact = flag["line_impacts"][0]
        self.assertEqual((impact["old_suggested"], impact["current_suggested"], impact["delta"]),
                         (7, 4, -3))
        self.assertNotEqual(flag["published_data_version"], flag["current_data_version"])

    def test_stock_change_also_flags_history(self):
        self.service.publish_replenishment("rep-2", "2026-11-10", "库存基线建议")
        self.service.domain.set_available("wh-1", "sku-1", 8)
        flag = self.service.review_replenishment_reports()[0]
        self.assertIn("stock_changed", {r["type"] for r in flag["reasons"]})
        self.assertEqual(flag["line_impacts"][0]["current_suggested"], 2)

    def test_unchanged_data_needs_no_review_but_retraction_does(self):
        self.service.publish_replenishment("rep-3", "2026-11-10", "稳定基线建议")
        self.assertEqual(self.service.review_replenishment_reports(), [])

        retracted = event("wms", "sale-1", "sale", "wms-1",
                          {"warehouse": "wh-1", "sku": "sku-1", "quantity": 10},
                          revision=2, observed="2026-11-14T11:00:00+08:00")
        retracted["deleted"] = True
        self.service.ingest(retracted)
        flag = self.service.review_replenishment_reports()[0]
        self.assertEqual(flag["report_id"], "rep-3")
        self.assertIn("event_retracted", {r["type"] for r in flag["reasons"]})
        # 历史报告本身仍原样保留
        frozen = self.service.decisions.get("rep-3")
        self.assertEqual(self.line(frozen["body"])["sales_quantity"], 10)

    def test_unlinked_inspection_is_listed_not_counted(self):
        self.service.ingest(event("qc", "insp-x", "inspection", "qc-1",
                                  {"return_ref": "r-unknown", "warehouse": "wh-1",
                                   "sku": "sku-1", "quantity": 2, "conclusion": "resellable"}))
        snapshot = self.service.replenishment()
        self.assertEqual(snapshot["unlinked_inspections"],
                         [{"source": "qc", "event_id": "insp-x", "return_ref": "r-unknown"}])
        self.assertEqual(self.line(snapshot)["resellable_returns"], 0)

    def test_unmatched_event_is_excluded(self):
        self.service.ingest(event("wms", "sale-2", "sale", "wms-ghost",
                                  {"warehouse": "wh-1", "sku": "sku-1", "quantity": 99}))
        snapshot = self.service.replenishment()
        self.assertEqual(snapshot["unmatched_events"],
                         [{"source": "wms", "event_id": "sale-2"}])
        self.assertEqual(self.line(snapshot)["sales_quantity"], 10)

    def test_validation_rules(self):
        bad_return = dict(event("wms", "ret-bad", "return", "wms-1",
                                {"return_id": "r-bad", "warehouse": "wh-1",
                                 "sku": "sku-1", "quantity": 1, "status": "sold"}))
        self.service.ingest(bad_return)
        with self.assertRaises(ValueError):
            self.service.replenishment()
        bad_inspection = event("qc", "insp-bad", "inspection", "qc-1",
                               {"return_ref": "r-1", "warehouse": "wh-1",
                                "sku": "sku-1", "quantity": 1, "conclusion": "maybe"})
        self.service.ingest(bad_inspection)
        with self.assertRaises(ValueError):
            self.service.replenishment()
        over = event("qc", "insp-over", "inspection", "qc-1",
                     {"return_ref": "r-1", "warehouse": "wh-1",
                      "sku": "sku-1", "quantity": 5, "conclusion": "resellable"})
        self.service.ingest(over)
        with self.assertRaises(ValueError):
            self.service.replenishment()
        zero_qty = event("wms", "sale-0", "sale", "wms-1",
                         {"warehouse": "wh-1", "sku": "sku-1", "quantity": 0})
        self.service.ingest(zero_qty)
        with self.assertRaises(ValueError):
            self.service.replenishment()

    def test_manifest_pins_exact_input_revisions(self):
        first = self.service.replenishment()
        second = self.service.replenishment()
        self.assertEqual(first["data_version"], second["data_version"])
        ids = {item["event_id"] for item in first["manifest"]["events"]}
        self.assertEqual(ids, {"sale-1", "ret-1"})
        self.assertEqual(first["manifest"]["stock"],
                         [{"warehouse": "wh-1", "sku": "sku-1", "quantity": 3}])


if __name__ == "__main__":
    unittest.main()
