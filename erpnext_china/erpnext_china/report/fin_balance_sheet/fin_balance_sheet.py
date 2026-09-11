# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.utils import flt, getdate, get_last_day, today
from frappe.query_builder import Case
from frappe.query_builder.functions import Coalesce, Sum, IfNull
from frappe.query_builder import Field
from pypika import Order
from erpnext.accounts.utils import get_balance_on


def execute(filters=None):
    if not filters:
        filters = {}

    if not filters.get("company"):
        filters["company"] = frappe.defaults.get_user_default("company")

    if not filters.get("month"):
        filters["month"] = get_last_day(today())

    report_date = getdate(filters.get("month"))
    period_start = getdate(report_date.replace(day=1))
    period_end = report_date
    adjustment_type = filters.get("adjustment_type", "All (Overview)")

    accounts = get_accounts(filters.get("company"))

    data = []
    asset_rows = []
    liability_rows = []

    if adjustment_type in ["All (Overview)", "Goods Issued Only"]:
        asset_rows = get_asset_adjustment_rows(period_start, period_end, filters.get("company"), accounts)
        data.extend(asset_rows)

    if adjustment_type in ["All (Overview)", "Contract Liability Only"]:
        liability_rows = get_liability_adjustment_rows(period_start, period_end, filters.get("company"), accounts)
        data.extend(liability_rows)

    summary = calculate_summary(asset_rows, liability_rows, accounts, filters.get("company"), period_end)
    columns = get_columns()
    message = build_summary_message(summary, accounts)

    return columns, data, message, None, None


def get_columns():
    return [
        {"fieldname": "adjustment_category", "label": _("Adjustment Category"), "fieldtype": "Data", "width": 140},
        {"fieldname": "scenario_label", "label": _("Scenario"), "fieldtype": "Data", "width": 120},
        {"fieldname": "source_doctype", "label": _("Source DocType"), "fieldtype": "Data", "width": 120},
        {"fieldname": "source_name", "label": _("Source Document"), "fieldtype": "Dynamic Link", "options": "source_doctype", "width": 160},
        {"fieldname": "item_code", "label": _("Item Code"), "fieldtype": "Link", "options": "Item", "width": 140},
        {"fieldname": "item_name", "label": _("Item Name"), "fieldtype": "Data", "width": 180},
        {"fieldname": "total_qty", "label": _("Total Qty"), "fieldtype": "Float", "width": 90, "precision": 2},
        {"fieldname": "settled_qty", "label": _("Settled Qty"), "fieldtype": "Float", "width": 100, "precision": 2},
        {"fieldname": "unsettled_qty", "label": _("Unsettled Qty"), "fieldtype": "Float", "width": 100, "precision": 2},
        {"fieldname": "unit_price", "label": _("Unit Price"), "fieldtype": "Currency", "width": 120},
        {"fieldname": "unsettled_amount", "label": _("Unsettled Amount"), "fieldtype": "Currency", "width": 140},
        {"fieldname": "opening_balance", "label": _("Opening Balance"), "fieldtype": "Currency", "width": 140},
        {"fieldname": "suggested_adjustment", "label": _("Suggested Adjustment"), "fieldtype": "Currency", "width": 160},
        {"fieldname": "transition_account", "label": _("Transition Account"), "fieldtype": "Link", "options": "Account", "width": 160},
        {"fieldname": "pinned", "label": _("Pinned"), "fieldtype": "Check", "width": 70},
        {"fieldname": "remarks", "label": _("Remarks"), "fieldtype": "Data", "width": 200}
    ]


def get_accounts(company):
    accounts = {
        "goods_in_transit": None,
        "cost_of_goods_sold": None,
        "contract_liability": None,
        "revenue": None,
        "inventory": None
    }

    Account = frappe.qb.DocType("Account")
    for key, search_names in {
        "goods_in_transit": ["Goods-in-Transit", "发出商品"],
        "cost_of_goods_sold": ["Cost of Goods Sold", "主营业务成本"],
        "contract_liability": ["Contract Liability", "合同负债"],
        "revenue": ["Revenue", "主营业务收入"],
        "inventory": ["Inventory", "库存商品"]
    }.items():
        for name in search_names:
            account = frappe.qb.from_(Account) \
                .select(Account.name) \
                .where((Account.company == company) & (Account.account_name.like(f"%{name}%")) & (Account.is_group == 0)) \
                .limit(1) \
                .run(as_dict=True)
            if account:
                accounts[key] = account[0].name
                break

    return accounts


