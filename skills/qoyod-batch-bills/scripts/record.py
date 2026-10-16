"""Validate extracted invoices, then record them in Qoyod as draft bills.

Run it twice: once with --dry-run to get the review table, once without to
write. Every check lives here rather than in the model's head, so a batch of
200 invoices costs the same reasoning as a batch of 2.

Usage:
  py -3 record.py --company <slug> --dir <run_dir> --dry-run
  py -3 record.py --company <slug> --dir <run_dir> [--only file1.pdf file2.pdf]

Reads <run_dir>/extracted/*.json  (one per invoice, schema in SKILL.md)
Writes <run_dir>/log.csv and prints a JSON result per invoice.
"""
import argparse
import csv
import datetime
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import qoyod  # noqa: E402

qoyod.utf8_stdout()

CENT = 0.01
LOG_COLUMNS = ["file", "supplier", "supplier_vat", "reference", "issue_date",
               "pdf_total", "qoyod_bill_id", "qoyod_total", "tax_type",
               "attached", "status", "notes", "timestamp"]


def money(x):
    return round(float(x or 0), 2)


def normalize_name(s):
    """Loose comparison for company names: spelling variants are fine, the
    VAT number is what actually has to match."""
    drop = ["شركة", "مؤسسة", "company", "co.", "co", "est.", "est", "ltd",
            "llc", "trading", "for", "the", "&", "-", ",", "."]
    s = (s or "").lower()
    for d in drop:
        s = s.replace(d, " ")
    return " ".join(s.split())


def validate(inv, company):
    """Return (tax_id, tax_label, blockers, warnings).

    Blockers stop the invoice. Warnings travel with it into the report.
    """
    blockers, warnings = [], []

    lines = inv.get("lines") or []
    if not lines:
        blockers.append("no line items")

    for field in ("reference", "issue_date"):
        if not inv.get(field):
            blockers.append(f"missing {field}")

    net_sum = money(sum(money(li.get("net")) for li in lines))
    vat_sum = money(sum(money(li.get("vat")) for li in lines))
    subtotal = money(inv.get("subtotal"))
    vat_total = money(inv.get("vat_total"))
    grand = money(inv.get("grand_total"))

    if subtotal and abs(net_sum - subtotal) > CENT:
        blockers.append(f"lines sum {net_sum} != printed subtotal {subtotal}")
    if vat_total and abs(vat_sum - vat_total) > CENT:
        warnings.append(f"line VAT {vat_sum} vs printed VAT {vat_total}")
    if grand and abs(money(subtotal + vat_total) - grand) > CENT:
        warnings.append(f"subtotal+VAT {money(subtotal + vat_total)} vs printed total {grand}")
    if any(money(li.get("net")) < 0 for li in lines):
        blockers.append("negative line value (Qoyod rejects it) — needs a human")

    cust_vat = (inv.get("customer") or {}).get("vat") or ""
    cust_name = (inv.get("customer") or {}).get("name") or ""
    supp_vat = (inv.get("supplier") or {}).get("vat") or ""
    supp_name = (inv.get("supplier") or {}).get("name_ar") or \
                (inv.get("supplier") or {}).get("name_en") or ""

    if cust_vat and cust_vat != company["vat"]:
        blockers.append(f"customer VAT {cust_vat} != company VAT {company['vat']}")
    if cust_name and not normalize_name(cust_name):
        warnings.append("customer name unreadable")
    elif cust_name and normalize_name(company["name"]) not in normalize_name(cust_name) \
            and normalize_name(cust_name) not in normalize_name(company["name"]):
        warnings.append(f"customer name '{cust_name}' differs from '{company['name']}'")
    if not supp_name:
        blockers.append("no supplier name")

    # VAT is only reclaimable when the document actually proves both parties.
    doc_type = (inv.get("doc_type") or "").lower()
    proforma = doc_type in ("proforma", "quotation", "delivery_note")
    claimable = (
        not proforma
        and doc_type in ("tax_invoice", "sales_invoice_with_qr")
        and bool(supp_name) and bool(supp_vat)
        and bool(cust_name) and cust_vat == company["vat"]
    )
    if claimable and vat_sum <= 0:
        claimable = False
        warnings.append("tax invoice but no VAT shown — recorded out of scope")

    if claimable:
        return qoyod.TAX_15, "S 15%", blockers, warnings

    reasons = []
    if proforma:
        reasons.append(f"{doc_type} — ask the supplier for the final tax invoice")
    if not supp_vat:
        reasons.append("no supplier VAT number")
    if not cust_vat:
        reasons.append("no customer VAT number")
    elif cust_vat != company["vat"]:
        reasons.append("customer VAT mismatch")
    if doc_type not in ("tax_invoice", "sales_invoice_with_qr") and not proforma:
        reasons.append(f"not a tax invoice (doc_type={doc_type or 'unknown'})")
    warnings.append("0% because: " + "; ".join(reasons or ["conditions not met"]))
    return qoyod.TAX_OUT, "O 0%", blockers, warnings


