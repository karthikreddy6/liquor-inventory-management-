from sqlalchemy import Column, Integer, String, Float, DateTime, UniqueConstraint
from sqlalchemy.sql import func
from database import Base

class Invoice(Base):
    __tablename__ = "invoices"

    id = Column(Integer, primary_key=True)
    invoice_number = Column(String, unique=True, index=True)
    invoice_date = Column(String)

    retailer_name = Column(String)
    retailer_code = Column(String)
    licensee_pan = Column(String)
    uploaded_by = Column(String)
    uploaded_at = Column(DateTime, server_default=func.now())

    created_at = Column(DateTime, server_default=func.now())

class InvoiceTotals(Base):
    __tablename__ = "invoice_totals"

    id = Column(Integer, primary_key=True)
    invoice_number = Column(String, index=True)
    e_challan_amount = Column(Float)
    previous_credit = Column(Float)
    sub_total = Column(Float)
    special_excise_cess = Column(Float)
    tcs = Column(Float)
    less_this_invoice_value = Column(Float)
    retailer_credit_balance = Column(Float)
    invoice_value = Column(Float)
    mrp_round_off = Column(Float)
    net_invoice_value = Column(Float)
    total_invoice_value = Column(Float)

class InvoiceItem(Base):
    __tablename__ = "invoice_items"

    id = Column(Integer, primary_key=True)
    invoice_number = Column(String, index=True)

    sl_no = Column(Integer)
    brand_number = Column(String)
    brand_name = Column(String)

    product_type = Column(String)
    pack_type = Column(String)

    pack_size_case = Column(Integer)
    pack_size_quantity_ml = Column(Integer)

    cases_delivered = Column(Integer)
    bottles_delivered = Column(Integer)

    rate_per_case = Column(Float)
    unit_rate_per_bottle = Column(Float)
    total_amount = Column(Float)

class PresentStockDetail(Base):
    __tablename__ = "present_stock_details"

    id = Column(Integer, primary_key=True)
    brand_number = Column(String, index=True)
    brand_name = Column(String)

    product_type = Column(String)
    pack_type = Column(String)

    pack_size_case = Column(Integer, index=True)
    pack_size_quantity_ml = Column(Integer, index=True)

    total_cases = Column(Integer)
    total_bottles = Column(Integer)

    rate_per_case = Column(Float)
    unit_rate_per_bottle = Column(Float)
    total_amount = Column(Float)

    last_invoice_date = Column(String)
    last_updated_item_name = Column(String)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

class StockSummary(Base):
    __tablename__ = "stock_summary"

    id = Column(Integer, primary_key=True)
    total_cases_all_items = Column(Integer)
    total_price_all_items = Column(Float)
    last_updated_item_name = Column(String)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

class SellReport(Base):
    __tablename__ = "sell_reports"

    id = Column(Integer, primary_key=True)
    stock_id = Column(Integer, index=True)
    brand_number = Column(String, index=True)
    brand_name = Column(String)

    pack_size_case = Column(Integer)
    pack_size_quantity_ml = Column(Integer)

    opening_cases = Column(Integer)
    opening_bottles = Column(Integer)

    invoice_added_cases = Column(Integer)
    invoice_added_bottles = Column(Integer)

    total_cases = Column(Integer)
    total_bottles = Column(Integer)

    closing_cases = Column(Integer)
    closing_bottles = Column(Integer)

    sold_cases = Column(Integer)
    sold_bottles = Column(Integer)

    unit_rate_per_bottle = Column(Float)
    sell_amount = Column(Float)

    report_date = Column(String)
    created_by = Column(String)
    edited_by = Column(String)
    edited_at = Column(DateTime)
    edit_count = Column(Integer, default=0)
    created_at = Column(DateTime, server_default=func.now())

class PriceListItem(Base):
    __tablename__ = "price_list"
    __table_args__ = (
        UniqueConstraint("brand_number", "size_code", "pack_type", "volume_ml", name="uq_price_list_item"),
    )

    id = Column(Integer, primary_key=True)
    brand_number = Column(String, index=True)
    size_code = Column(String)
    pack_type = Column(String)
    product_name = Column(String)
    mrp = Column(Float)
    volume_ml = Column(Integer)
    description = Column(String)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

class SellFinance(Base):
    __tablename__ = "sell_finance"
    __table_args__ = (
        UniqueConstraint("report_date", name="uq_sell_finance_report_date"),
    )

    id = Column(Integer, primary_key=True)
    report_date = Column(String, index=True)
    total_sell_amount = Column(Float)
    last_balance_amount = Column(Float)
    total_amount = Column(Float)
    upi_phonepay = Column(Float)
    cash = Column(Float)
    total_balance = Column(Float)
    total_expenses = Column(Float)
    final_balance = Column(Float)
    created_by = Column(String)
    updated_by = Column(String)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

class SellFinanceExpense(Base):
    __tablename__ = "sell_finance_expenses"

    id = Column(Integer, primary_key=True)
    finance_id = Column(Integer, index=True)
    name = Column(String)
    amount = Column(Float)
    created_at = Column(DateTime, server_default=func.now())

class AuditLog(Base):
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True)
    username = Column(String, index=True)
    role = Column(String, index=True)
    action = Column(String, index=True)
    entity_type = Column(String)
    entity_id = Column(String)
    details = Column(String)
    created_at = Column(DateTime, server_default=func.now())

class UserLogin(Base):
    __tablename__ = "user_logins"

    id = Column(Integer, primary_key=True)
    username = Column(String, unique=True, index=True)
    role = Column(String)
    last_login_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
