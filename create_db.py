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
    SellFinanceOutsideIncome,
    AuditLog,
    UserBrandAlias,
    UserLogin,
    UserBrandSortPreference,
)
from services.db_migrations import (
    ensure_invoice_totals_tax_columns,
    ensure_user_brand_aliases_support,
    ensure_sell_finance_outside_income_support,
    ensure_user_brand_sort_preferences_support,
)

def create_tables():
    Base.metadata.create_all(bind=engine)
    ensure_invoice_totals_tax_columns(engine)
    ensure_sell_finance_outside_income_support(engine)
    ensure_user_brand_aliases_support(engine)
    ensure_user_brand_sort_preferences_support(engine)
    print("Database created successfully")

if __name__ == "__main__":
    create_tables()
