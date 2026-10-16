"""Build the batch report from log.csv.

Usage: py -3 report.py --company <slug> --dir <run_dir>
Writes <run_dir>/reports/<today>.md and prints the path.
"""
import argparse
import csv
import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import qoyod  # noqa: E402

qoyod.utf8_stdout()


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--company", required=True)
    ap.add_argument("--dir", required=True)
    args = ap.parse_args()

    run_dir = qoyod.resolve_run_dir(args.dir)
    company = qoyod.load_company(args.company, run_dir)
    with open(os.path.join(run_dir, "log.csv"), encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))

    today = datetime.date.today().isoformat()
    out_dir = os.path.join(run_dir, "reports")
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, f"{today}.md")

    total_pdf = sum(num(r["pdf_total"]) for r in rows)
    total_qoyod = sum(num(r["qoyod_total"]) for r in rows)

    # The report is written in Arabic on purpose: it is read by the accountant,
    # not by this repository's English-speaking readers. Leave it that way.
    L = []
    L.append(f"الشركة: {company['name']} | الرقم الضريبي: {company['vat']} "
             f"| الحساب: {company['expense_account']}")
    L.append("")
    L.append(f"# تقرير دفعة {today}")
    L.append("")
    L.append(f"{len(rows)} فاتورة | إجمالي الـPDF {total_pdf:,.2f} "
             f"| إجمالي قيود {total_qoyod:,.2f} | فرق {total_qoyod - total_pdf:,.2f}")
    L.append("")
    L.append("| # | المورد | المرجع | التاريخ | الضريبة | إجمالي الـPDF | إجمالي قيود "
             "| فرق | مرفق | الحالة |")
    L.append("|---|---|---|---|---|---:|---:|---:|---|---|")
    for i, r in enumerate(rows, 1):
        diff = num(r["qoyod_total"]) - num(r["pdf_total"])
        mark = "✅" if abs(diff) < 0.005 else f"{diff:+.2f}"
        L.append(f"| {i} | {r['supplier']} | {r['reference']} | {r['issue_date']} "
                 f"| {r['tax_type']} | {num(r['pdf_total']):,.2f} "
                 f"| {num(r['qoyod_total']):,.2f} | {mark} "
                 f"| {'✅' if r['attached'] == 'yes' else '❌'} | {r['status']} |")

    def section(title, picked, line):
        L.append("")
        L.append(f"## {title}")
        if not picked:
            L.append("لا يوجد.")
            return
        for r in picked:
            L.append(f"- {line(r)}")

    section("الفواتير المسجلة 0% وليه",
            [r for r in rows if r["tax_type"].startswith("O")],
            lambda r: f"{r['reference']} ({r['supplier']}) — {r['notes'] or 'غير محدد'}")

    section("فروقات التقريب",
            [r for r in rows if 0 < abs(num(r['qoyod_total']) - num(r['pdf_total'])) <= 0.01],
            lambda r: f"{r['reference']}: {num(r['qoyod_total']) - num(r['pdf_total']):+.2f}")

    section("فروقات كبيرة — متعتمدش",
            [r for r in rows if abs(num(r['qoyod_total']) - num(r['pdf_total'])) > 0.01],
            lambda r: f"{r['reference']}: فرق "
                      f"{num(r['qoyod_total']) - num(r['pdf_total']):+.2f} "
                      f"(فاتورة {r['qoyod_bill_id']})")

    section("موردين متكررين",
            [r for r in rows if "share this VAT" in (r["notes"] or "")],
            lambda r: f"{r['supplier']} ({r['supplier_vat']}) — {r['notes']}")

    section("مرفقات ناقصة",
            [r for r in rows if r["attached"] != "yes"],
            lambda r: f"{r['reference']} — فاتورة {r['qoyod_bill_id']}")

    section("ملاحظات أخرى",
            [r for r in rows if r["notes"] and not r["tax_type"].startswith("O")],
            lambda r: f"{r['reference']}: {r['notes']}")

    L.append("")
    L.append("> كل الفواتير اتسجلت **مسودة**. الاعتماد قرار المستخدم.")

    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")
    print(out)


if __name__ == "__main__":
    main()