def get_asset_adjustment_rows(period_start, period_end, company, accounts):
    rows = []
    DN = frappe.qb.DocType("Delivery Note")
    DNI = frappe.qb.DocType("Delivery Note Item")
    SI = frappe.qb.DocType("Sales Invoice")
    SII = frappe.qb.DocType("Sales Invoice Item")
    SO = frappe.qb.DocType("Sales Order")
    Item = frappe.qb.DocType("Item")

    settled_qty_subquery = frappe.qb.from_(SII) \
        .inner_join(SI).on(SII.parent == SI.name) \
        .select(Sum(SII.qty)) \
        .where((SII.delivery_note == DNI.parent) & (SI.docstatus == 1) & (SII.item_code == DNI.item_code))

    query = frappe.qb.from_(DNI) \
        .inner_join(DN).on(DNI.parent == DN.name) \
        .left_join(SO).on(DNI.against_sales_order == SO.name) \ 
        .inner_join(Item).on(DNI.item_code == Item.name) \
        .select(
            DNI.parent.as_("delivery_note"),
            DNI.item_code,
            DNI.item_name,
            DNI.qty.as_("total_qty"),
            DNI.amount.as_("total_amount"),
            DNI.rate.as_("unit_price"),
            settled_qty_subquery.as_("settled_qty"),
            DN.posting_date,
            DN.posting_time,
            SO.status.as_("so_status")
        ) \
        .where(
            (DN.docstatus == 1) &
            (DN.company == company) &
            (DN.posting_date <= period_end) &
            (DNI.qty > 0) &
            ((SO.status != "Closed") | (SO.status.isnull())) &
            (Item.is_stock_item == 1)
        ) \
        .orderby(DN.posting_date, order=Order.asc) \
        .orderby(DN.name, order=Order.asc)

    result = query.run(as_dict=True)

    for row in result:
        unsettled_qty = flt(row.total_qty) - flt(row.settled_qty or 0)
        if unsettled_qty <= 0:
            continue
        if flt(row.unit_price) <= 0:
            continue

        unsettled_amount = unsettled_qty * flt(row.unit_price)

        if getdate(row.posting_date) < period_start:
            scenario = _("Historical")
        else:
            scenario = _("Current Period")

        if flt(row.settled_qty or 0) > 0:
            scenario = _("Partially Settled")

        rows.append({
            "adjustment_category": _("Goods-in-Transit (Asset)"),
            "scenario_label": scenario,
            "source_doctype": "Delivery Note",
            "source_name": row.delivery_note,
            "item_code": row.item_code,
            "item_name": row.item_name,
            "total_qty": row.total_qty,
            "settled_qty": row.settled_qty or 0,
            "unsettled_qty": unsettled_qty,
            "unit_price": row.unit_price,
            "unsettled_amount": unsettled_amount,
            "opening_balance": 0,
            "suggested_adjustment": unsettled_amount,
            "transition_account": accounts.get("goods_in_transit"),
            "pinned": 0,
            "remarks": ""
        })

    return rows


