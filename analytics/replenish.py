"""补货建议分析口径：销售消耗与退货回流分阶段核算。

口径约定：
- 销量按 sale 事件的 quantity 汇总；退货只有质检结论为 resellable（可再售）
  后才允许抵扣需求。申请退货（requested）与在途（in_transit）数量尚未回到
  可售库存，只作为预期回流提示，不参与抵扣，避免采购断货；同时单列展示，
  避免把未来必然回流的货物重复下单。
- inspection 事件通过 payload.return_ref 关联退货单 payload.return_id，
  质检结论分为 resellable / unresellable 两档，支持分批质检。
- 每份建议附带 data_version（输入清单的 sha256）与 manifest（所用事件的
  来源/编号/修订、所用库存快照），说明它采用了哪一版销售与回流数据。
- 建议发布后由 DecisionBook 固化；退货状态随后变化不会改写历史建议，
  只能通过 review_report 输出重审提示。
"""
import hashlib
import json
from collections import defaultdict

DOMAIN_KINDS = ("sale", "return", "inspection")

RETURN_STATUS_LABELS = {"requested": "申请退货", "in_transit": "在途"}
CONCLUSION_LABELS = {"resellable": "可再售", "unresellable": "不可再售"}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


class ReplenishmentAdvisor:
    """基于事件存储、主体目录与库存台账生成补货建议。"""

    def __init__(self, store, entities, ledger, return_statuses, conclusions):
        self.store = store
        self.entities = entities
        self.ledger = ledger
        self.return_statuses = frozenset(return_statuses)
        self.conclusions = frozenset(conclusions)

    @staticmethod
    def _text(payload, field):
        value = payload.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"字段 {field} 不能为空")
        return value

    @staticmethod
    def _quantity(payload):
        value = payload.get("quantity")
        if type(value) is not int or value <= 0:
            raise ValueError("数量必须为正整数")
        return value

    def _line_key(self, payload):
        return self._text(payload, "warehouse"), self._text(payload, "sku")

    # ------------------------------------------------------------------
    # 当前口径快照
    # ------------------------------------------------------------------
    def snapshot(self, as_of=None):
        sales = []
        returns = {}
        inspections = []
        unmatched = []
        manifest_events = []

        for event in self.store.current(as_of):
            kind = event["kind"]
            if kind not in DOMAIN_KINDS:
                continue
            if self.entities.resolve(event["source"], event["entity"]) is None:
                unmatched.append({"source": event["source"], "event_id": event["event_id"]})
                continue
            manifest_events.append({"source": event["source"], "event_id": event["event_id"],
                                    "revision": event["revision"]})
            payload = event["payload"]
            if kind == "sale":
                sales.append({"key": self._line_key(payload), "quantity": self._quantity(payload)})
            elif kind == "return":
                return_id = self._text(payload, "return_id")
                if return_id in returns:
                    raise ValueError(f"退货编号 {return_id} 被多个事件登记")
                status = self._text(payload, "status")
                if status not in self.return_statuses:
                    raise ValueError("退货状态不在允许范围")
                returns[return_id] = {
                    "return_id": return_id,
                    "key": self._line_key(payload),
                    "quantity": self._quantity(payload),
                    "status": status,
                    "resellable": 0,
                    "unresellable": 0,
                }
            else:
                conclusion = self._text(payload, "conclusion")
                if conclusion not in self.conclusions:
                    raise ValueError("质检结论不在允许范围")
                inspections.append({
                    "source": event["source"],
                    "event_id": event["event_id"],
                    "return_ref": self._text(payload, "return_ref"),
                    "key": self._line_key(payload),
                    "quantity": self._quantity(payload),
                    "conclusion": conclusion,
                })

        unlinked = []
        for inspection in inspections:
            target = returns.get(inspection["return_ref"])
            if target is None:
                unlinked.append({"source": inspection["source"],
                                 "event_id": inspection["event_id"],
                                 "return_ref": inspection["return_ref"]})
                continue
            if inspection["key"] != target["key"]:
                raise ValueError(f"质检单 {inspection['event_id']} 的商品与退货单不一致")
            target[inspection["conclusion"]] += inspection["quantity"]
            if target["resellable"] + target["unresellable"] > target["quantity"]:
                raise ValueError(f"退货单 {target['return_id']} 的质检数量超过退货数量")

        buckets = defaultdict(lambda: {
            "sales_quantity": 0, "return_requested": 0, "return_in_transit": 0,
            "resellable_returns": 0, "unresellable_returns": 0,
        })
        for sale in sales:
            buckets[sale["key"]]["sales_quantity"] += sale["quantity"]
        for record in returns.values():
            entry = buckets[record["key"]]
            pending = record["quantity"] - record["resellable"] - record["unresellable"]
            if pending:
                field = "return_requested" if record["status"] == "requested" else "return_in_transit"
                entry[field] += pending
            entry["resellable_returns"] += record["resellable"]
            entry["unresellable_returns"] += record["unresellable"]

        lines = []
        manifest_stock = []
        for warehouse, sku in sorted(buckets):
            entry = buckets[(warehouse, sku)]
            available = self.ledger.available(warehouse, sku)
            pending = entry["return_requested"] + entry["return_in_transit"]
            net_demand = entry["sales_quantity"] - entry["resellable_returns"]
            suggested = max(0, net_demand - available)
            manifest_stock.append({"warehouse": warehouse, "sku": sku, "quantity": available})
            lines.append({
                "warehouse": warehouse,
                "sku": sku,
                "sales_quantity": entry["sales_quantity"],
                "return_requested": entry["return_requested"],
                "return_in_transit": entry["return_in_transit"],
                "resellable_returns": entry["resellable_returns"],
                "unresellable_returns": entry["unresellable_returns"],
                "pending_returns": pending,
                "available_stock": available,
                "net_demand": net_demand,
                "suggested_quantity": suggested,
                "basis": (
                    f"净需求=销量{entry['sales_quantity']}-质检通过可再售"
                    f"{entry['resellable_returns']}={net_demand}；可用库存{available}；"
                    f"申请/在途待回流{pending}件未抵扣，质检通过后才计入可再售数量"
                ),
            })

        manifest = {
            "as_of": as_of,
            "events": sorted(manifest_events, key=lambda item: (item["source"], item["event_id"])),
            "stock": sorted(manifest_stock, key=lambda item: (item["warehouse"], item["sku"])),
        }
        return {
            "type": "replenishment_suggestion",
            "as_of": as_of,
            "data_version": digest(manifest),
            "lines": lines,
            "unmatched_events": unmatched,
            "unlinked_inspections": unlinked,
            "manifest": manifest,
        }

    # ------------------------------------------------------------------
    # 历史建议重审（只读，不改写已发布内容）
    # ------------------------------------------------------------------
    def review_report(self, report):
        body = report["body"]
        if body.get("type") != "replenishment_suggestion":
            return None

        old_events = {(item["source"], item["event_id"]): item["revision"]
                      for item in body["manifest"]["events"]}
        old_stock = {(item["warehouse"], item["sku"]): item["quantity"]
                     for item in body["manifest"]["stock"]}
        scope = {(line["warehouse"], line["sku"]) for line in body["lines"]}

        reasons = []
        for (source, event_id), old_revision in sorted(old_events.items()):
            latest = self.store.find(source, event_id)
            if latest is None:
                reasons.append({"type": "event_retracted", "source": source,
                                "event_id": event_id, "revision": old_revision,
                                "change": "原引用事件已被删除"})
                continue
            if latest["deleted"]:
                # 最新修订为删除标记时，current() 已不再返回该事件，优先判定为撤回
                reasons.append({"type": "event_retracted", "source": source,
                                "event_id": event_id, "revision": latest["revision"],
                                "change": f"事件在修订 {latest['revision']} 被删除"})
                continue
            if latest["revision"] > old_revision:
                old = self.store.find(source, event_id, old_revision)
                reasons.append({"type": "event_revised", "source": source, "event_id": event_id,
                                "from_revision": old_revision, "to_revision": latest["revision"],
                                "warehouse": latest["payload"].get("warehouse"),
                                "sku": latest["payload"].get("sku"),
                                "change": self._describe_change(old, latest)})

        for latest in self.store.current():
            if latest["kind"] not in DOMAIN_KINDS:
                continue
            key = (latest["source"], latest["event_id"])
            if key in old_events:
                continue
            payload = latest["payload"]
            line_key = (payload.get("warehouse"), payload.get("sku"))
            if line_key in scope and self.entities.resolve(latest["source"], latest["entity"]):
                reasons.append({"type": "event_added", "source": latest["source"],
                                "event_id": latest["event_id"], "kind": latest["kind"],
                                "warehouse": line_key[0], "sku": line_key[1],
                                "change": "报告发布后出现新的相关事件"})

        for (warehouse, sku), old_quantity in sorted(old_stock.items()):
            current_quantity = self.ledger.available(warehouse, sku)
            if current_quantity != old_quantity:
                reasons.append({"type": "stock_changed", "warehouse": warehouse, "sku": sku,
                                "from_quantity": old_quantity, "to_quantity": current_quantity,
                                "change": f"可售库存 {old_quantity} → {current_quantity}"})

        current = self.snapshot()
        current_lines = {(line["warehouse"], line["sku"]): line for line in current["lines"]}
        impacts = []
        for line in body["lines"]:
            key = (line["warehouse"], line["sku"])
            latest_line = current_lines.get(key)
            new_suggested = latest_line["suggested_quantity"] if latest_line else 0
            delta = new_suggested - line["suggested_quantity"]
            if delta != 0:
                impacts.append({"warehouse": line["warehouse"], "sku": line["sku"],
                                "old_suggested": line["suggested_quantity"],
                                "current_suggested": new_suggested, "delta": delta})

        if not reasons and not impacts:
            return None
        return {
            "report_id": report["report_id"],
            "needs_review": True,
            "reasons": reasons,
            "line_impacts": impacts,
            "published_data_version": body["data_version"],
            "current_data_version": current["data_version"],
        }

    @staticmethod
    def _describe_change(old, new):
        old_payload, new_payload = old["payload"], new["payload"]
        if new["kind"] == "return":
            return (f"退货单 {new_payload.get('return_id')} 状态 "
                    f"{RETURN_STATUS_LABELS.get(old_payload.get('status'), old_payload.get('status'))} → "
                    f"{RETURN_STATUS_LABELS.get(new_payload.get('status'), new_payload.get('status'))}")
        if new["kind"] == "inspection":
            return (f"退货单 {new_payload.get('return_ref')} 质检结论 "
                    f"{CONCLUSION_LABELS.get(old_payload.get('conclusion'), old_payload.get('conclusion'))} → "
                    f"{CONCLUSION_LABELS.get(new_payload.get('conclusion'), new_payload.get('conclusion'))}")
        return (f"销量修订：{old_payload.get('quantity', 0)} → "
                f"{new_payload.get('quantity', 0)}")
