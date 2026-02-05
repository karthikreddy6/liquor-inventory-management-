# Inventory Management API Guide

This document explains how the application works end‑to‑end, the main data flow, and the key APIs.

## 1) Overview (Flow)

1. Login -> get JWT token
2. Upload invoice PDFs (owner/supervisor)
3. Stock updates automatically based on invoice items
4. Create sell report (supervisor) for a date
5. Owner can edit only the latest sell report once
6. Create sell finance (owner/supervisor) after sell report
7. Dashboards and reports read from DB

## 2) Auth

### POST `/auth/login`
Body:
```
{"username":"owner","password":"owner123"}
```
Response includes `access_token` and `summary`:
- `last_uncleared_amount`
- `last_invoice_date`, `last_invoice_number`
- `last_invoice_value`, `last_invoice_retailer_credit_balance`
- `total_present_stock`
- `total_present_stock_mrp_value`
- `last_sell_report_date`, `last_sell_report_value`

Use the token for all protected endpoints:
```
Authorization: Bearer <token>
```

## 3) Invoice Upload

### POST `/upload`
Roles: owner, supervisor
Form‑data: `file=<pdf>`

Rules:
- Retailer code must be `2500552`

Side effects:
- Creates `Invoice`, `InvoiceTotals`, `InvoiceItem`
- Updates `PresentStockDetail` + `StockSummary`

## 4) Present Stock

### GET `/stock`
Roles: owner, supervisor
Returns current stock list and stock summary.

## 5) Sell Report

### GET `/seller/sell-report/prepare`
Roles: owner, supervisor
Returns:
- stock list
- latest invoice date
- last balance amount (from last finance)

### POST `/seller/sell-report`
Role: supervisor  
Body:
```
{
  "report_date": "YYYY-MM-DD",
  "items": [
    {"stock_id": 1, "closing_cases": 2, "closing_bottles": 0}
  ]
}
```
Rules:
- `report_date` must be on or after the latest invoice date
- only one report per date
Side effects:
- Inserts `SellReport`
- Writes JSON file to `output/sell_report_<date>.json`

### POST `/seller/sell-report/edit-last`
Role: owner  
Edits only the latest sell report, only once.

## 6) Sell Finance

### GET `/seller/sell-finance/prepare?report_date=YYYY-MM-DD`
Roles: owner, supervisor  
Returns:
- `total_sell_amount`
- `last_balance_amount`
- `total_amount`

### POST `/seller/sell-finance`
Roles: owner, supervisor  
Body:
```
{
  "report_date": "YYYY-MM-DD",
  "upi_phonepay": 1000,
  "cash": 500,
  "expenses": [
    {"name":"rent","amount":800}
  ]
}
```
Calculated fields:
- `total_sell_amount` = sum of sell report amounts for date
- `last_balance_amount` = last finance final_balance
- `total_amount` = total_sell_amount + last_balance_amount
- `total_balance` = upi + cash - total_amount
- `total_expenses` = sum of expenses
- `final_balance` = total_balance - total_expenses

## 7) Reports and PDF

### GET `/reports/invoices`
Roles: owner, supervisor  
Returns invoice list with upload info.

### GET `/reports/sell-reports`
Roles: owner, supervisor  
Returns sell report batches with finance summary.

### PDF Download
Generates and stores PDFs in:
- `requested_pdf/invoices`
- `requested_pdf/sellreport`

Endpoints:
```
GET /reports/invoices/<invoice_number>/pdf
GET /reports/sell-reports/<report_date>/pdf
```

## 8) Dashboard Summary

### GET `/dashboard/summary`
Roles: owner, supervisor  
Returns a live dashboard summary (same as login summary).

## 9) Database Tables (Key)

- `invoices`, `invoice_items`, `invoice_totals`
- `present_stock_details`, `stock_summary`
- `sell_reports`
- `sell_finance`, `sell_finance_expenses`
- `price_list`

## 10) Notes

- Use `127.0.0.1:5000` when React runs on the same laptop.
- If accessing from another device, use laptop LAN IP and ensure Flask listens on `0.0.0.0`.