def get_liability_adjustment_rows(period_start, period_end, company, accounts):
    rows = []
    SI = frappe.qb.DocType("Sales Invoice")
    SII = frappe.qb.DocType("Sales Invoice Item")
    DN = frappe.qb.DocType("Delivery Note")
    DNI = frappe.qb.DocType("Delivery Note Item")
    Item = frappe.qb.DocType("Item")

    settled_qty_subquery = frappe.qb.from_(DNI) \
        .inner_join(DN).on(DNI.parent == DN.name) \
        .select(Sum(DNI.qty)) \
        .where((DNI.against_sales_invoice == SII.parent) & (DN.docstatus == 1) & (DNI.item_code == SII.item_code))

    returned_qty_subquery = frappe.qb.from_(DNI) \
        .inner_join(DN).on(DNI.parent == DN.name) \
        .select(Sum(DNI.qty)) \
        .where((DNI.against_sales_invoice == SII.parent) & (DN.docstatus == 1) & (DN.is_return == 1) & (DNI.item_code == SII.item_code))

    query = frappe.qb.from_(SII) \
        .inner_join(SI).on(SII.parent == SI.name) \
        .inner_join(Item).on(SII.item_code == Item.name) \
        .select(
            SII.parent.as_("sales_invoice"),
            SII.item_code,
            SII.item_name,
            SII.qty.as_("total_qty"),
            SII.amount.as_("total_amount"),
            SII.rate.as_("unit_price"),
            settled_qty_subquery.as_("settled_qty"),
            returned_qty_subquery.as_("returned_qty"),
            SI.posting_date,
            SI.is_return,
            SI.is_debit_note,
            SI.return_against
        ) \
        .where(
            (SI.docstatus == 1) &
            (SI.company == company) &
            (SI.posting_date <= period_end) &
            (SI.is_return == 0) &
            (SI.is_debit_note == 0) &
            (SII.qty > 0) &
            (Item.is_stock_item == 1)
        ) \
        .orderby(SI.posting_date, order=Order.asc) \
        .orderby(SI.name, order=Order.asc)

    result = query.run(as_dict=True)

    for row in result:
        unsettled_qty = flt(row.total_qty) - flt(row.settled_qty or 0)
        if unsettled_qty <= 0:
            continue
        if flt(row.returned_qty or 0) >= flt(row.total_qty):
            continue

        unsettled_amount = unsettled_qty * flt(row.unit_price)

        if getdate(row.posting_date) < period_start:
            scenario = _("Historical")
        else:
            scenario = _("Current Period")

        if flt(row.settled_qty or 0) > 0:
            scenario = _("Partially Settled")

        rows.append({
            "adjustment_category": _("Contract Liability (Liability)"),
            "scenario_label": scenario,
            "source_doctype": "Sales Invoice",
            "source_name": row.sales_invoice,
            "item_code": row.item_code,
            "item_name": row.item_name,
            "total_qty": row.total_qty,
            "settled_qty": row.settled_qty or 0,
            "unsettled_qty": unsettled_qty,
            "unit_price": row.unit_price,
            "unsettled_amount": unsettled_amount,
            "opening_balance": 0,
            "suggested_adjustment": unsettled_amount,
            "transition_account": accounts.get("contract_liability"),
            "pinned": 0,
            "remarks": ""
        })

    return rows


def calculate_summary(asset_rows, liability_rows, accounts, company, posting_date):
    target_asset = sum([flt(row.get("unsettled_amount", 0)) for row in asset_rows if not row.get("pinned")])
    target_liability = sum([flt(row.get("unsettled_amount", 0)) for row in liability_rows if not row.get("pinned")])

    book_asset = 0
    book_liability = 0

    if accounts.get("goods_in_transit"):
        try:
            book_asset = flt(get_balance_on(
                account=accounts["goods_in_transit"],
                posting_date=posting_date,
                company=company
            ) or 0)
        except Exception:
            frappe.log_error(f"Failed to get balance for {accounts['goods_in_transit']}", "Income Cost Matching Report")
            book_asset = 0

    if accounts.get("contract_liability"):
        try:
            book_liability = flt(get_balance_on(
                account=accounts["contract_liability"],
                posting_date=posting_date,
                company=company
            ) or 0)
        except Exception:
            frappe.log_error(f"Failed to get balance for {accounts['contract_liability']}", "Income Cost Matching Report")
            book_liability = 0

    return {
        "target_asset": target_asset,
        "target_liability": target_liability,
        "book_asset": book_asset,
        "book_liability": book_liability,
        "asset_diff": target_asset - book_asset,
        "liability_diff": target_liability - book_liability,
        "currency": frappe.get_cached_value("Company", company, "default_currency") or "CNY"
    }


