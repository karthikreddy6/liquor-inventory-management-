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
    SellFinancePhonePay,
    SellFinanceCash,
    AuditLog,
    UserLogin,
)
from services.db_migrations import ensure_invoice_totals_tax_columns

def create_tables():
    Base.metadata.create_all(bind=engine)
    ensure_invoice_totals_tax_columns(engine)
    print("Database created successfully")

if __name__ == "__main__":
    create_tables()
