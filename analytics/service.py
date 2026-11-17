"""协调来源事件、主体目录、现有摘要和报告发布。"""
import json
import sqlite3
from pathlib import Path
from .store import EventStore
from .identity import EntityDirectory
from .metrics import Metrics
from .decisions import DecisionBook
from .domain import StockLedger
from .replenish import ReplenishmentAdvisor

class AnalyticsService:
    def __init__(self, database, config_path=None):
        if config_path is None:
            config_path = Path(__file__).resolve().parents[1] / "config" / "domain.json"
        self.config = json.loads(Path(config_path).read_text(encoding="utf-8"))
        self.connection = sqlite3.connect(database)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.store = EventStore(self.connection, self.config["event_kinds"])
        self.entities = EntityDirectory(self.connection)
        self.metrics = Metrics(self.store, self.entities)
        self.decisions = DecisionBook(self.connection)
        self.domain = StockLedger(self.connection)
        self.advisor = ReplenishmentAdvisor(
            self.store, self.entities, self.domain,
            self.config["return_statuses"], self.config["inspection_conclusions"])
        self.connection.commit()

    def close(self):
        self.connection.close()

    def register(self, entity_id, label, group_id):
        with self.connection:
            return self.entities.register(entity_id,label,group_id)

    def link(self, source, external_id, entity_id):
        with self.connection:
            return self.entities.link(source,external_id,entity_id)

    def ingest(self, event):
        with self.connection:
            return self.store.ingest(event)

    def summary(self, as_of=None):
        return self.metrics.summary(as_of)

    def publish(self, report_id, created_at, title, as_of=None):
        body = {"as_of": as_of, "summary": self.summary(as_of)}
        with self.connection:
            return self.decisions.publish(report_id, created_at, title, body)

    def replenishment(self, as_of=None):
        """按当前口径生成补货建议（只读，不固化）。"""
        return self.advisor.snapshot(as_of)

    def publish_replenishment(self, report_id, created_at, title, as_of=None):
        """发布并固化一份补货建议；同编号内容不同会被拒绝，历史建议不会被改写。"""
        body = self.advisor.snapshot(as_of)
        with self.connection:
            return self.decisions.publish(report_id, created_at, title, body)

    def review_replenishment_reports(self):
        """比对当前数据，返回需要重新审视的历史补货建议清单（不修改任何报告）。"""
        flags = []
        for row in self.connection.execute("SELECT report_id FROM reports ORDER BY report_id"):
            report = self.decisions.get(row["report_id"])
            flag = self.advisor.review_report(report)
            if flag is not None:
                flags.append(flag)
        return flags