def build_summary_message(summary, accounts):
    html = """
    <div style="margin-top: 20px; padding: 15px; background: #f5f7fa; border: 1px solid #d1d8dd; border-radius: 4px;">
        <h4 style="margin-top: 0;">{summary_title}</h4>
        <table style="width: 100%; border-collapse: collapse;">
            <tr>
                <th style="text-align: left; padding: 5px;">{item}</th>
                <th style="text-align: right; padding: 5px;">{target_label}</th>
                <th style="text-align: right; padding: 5px;">{book_label}</th>
                <th style="text-align: right; padding: 5px;">{adjustment_label}</th>
                <th style="text-align: left; padding: 5px;">{direction_label}</th>
            </tr>
    """.format(
        summary_title=_("Summary Adjustment Suggestion"),
        item=_("Item"),
        target_label=_("Target Balance"),
        book_label=_("Book Balance"),
        adjustment_label=_("Suggested Adjustment"),
        direction_label=_("Direction")
    )

    if accounts.get("goods_in_transit"):
        asset_dir = _("Dr Goods-in-Transit / Cr COGS") if summary["asset_diff"] > 0 else \
                    (_("Dr COGS / Cr Goods-in-Transit") if summary["asset_diff"] < 0 else _("No adjustment"))
        html += """
            <tr>
                <td style="padding: 5px;"><b>{account}</b></td>
                <td style="text-align: right; padding: 5px;">{target_asset:,.2f}</td>
                <td style="text-align: right; padding: 5px;">{book_asset:,.2f}</td>
                <td style="text-align: right; padding: 5px; font-weight: bold; color: {asset_color};">{asset_diff:+,.2f}</td>
                <td style="padding: 5px;">{asset_dir}</td>
            </tr>
        """.format(
            account=_("Goods-in-Transit"),
            target_asset=summary["target_asset"],
            book_asset=summary["book_asset"],
            asset_diff=summary["asset_diff"],
            asset_color="#1a73e8" if summary["asset_diff"] != 0 else "#333",
            asset_dir=asset_dir
        )

    if accounts.get("contract_liability"):
        liab_dir = _("Dr Revenue / Cr Contract Liability") if summary["liability_diff"] > 0 else \
                   (_("Dr Contract Liability / Cr Revenue") if summary["liability_diff"] < 0 else _("No adjustment"))
        html += """
            <tr>
                <td style="padding: 5px;"><b>{account}</b></td>
                <td style="text-align: right; padding: 5px;">{target_liability:,.2f}</td>
                <td style="text-align: right; padding: 5px;">{book_liability:,.2f}</td>
                <td style="text-align: right; padding: 5px; font-weight: bold; color: {liab_color};">{liability_diff:+,.2f}</td>
                <td style="padding: 5px;">{liab_dir}</td>
            </tr>
        """.format(
            account=_("Contract Liability"),
            target_liability=summary["target_liability"],
            book_liability=summary["book_liability"],
            liability_diff=summary["liability_diff"],
            liab_color="#e37400" if summary["liability_diff"] != 0 else "#333",
            liab_dir=liab_dir
        )

    html += """
        </table>
        <div style="margin-top: 10px; color: #666; font-size: 12px;">
            <i>{hint}</i>
        </div>
    </div>
    """.format(hint=_("Hint: Click the 'Generate Adjustment Entry' button above to create the journal entry with PDF attachment."))

    return html


@frappe.whitelist()
def get_adjustment_summary(filters):
    if isinstance(filters, str):
        import json
        filters = json.loads(filters)

    if not filters.get("company"):
        filters["company"] = frappe.defaults.get_user_default("company")
    if not filters.get("month"):
        filters["month"] = get_last_day(today())

    period_end = getdate(filters.get("month"))
    period_start = getdate(period_end.replace(day=1))
    accounts = get_accounts(filters.get("company"))

    asset_rows = get_asset_adjustment_rows(period_start, period_end, filters.get("company"), accounts)
    liability_rows = get_liability_adjustment_rows(period_start, period_end, filters.get("company"), accounts)

    summary = calculate_summary(asset_rows, liability_rows, accounts, filters.get("company"), period_end)
    summary["currency"] = frappe.get_cached_value("Company", filters.get("company"), "default_currency") or "CNY"
    return summary