def build_lines(inv, tax_id, account_id):
    """Out-of-scope invoices are entered gross so the Qoyod total still equals
    the paper total; 15% invoices are entered net and Qoyod adds the VAT."""
    inclusive = tax_id != qoyod.TAX_15
    out = []
    for li in inv["lines"]:
        amount = money(li.get("gross") if inclusive else li.get("net"))
        if inclusive and not amount:
            amount = money(li.get("net"))
        out.append({
            "description": (li.get("description") or "").strip()[:255],
            "total_amount": amount,
            "tax_id": tax_id,
            "is_inclusive": inclusive,
            "expense_category_id": account_id,
        })
    return out


def append_log(run_dir, row):
    path = os.path.join(run_dir, "log.csv")
    new = not os.path.exists(path)
    with open(path, "a", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=LOG_COLUMNS)
        if new:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in LOG_COLUMNS})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--company", required=True)
    ap.add_argument("--dir", required=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--only", nargs="*", default=None,
                    help="limit to these source pdf file names")
    ap.add_argument("--force-customer-mismatch", action="store_true",
                    help="user confirmed the invoice belongs to this company even "
                         "though it names another buyer; keeps it as a warning")
    args = ap.parse_args()

    run_dir = qoyod.resolve_run_dir(args.dir)
    company = qoyod.load_company(args.company, run_dir)
    api = qoyod.client_for(company)

    account = api.account_by_code(company["expense_account"])
    if not account:
        raise SystemExit(f"expense account {company['expense_account']} not found in Qoyod")

    inv_id = company.get("inventory_id")
    if not inv_id:
        invs = api.inventories()
        inv_id = invs[0]["id"] if invs else None
    if not inv_id:
        raise SystemExit("no inventory/location available")

    results = []
    for path in sorted(glob.glob(os.path.join(run_dir, "extracted", "*.json"))):
        with open(path, encoding="utf-8") as f:
            inv = json.load(f)
        if args.only and inv.get("file") not in args.only:
            continue

        tax_id, tax_label, blockers, warnings = validate(inv, company)
        if args.force_customer_mismatch:
            kept = [b for b in blockers if not b.startswith("customer VAT ")]
            for b in blockers:
                if b.startswith("customer VAT "):
                    warnings.append("FORCED by user: " + b)
            blockers = kept
        supplier = inv.get("supplier") or {}
        row = {
            "file": inv.get("file"),
            "reference": inv.get("reference"),
            "issue_date": inv.get("issue_date"),
            "supplier": supplier.get("name_ar") or supplier.get("name_en"),
            "supplier_vat": supplier.get("vat"),
            "pdf_total": money(inv.get("grand_total")),
            "tax": tax_label,
            "lines": len(inv.get("lines") or []),
            "blockers": blockers,
            "warnings": warnings,
        }

        if blockers:
            row["action"] = "blocked"
            results.append(row)
            continue

        existing = api.bill_by_reference(inv["reference"])
        if existing:
            row["action"] = "already recorded"
            row["bill_id"] = existing[0]["id"]
            row["qoyod_total"] = money(existing[0]["total"])
            results.append(row)
            continue

        known = api.vendors_by_vat(supplier.get("vat") or "___none___")
        row["vendor"] = "existing" if known else "new"
        if len(known) > 1:
            row["warnings"].append(
                f"{len(known)} vendors share this VAT (ids {[v['id'] for v in known]}) "
                "— using the oldest")

        if args.dry_run:
            row["action"] = "would record"
            results.append(row)
            continue

        vendor, _ = api.create_vendor(
            supplier.get("name_ar") or supplier.get("name_en"), supplier.get("vat") or "")
        bill, how = api.create_draft_bill(
            contact_id=vendor["id"],
            reference=inv["reference"],
            issue_date=inv["issue_date"],
            description=inv["reference"],
            line_items=build_lines(inv, tax_id, account["id"]),
            inventory_id=inv_id,
        )
        saved = api.get_bill(bill["id"])
        qtotal = money(saved["total"])
        diff = money(qtotal - money(inv.get("grand_total")))

        row.update({"action": how, "bill_id": saved["id"], "vendor_id": vendor["id"],
                    "qoyod_total": qtotal, "diff": diff,
                    "status": saved["status"]})
        if abs(diff) > CENT:
            row["warnings"].append(
                f"TOTAL MISMATCH {diff} — bill {saved['id']} left as a draft, do not approve")
        elif diff:
            row["warnings"].append(f"rounding difference {diff}")

        append_log(run_dir, {
            "file": row["file"], "supplier": row["supplier"],
            "supplier_vat": row["supplier_vat"], "reference": row["reference"],
            "issue_date": row["issue_date"], "pdf_total": row["pdf_total"],
            "qoyod_bill_id": row["bill_id"], "qoyod_total": qtotal,
            "tax_type": tax_label, "attached": "no",
            "status": "recorded_draft" if abs(diff) <= CENT else "total_mismatch",
            "notes": "; ".join(row["warnings"]),
            "timestamp": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        })
        results.append(row)

    print(json.dumps({"dry_run": args.dry_run,
                      "company": company["name"],
                      # the list endpoint says name_ar, nested copies say ar_name
                      "account": f"{account['code']} - "
                                 f"{account.get('name_ar') or account.get('ar_name')}",
                      "results": results}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
