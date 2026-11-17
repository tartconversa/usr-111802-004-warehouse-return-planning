"""仓库可售库存的现有手工登记。"""
class StockLedger:
    def __init__(self, connection):
        self.connection = connection
        connection.execute("CREATE TABLE IF NOT EXISTS stock(warehouse TEXT, sku TEXT, quantity INTEGER, PRIMARY KEY(warehouse,sku))")

    def set_available(self, warehouse, sku, quantity):
        if type(quantity) is not int or quantity < 0: raise ValueError("可售数量无效")
        self.connection.execute("INSERT OR REPLACE INTO stock VALUES(?,?,?)", (warehouse,sku,quantity))

    def available(self, warehouse, sku):
        row = self.connection.execute("SELECT quantity FROM stock WHERE warehouse=? AND sku=?", (warehouse,sku)).fetchone()
        return row[0] if row else 0
