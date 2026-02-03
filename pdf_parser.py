import pdfplumber
import json
import re
import os


# ---------------- CLEAN HELPERS ----------------
def safe_int(val):
    try:
        return int(val)
    except:
        return 0
def parse_pack_size(val):
    """
    Parses '12 / 650 ml' → (12, 650)
    """
    if not val:
        return 0, 0

    match = re.search(r"(\d+)\s*/\s*(\d+)\s*ml", val.lower())
    if match:
        return int(match.group(1)), int(match.group(2))

    return 0, 0


def clean_amount(val):
    if not val:
        return 0.0
    match = re.search(r"[\d,]+\.\d{2}", str(val))
    return float(match.group().replace(",", "")) if match else 0.0


def extract_amount_by_label(label, text):
    match = re.search(
        rf"{label}\s*[:\-]?\s*([\d,]+\.\d{{2}})",
        text,
        re.IGNORECASE
    )
    return clean_amount(match.group(1)) if match else 0.0


# ---------------- INVOICE VALUES FROM TABLE ----------------
def extract_invoice_values_from_table(pdf):
    values = {
        "invoice_value": 0.0,
        "mrp_round_off": 0.0,
        "net_invoice_value": 0.0
    }

    for page in pdf.pages:
        table = page.extract_table()
        if not table:
            continue

        for row in table:
            joined = " ".join([c or "" for c in row]).lower()

            if (
                "invoice" in joined
                and "mrp" in joined
                and "rounding" in joined
                and "net" in joined
            ):
                cell = row[-1]
                if not cell:
                    continue

                parts = [
                    clean_amount(v)
                    for v in cell.split("\n")
                    if re.search(r"\d", v)
                ]

                if len(parts) >= 3:
                    values["invoice_value"] = parts[0]
                    values["mrp_round_off"] = parts[1]
                    values["net_invoice_value"] = parts[2]

                return values

    return values


# ---------------- OTHER TOTALS ----------------
def extract_totals_block(text):
    totals = {
        "e_challan_amount": 0.0,
        "previous_credit": 0.0,
        "sub_total": 0.0,
        "special_excise_cess": 0.0,
        "tcs": 0.0,
        "less_this_invoice_value": 0.0,
        "retailer_credit_balance": 0.0
    }

    def extract(label):
        m = re.search(
            rf"{label}[\s\S]*?([\d,]+\.\d{{2}})",
            text,
            re.IGNORECASE
        )
        return clean_amount(m.group(1)) if m else 0.0

    totals["special_excise_cess"] = extract("Special Excise Cess")
    totals["tcs"] = extract("TCS")
    totals["e_challan_amount"] = extract("e-challan / DD Amount")
    totals["previous_credit"] = extract("Previous Credit")
    totals["sub_total"] = extract("Sub Total")
    totals["less_this_invoice_value"] = extract("Less this Invoice Value")

    rc = re.search(
        r"Retailer Credit Balance[\s\S]*?Rs\.?\s*([\d,]+\.\d{2})",
        text,
        re.IGNORECASE
    )
    if rc:
        totals["retailer_credit_balance"] = clean_amount(rc.group(1))

    return totals


# ---------------- MAIN PARSER FUNCTION ----------------
def parse_invoice_pdf(pdf_path: str):

    invoice = {
        "invoice_meta": {},
        "retailer": {},
        "licensee": {},
        "items": [],
        "totals": {}
    }

    with pdfplumber.open(pdf_path) as pdf:

        text = "\n".join(page.extract_text() or "" for page in pdf.pages)

        # -------- META --------
        m = re.search(r"ICDC\d+", text)
        invoice["invoice_meta"]["invoice_number"] = m.group() if m else ""

        m = re.search(r"Invoice Date:\s*(.*)", text)
        invoice["invoice_meta"]["invoice_date"] = m.group(1) if m else ""

        # -------- RETAILER --------
        m = re.search(r"Name:\s*(.*?)\s*Code", text, re.DOTALL)
        invoice["retailer"]["name"] = m.group(1).strip() if m else ""

        m = re.search(r"Code:\s*(\d+)", text)
        invoice["retailer"]["code"] = m.group(1) if m else ""

        # -------- LICENSEE --------
        m = re.search(r"PAN:\s*(\w+)", text)
        invoice["licensee"]["pan"] = m.group(1) if m else ""

        # -------- ITEMS --------
        for page in pdf.pages:
            table = page.extract_table()
            if not table:
                continue

            for row in table:
                if not row or not row[0] or not row[0].strip().isdigit():
                    continue

                row = [c.replace("\n", " ").strip() for c in row if c and c.strip()]

                if len(row) < 6:
                    continue
                pack_case, pack_qty = parse_pack_size(row[5])
                item = {
                    "sl_no": safe_int(row[0]),
                    "brand_number": row[1],
                    "brand_name": row[2],
                    "product_type": row[3],
                    "pack_type": row[4],
                    "pack_size_case": pack_case,
                    "pack_size_quantity_ml": pack_qty,
                    "cases_delivered": 0,
                    "bottles_delivered": 0,
                    "rate_per_case": 0.0,
                    "unit_rate_per_bottle": 0.0,
                    "total_amount": clean_amount(row[-1])
                }

                for i, val in enumerate(row):
                    if re.search(r"/\s*\d+\s*ml", val.lower()):
                        if i + 1 < len(row):
                            item["cases_delivered"] = safe_int(row[i + 1])
                        if i + 2 < len(row):
                            item["bottles_delivered"] = safe_int(row[i + 2])
                        break

                for val in row:
                    rates = re.findall(r"([\d,]+\.\d{2})", val)
                    if len(rates) >= 2:
                        item["rate_per_case"] = clean_amount(rates[0])
                        item["unit_rate_per_bottle"] = clean_amount(rates[1])

                item["total_amount"] = clean_amount(row[-1])

                invoice["items"].append(item)

        # -------- TOTALS --------
        invoice["totals"] = extract_totals_block(text)
        invoice["totals"].update(extract_invoice_values_from_table(pdf))

    # -------- SAVE JSON FILE --------
    base = os.path.basename(pdf_path)
    json_name = base.replace(".pdf", ".json")

    os.makedirs("output", exist_ok=True)

    json_path = os.path.join("output", json_name)

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(invoice, f, indent=4)

    return invoice
