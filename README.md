# 仓库补货运营数据服务

服务登记多来源事件、业务主体及现有摘要，并保存发布报告的原始内容。示例数据为虚构记录。

需要 Python 3.10 或更新版本；仅依赖标准库。

运行测试：python -m unittest discover -s tests -v
编译检查：python -m compileall -q analytics tests
本地演练：python -m analytics --database demo.sqlite3 --fixture fixtures/sample.json

analytics/store.py 保存来源事件和修订；identity.py 管理外部身份映射；metrics.py 汇总已映射主体的事件计数与通用金额；decisions.py 保存已发布报告；service.py 协调这些现有接口。当前摘要只表示原始观察，不提供行业专属分析结论。

本服务另有 StockLedger 的现行业务目录，见 analytics/domain.py。replenish.py 在其之上提供补货建议口径：区分申请退货、在途、质检结论与可再次销售数量（详见 docs/domain.md）。

补货相关接口：

- `service.replenishment(as_of=None)`：按当前口径生成建议（只读），每行含销量、四类退货数量、可用库存、净需求、建议采购量与文字依据，并带 `data_version` 与 `manifest`；
- `service.publish_replenishment(report_id, created_at, title, as_of=None)`：发布并固化建议，同编号内容不同会被拒绝，已送采购的建议不会被退货状态变化改写；
- `service.review_replenishment_reports()`：对照当前数据，返回需要重新审视的历史建议及采购行数量差异，不修改历史报告。

事件 payload 约定：sale 含 `warehouse`/`sku`/`quantity`；return 含 `return_id`/`warehouse`/`sku`/`quantity`/`status`（requested、in_transit）；inspection 含 `return_ref`/`warehouse`/`sku`/`quantity`/`conclusion`（resellable、unresellable），允许对同一退货单分批质检。状态与结论的允许取值见 config/domain.json。
