import json
import re
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle


INPUT_JSON = Path("old_stock_from_db.json")
OUTPUT_PDF = Path("ICDC_OLD_STOCK_UPLOAD.pdf")


def money(v):
    try:
        return f"{float(v):,.2f}"
    except Exception:
        return "0.00"


def to_int(v):
    try:
        return int(v)
    except Exception:
        return 0


def build_pdf():
    data = json.loads(INPUT_JSON.read_text(encoding="utf-8"))
    block = (data.get("old_stock_invoices") or [])[0]
    invoice = block.get("invoice", {})
    items = block.get("invoice_items", [])
    totals = (block.get("invoice_totals") or [{}])[0]

    doc = SimpleDocTemplate(
        str(OUTPUT_PDF),
        pagesize=A4,
        leftMargin=24,
        rightMargin=24,
        topMargin=24,
        bottomMargin=24,
    )
    styles = getSampleStyleSheet()
    story = []

    raw_invoice_no = str(invoice.get("invoice_number") or "113")
    digits_only = "".join(re.findall(r"\d+", raw_invoice_no)) or "113"
    invoice_no = f"ICDC{digits_only}"

    story.append(Paragraph(f"<b>TSBCL TAX INVOICE</b>", styles["Title"]))
    story.append(Spacer(1, 6))
    story.append(Paragraph(f"Invoice Number: {invoice_no}", styles["Normal"]))
    story.append(Paragraph(f"Invoice Date: {invoice.get('invoice_date', '')}", styles["Normal"]))
    story.append(Paragraph(f"Name: {invoice.get('retailer_name', 'Jilla Wines 1')}  Code: 2500552", styles["Normal"]))
    story.append(Paragraph(f"PAN: {invoice.get('licensee_pan', 'AAAPL1234C')}", styles["Normal"]))
    story.append(Spacer(1, 10))

    small = styles["BodyText"].clone("small")
    small.fontSize = 7
    small.leading = 8

    headers = [
        "Sl No",
        "Brand Number",
        "Brand Name",
        "Product Type",
        "Pack Type",
        "Pack Size",
        "Cases",
        "Bottles",
        "Rate(Case/Bottle)",
        "Total Amount",
    ]
    table_data = [[Paragraph(f"<b>{h}</b>", small) for h in headers]]

    for row in items:
        pack_case = to_int(row.get("pack_size_case"))
        pack_ml = to_int(row.get("pack_size_quantity_ml"))
        cases = to_int(row.get("cases_delivered"))
        bottles = to_int(row.get("bottles_delivered"))
        rate_case = money(row.get("rate_per_case"))
        rate_bottle = money(row.get("unit_rate_per_bottle"))
        total_amount = money(row.get("total_amount"))

        table_data.append(
            [
                Paragraph(str(to_int(row.get("sl_no"))), small),
                Paragraph(str(row.get("brand_number") or ""), small),
                Paragraph(str(row.get("brand_name") or ""), small),
                Paragraph(str(row.get("product_type") or ""), small),
                Paragraph(str(row.get("pack_type") or ""), small),
                Paragraph(f"{pack_case} / {pack_ml} ml", small),
                Paragraph(str(cases), small),
                Paragraph(str(bottles), small),
                Paragraph(f"{rate_case}<br/>{rate_bottle}", small),
                Paragraph(total_amount, small),
            ]
        )

    table = Table(
        table_data,
        repeatRows=1,
        colWidths=[24, 46, 170, 42, 30, 54, 26, 30, 58, 40],
    )
    table.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.5, colors.black),
                ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 7),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 3),
                ("RIGHTPADDING", (0, 0), (-1, -1), 3),
                ("TOPPADDING", (0, 0), (-1, -1), 2),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ]
        )
    )
    story.append(table)
    story.append(Spacer(1, 12))

    story.append(Paragraph(f"e-challan / DD Amount: {money(totals.get('e_challan_amount'))}", styles["Normal"]))
    story.append(Paragraph(f"Previous Credit: {money(totals.get('previous_credit'))}", styles["Normal"]))
    story.append(Paragraph(f"Sub Total: {money(totals.get('sub_total'))}", styles["Normal"]))
    story.append(Paragraph(f"Special Excise Cess: {money(totals.get('special_excise_cess'))}", styles["Normal"]))
    story.append(Paragraph(f"TCS: {money(totals.get('tcs'))}", styles["Normal"]))
    story.append(Paragraph(f"New Retailer Professional Tax: {money(totals.get('new_retailer_professional_tax'))}", styles["Normal"]))
    story.append(Paragraph(f"Retail Shop Excise Turnover Tax: {money(totals.get('retail_shop_excise_turnover_tax'))}", styles["Normal"]))
    story.append(Paragraph(f"Less this Invoice Value: {money(totals.get('less_this_invoice_value'))}", styles["Normal"]))
    story.append(Paragraph(f"Retailer Credit Balance Rs. {money(totals.get('retailer_credit_balance'))}", styles["Normal"]))
    story.append(Spacer(1, 8))
    story.append(Paragraph(f"Invoice Value: {money(totals.get('invoice_value'))}", styles["Normal"]))
    story.append(Paragraph(f"Rounding Off: {money(totals.get('mrp_round_off'))}", styles["Normal"]))
    story.append(Paragraph(f"Net Invoice Value: {money(totals.get('net_invoice_value'))}", styles["Normal"]))

    doc.build(story)
    print(f"Created {OUTPUT_PDF}")


if __name__ == "__main__":
    build_pdf()