@frappe.whitelist()
def generate_adjustment_entry(filters):
    if isinstance(filters, str):
        import json
        filters = json.loads(filters)

    try:
        summary = get_adjustment_summary(filters)
        if summary["asset_diff"] == 0 and summary["liability_diff"] == 0:
            return {"status": "skipped", "message": _("No adjustment needed for this period")}

        accounts = get_accounts(filters.get("company"))
        je = frappe.new_doc("Journal Entry")
        je.voucher_type = "Journal Entry"
        je.posting_date = getdate(filters.get("month"))
        je.company = filters.get("company")
        je.user_remark = _("Cross-period revenue/cost matching adjustment - {0}").format(filters.get("month"))

        if summary["asset_diff"] != 0 and accounts.get("goods_in_transit") and accounts.get("cost_of_goods_sold"):
            if summary["asset_diff"] > 0:
                je.append("accounts", {
                    "account": accounts["goods_in_transit"],
                    "debit_in_account_currency": summary["asset_diff"],
                    "credit_in_account_currency": 0
                })
                je.append("accounts", {
                    "account": accounts["cost_of_goods_sold"],
                    "debit_in_account_currency": 0,
                    "credit_in_account_currency": summary["asset_diff"]
                })
            else:
                abs_val = abs(summary["asset_diff"])
                je.append("accounts", {
                    "account": accounts["cost_of_goods_sold"],
                    "debit_in_account_currency": abs_val,
                    "credit_in_account_currency": 0
                })
                je.append("accounts", {
                    "account": accounts["goods_in_transit"],
                    "debit_in_account_currency": 0,
                    "credit_in_account_currency": abs_val
                })

        if summary["liability_diff"] != 0 and accounts.get("contract_liability") and accounts.get("revenue"):
            if summary["liability_diff"] > 0:
                je.append("accounts", {
                    "account": accounts["revenue"],
                    "debit_in_account_currency": summary["liability_diff"],
                    "credit_in_account_currency": 0
                })
                je.append("accounts", {
                    "account": accounts["contract_liability"],
                    "debit_in_account_currency": 0,
                    "credit_in_account_currency": summary["liability_diff"]
                })
            else:
                abs_val = abs(summary["liability_diff"])
                je.append("accounts", {
                    "account": accounts["contract_liability"],
                    "debit_in_account_currency": abs_val,
                    "credit_in_account_currency": 0
                })
                je.append("accounts", {
                    "account": accounts["revenue"],
                    "debit_in_account_currency": 0,
                    "credit_in_account_currency": abs_val
                })

        je.insert()
        attach_report_pdf(je.name, filters)
        return {"status": "success", "je_name": je.name}

    except Exception as e:
        frappe.log_error(frappe.get_traceback(), _("Failed to generate adjustment entry"))
        return {"status": "error", "message": str(e)}


def attach_report_pdf(je_name, filters):
    try:
        period_end = getdate(filters.get("month"))
        period_start = getdate(period_end.replace(day=1))
        accounts = get_accounts(filters.get("company"))

        asset_rows = get_asset_adjustment_rows(period_start, period_end, filters.get("company"), accounts)
        liability_rows = get_liability_adjustment_rows(period_start, period_end, filters.get("company"), accounts)
        data = asset_rows + liability_rows
        summary = calculate_summary(asset_rows, liability_rows, accounts, filters.get("company"), period_end)

        html = build_pdf_html(filters, data, summary, accounts)
        from frappe.utils.pdf import get_pdf
        pdf_bytes = get_pdf(html)

        file_name = _("Adjustment_Support_{0}.pdf").format(filters.get("month"))
        file_doc = frappe.get_doc({
            "doctype": "File",
            "file_name": file_name,
            "attached_to_doctype": "Journal Entry",
            "attached_to_name": je_name,
            "content": pdf_bytes,
            "is_private": 1
        })
        file_doc.insert()
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), _("Failed to attach PDF"))


