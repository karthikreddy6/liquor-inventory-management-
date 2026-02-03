from sqlalchemy import Column, Integer, String, Float, DateTime
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
    
