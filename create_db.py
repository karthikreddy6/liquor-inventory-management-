from database import engine, Base
from models import (
    Invoice,
    InvoiceItem,
    InvoiceTotals,
    PresentStockDetail,
    StockSummary,
    SellReport,
    PriceListItem,
    SellFinance,
    SellFinanceExpense,
    AuditLog,
    UserLogin,
)

def create_tables():
    Base.metadata.create_all(bind=engine)
    print("Database created successfully")

if __name__ == "__main__":
    create_tables()