def build_pdf_html(filters, data, summary, accounts):
    company = filters.get("company")
    period = filters.get("month")
    currency = summary.get("currency", "CNY")

    html = """
    <html>
    <head>
        <meta charset="utf-8">
        <style>
            body {{ font-family: "SimSun", "Microsoft YaHei", sans-serif; font-size: 11px; padding: 20px; }}
            h1 {{ text-align: center; font-size: 16px; margin-bottom: 5px; }}
            .subtitle {{ text-align: center; font-size: 12px; color: #666; margin-bottom: 20px; }}
            table {{ width: 100%; border-collapse: collapse; margin-top: 10px; }}
            th {{ background: #f0f0f0; border: 1px solid #ccc; padding: 5px; text-align: center; font-weight: bold; }}
            td {{ border: 1px solid #ccc; padding: 4px 5px; text-align: center; }}
            .text-left {{ text-align: left; }}
            .text-right {{ text-align: right; }}
            .summary {{ margin-top: 20px; padding: 10px; background: #f9f9f9; border: 1px solid #ddd; }}
            .summary-table {{ width: 60%; margin: 0 auto; }}
            .footer {{ margin-top: 30px; text-align: center; font-size: 10px; color: #999; }}
            .asset-row {{ background-color: #f0f7ff; }}
            .liability-row {{ background-color: #fff7f0; }}
        </style>
    </head>
    <body>
        <h1>{title}</h1>
        <div class="subtitle">{company} | {period} | {currency}</div>
    """.format(
        title=_("Income Cost Matching Adjustment Report"),
        company=company,
        period=period,
        currency=currency
    )

    if data:
        html += """
        <table>
            <thead>
                <tr>
                    <th style="width:10%;">{col1}</th>
                    <th style="width:8%;">{col2}</th>
                    <th style="width:10%;">{col3}</th>
                    <th style="width:12%;">{col4}</th>
                    <th style="width:10%;">{col5}</th>
                    <th style="width:8%;">{col6}</th>
                    <th style="width:8%;">{col7}</th>
                    <th style="width:8%;">{col8}</th>
                    <th style="width:10%;">{col9}</th>
                    <th style="width:10%;">{col10}</th>
                </tr>
            </thead>
            <tbody>
        """.format(
            col1=_("Category"),
            col2=_("Scenario"),
            col3=_("DocType"),
            col4=_("Document"),
            col5=_("Item"),
            col6=_("Unsettled Qty"),
            col7=_("Unit Price"),
            col8=_("Unsettled Amount"),
            col9=_("Transition Account"),
            col10=_("Remarks")
        )

        for row in data:
            if row.get("pinned"):
                continue
            row_class = "asset-row" if "Goods-in-Transit" in row.get("adjustment_category", "") else "liability-row"
            html += """
                <tr class="{row_class}">
                    <td>{category}</td>
                    <td>{scenario}</td>
                    <td>{doctype}</td>
                    <td>{name}</td>
                    <td>{item}</td>
                    <td>{qty}</td>
                    <td class="text-right">{price}</td>
                    <td class="text-right">{amount}</td>
                    <td>{account}</td>
                    <td>{remarks}</td>
                </tr>
            """.format(
                row_class=row_class,
                category=row.get("adjustment_category", ""),
                scenario=row.get("scenario_label", ""),
                doctype=row.get("source_doctype", ""),
                name=row.get("source_name", ""),
                item=row.get("item_code", "") + " " + (row.get("item_name", "") or ""),
                qty=flt(row.get("unsettled_qty", 0)),
                price=flt(row.get("unit_price", 0)),
                amount=flt(row.get("unsettled_amount", 0)),
                account=row.get("transition_account", ""),
                remarks=row.get("remarks", "")
            )

        html += """
            </tbody>
        </table>
        """

    html += """
        <div class="summary">
            <h4 style="text-align:center;margin-top:0;">{summary_title}</h4>
            <table class="summary-table">
                <tr>
                    <td><b>{label1}</b></td>
                    <td class="text-right">{target_asset:,.2f}</td>
                    <td class="text-right">{book_asset:,.2f}</td>
                    <td class="text-right" style="font-weight:bold;color:{asset_color};">{asset_diff:+,.2f}</td>
                </tr>
                <tr>
                    <td><b>{label2}</b></td>
                    <td class="text-right">{target_liability:,.2f}</td>
                    <td class="text-right">{book_liability:,.2f}</td>
                    <td class="text-right" style="font-weight:bold;color:{liab_color};">{liability_diff:+,.2f}</td>
                </tr>
            </table>
            <div style="text-align:center;margin-top:10px;font-size:10px;color:#666;">
                {note}
            </div>
        </div>
    """.format(
        summary_title=_("Summary Adjustment Suggestion"),
        label1=_("Goods-in-Transit (Target → Book → Diff)"),
        label2=_("Contract Liability (Target → Book → Diff)"),
        target_asset=summary.get("target_asset", 0),
        book_asset=summary.get("book_asset", 0),
        asset_diff=summary.get("asset_diff", 0),
        asset_color="#1a73e8" if summary.get("asset_diff", 0) != 0 else "#333",
        target_liability=summary.get("target_liability", 0),
        book_liability=summary.get("book_liability", 0),
        liability_diff=summary.get("liability_diff", 0),
        liab_color="#e37400" if summary.get("liability_diff", 0) != 0 else "#333",
        note=_("Note: Positive means Dr (asset) or Cr (liability); negative means Cr (asset) or Dr (liability).")
    )

    html += """
        <div class="footer">{generated} {datetime}</div>
    </body>
    </html>
    """.format(
        generated=_("Generated on"),
        datetime=frappe.utils.now()
    )

    return html