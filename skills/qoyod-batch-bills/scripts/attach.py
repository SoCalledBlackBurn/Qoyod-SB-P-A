"""Attach each invoice PDF to its bill, through the user's logged-in Chrome.

The Qoyod API has no attachments endpoint, so this drives the real page over
CDP. One browser tab is reused for the whole batch — reconnecting per file is
most of the wall-clock cost otherwise.

Usage:
  py -3 attach.py --dir <run_dir> [--port 9222] [--only file1.pdf]

Reads <run_dir>/log.csv, uploads every row whose attached != yes, verifies the
file is really linked, and writes the column back.
"""
import argparse
import csv
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import qoyod  # noqa: E402

qoyod.utf8_stdout()

BILL_URL = "https://app.qoyod.com/tenant/simple_bills/{}"

# A name on the page proves nothing — a failed upload can still echo the file
# name. A link to the CDN is the only evidence the file actually landed.
LINKED_JS = """n => [...document.querySelectorAll('a')].some(
    a => /cdn\\.qoyod\\.com/.test(a.href || '') && (a.innerText || '').includes(n))"""


def read_log(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def write_log(path, rows, columns):
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=columns)
        w.writeheader()
        w.writerows(rows)


def find_pdf(run_dir, name):
    for sub in ("inbox", "done", ""):
        p = os.path.join(run_dir, sub, name)
        if os.path.isfile(p):
            return p
    return None


def attach_one(page, bill_id, pdf_path):
    name = os.path.basename(pdf_path)
    page.goto(BILL_URL.format(bill_id), wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(5000)
    if "/users/sign_in" in page.url:
        raise SystemExit("this Chrome profile is not signed in to Qoyod")

    if page.evaluate(LINKED_JS, name):
        return "already"

    page.click("button.comment-attach-btn")
    page.wait_for_selector("._attach-modal", state="visible", timeout=15000)
    page.click("#up-new-tab-link")
    page.set_input_files("#dropFileInput", pdf_path)
    page.wait_for_timeout(1500)
    page.click("._attach-modal button.btn-primary")

    # Submitting reloads the page; reading document.body mid-navigation throws.
    page.wait_for_load_state("domcontentloaded", timeout=60000)
    page.wait_for_timeout(6000)
    return "attached" if page.evaluate(LINKED_JS, name) else "FAILED"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    ap.add_argument("--port", type=int, default=9222)
    ap.add_argument("--only", nargs="*", default=None)
    args = ap.parse_args()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise SystemExit("playwright missing. Run: py -3 -m pip install playwright")

    run_dir = qoyod.resolve_run_dir(args.dir)
    log_path = os.path.join(run_dir, "log.csv")
    if not os.path.exists(log_path):
        raise SystemExit(f"no log.csv in {run_dir} — record the bills first")
    rows = read_log(log_path)
    columns = list(rows[0].keys()) if rows else []

    todo = [r for r in rows
            if r.get("qoyod_bill_id") and r.get("attached") != "yes"
            and (not args.only or r["file"] in args.only)]
    if not todo:
        print(json.dumps({"attached": 0, "note": "nothing pending"}, ensure_ascii=False))
        return

    results = []
    with sync_playwright() as p:
        try:
            browser = p.chromium.connect_over_cdp(f"http://localhost:{args.port}")
        except Exception as e:
            raise SystemExit(
                f"cannot reach Chrome on port {args.port} ({e}).\n"
                "Chrome 136+ ignores --remote-debugging-port on the default profile, "
                "so it needs its own --user-data-dir. Ask the user to run:\n"
                '  Stop-Process -Name chrome -Force; & "C:\\Program Files\\Google\\Chrome'
                '\\Application\\chrome.exe" --remote-debugging-port=9222 '
                '--user-data-dir="$env:USERPROFILE\\chrome-qoyod"\n'
                "then sign in to Qoyod in that window."
            )
        page = browser.contexts[0].new_page()
        try:
            for row in todo:
                pdf = find_pdf(run_dir, row["file"])
                if not pdf:
                    results.append({"file": row["file"], "result": "pdf not found"})
                    continue
                try:
                    outcome = attach_one(page, row["qoyod_bill_id"], pdf)
                except Exception as e:
                    outcome = f"error: {type(e).__name__}: {e}"
                if outcome in ("attached", "already"):
                    row["attached"] = "yes"
                    if row.get("status") == "recorded_draft":
                        row["status"] = "done"
                results.append({"file": row["file"], "bill_id": row["qoyod_bill_id"],
                                "result": outcome})
        finally:
            page.close()

    write_log(log_path, rows, columns)
    ok = sum(1 for r in results if r["result"] in ("attached", "already"))
    print(json.dumps({"attached": ok, "total": len(results), "results": results},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
