"""Prepare a batch: extract PDF text, split text-PDFs from scans, skip duplicates.

This is the step that keeps a big batch cheap. Reading a PDF page as an image
costs roughly 20-30x what reading its text layer costs, so anything with an
embedded text layer never needs to be looked at as a picture.

Usage:
  py -3 prep.py --company <slug> --dir <run_dir> [--limit N]

Writes <run_dir>/work/<stem>.txt for every text PDF and prints a JSON summary:
  {"company": {...}, "text": [...], "scans": [...], "skipped": [...]}
"""
import argparse
import csv
import json
import os
import re
import sys
import unicodedata

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import qoyod  # noqa: E402

qoyod.utf8_stdout()

ARABIC = re.compile(r"[؀-ۿݐ-ݿﭐ-﷿ﹰ-﻿]")
PRESENTATION = re.compile(r"[ﭐ-﷿ﹰ-﻿]")
JOINERS = " ـًٌٍَُِّْ"

# Below this many characters a PDF is effectively a scan: the text layer is
# empty or just a stray header, so the numbers have to be read from the image.
TEXT_THRESHOLD = 200


def unshape_arabic(line):
    """Turn PDF presentation-form Arabic back into normal logical-order Arabic.

    Extractors emit Arabic as shaped glyphs in visual (right-to-left) order.
    NFKC maps a ligature like ﴍ to two letters, so reverse the ORIGINAL
    characters and expand each in place; reversing the expanded string would
    scramble every ligature (شركة would come out رشكة).
    """
    out, run = [], []

    def flush():
        if run:
            out.append("".join(unicodedata.normalize("NFKC", c) for c in reversed(run)))
            run.clear()

    for ch in line:
        if ARABIC.match(ch) or (run and ch in JOINERS):
            run.append(ch)
        else:
            flush()
            out.append(ch)
    flush()
    return "".join(out)


def rejoin_rtl_runs(lines):
    """Put right-to-left word order back.

    These PDFs emit one word per line in visual order, so a supplier called
    «شركة أكمي للنقل والتخزين» arrives as four lines reading
    «والتخزين / للنقل / أكمي / شركة». Each line is correct on its
    own, which makes the damage easy to miss — and a reversed supplier name is
    exactly the kind of thing that gets written into the books.

    Consecutive Arabic-only lines belong to one visual run, so reverse the run
    and join it. Anything with Latin letters or digits ends the run.
    """
    out, run = [], []

    def flush():
        if run:
            out.append(" ".join(reversed(run)))
            run.clear()

    for line in lines:
        if ARABIC.search(line) and not re.search(r"[A-Za-z0-9]", line):
            run.append(line)
        else:
            flush()
            out.append(line)
    flush()
    return out


def extract_text(path):
    try:
        import pypdf
    except ImportError:
        raise SystemExit("pypdf missing. Run: py -3 -m pip install pypdf")
    try:
        reader = pypdf.PdfReader(path)
        raw = "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception as e:  # encrypted, corrupt, whatever — treat as a scan
        return "", f"unreadable: {type(e).__name__}"
    shaped = bool(PRESENTATION.search(raw))
    if shaped:
        raw = "\n".join(unshape_arabic(line) for line in raw.split("\n"))
    # Collapse the blank-line soup these extractors produce; it is pure cost.
    lines = [ln for ln in (ln.strip() for ln in raw.split("\n")) if ln]
    if shaped:
        lines = rejoin_rtl_runs(lines)
    return "\n".join(lines), None


def logged_files(run_dir):
    """Files already finished in a previous run, so a rerun is cheap and safe."""
    path = os.path.join(run_dir, "log.csv")
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8-sig", newline="") as f:
        return {
            row["file"]: row
            for row in csv.DictReader(f)
            if row.get("status") in ("done", "recorded_draft")
        }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--company", required=True)
    ap.add_argument("--dir", required=True, help="run directory containing inbox/")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    run_dir = qoyod.resolve_run_dir(args.dir)
    inbox = os.path.join(run_dir, "inbox")
    work = os.path.join(run_dir, "work")
    # Make the whole layout here so later steps never have to think about it.
    for sub in ("inbox", "work", "extracted", "done", "errors", "reports"):
        os.makedirs(os.path.join(run_dir, sub), exist_ok=True)

    company = qoyod.load_company(args.company, run_dir)
    done = logged_files(run_dir)

    pdfs = sorted(
        f for f in os.listdir(inbox)
        if f.lower().endswith(".pdf") and os.path.isfile(os.path.join(inbox, f))
    ) if os.path.isdir(inbox) else []
    if args.limit:
        pdfs = pdfs[:args.limit]

    text_files, scans, skipped = [], [], []
    for name in pdfs:
        if name in done:
            skipped.append({"file": name, "why": "already in log.csv",
                            "bill_id": done[name].get("qoyod_bill_id")})
            continue
        src = os.path.join(inbox, name)
        text, err = extract_text(src)
        stem = os.path.splitext(name)[0]
        if err or len(text) < TEXT_THRESHOLD:
            scans.append({"file": name, "chars": len(text),
                          "why": err or "no usable text layer"})
            continue
        out = os.path.join(work, stem + ".txt")
        with open(out, "w", encoding="utf-8") as f:
            f.write(text)
        text_files.append({"file": name, "text": os.path.relpath(out, run_dir),
                           "chars": len(text)})

    # Resolve the expense account to its name. The code alone ("5101") is the
    # one thing a user cannot sanity-check, and booking logistics fees to Cost
    # of Goods Sold is the kind of mistake that only shows up at the close.
    account_label = str(company.get("expense_account"))
    try:
        acc = qoyod.client_for(company).account_by_code(company["expense_account"])
        account_label = (f"{acc['code']} - {acc.get('name_ar') or acc.get('ar_name')}"
                         if acc else f"{account_label} — NOT FOUND in Qoyod")
    except SystemExit:
        raise
    except Exception as e:
        account_label += f" (could not verify: {type(e).__name__})"

    print(json.dumps({
        "company": {k: v for k, v in company.items()
                    if k not in ("api_key", "_config_path")},
        "expense_account_resolved": account_label,
        "run_dir": run_dir,
        "counts": {"text": len(text_files), "scans": len(scans),
                   "skipped": len(skipped)},
        "text": text_files,
        "scans": scans,
        "skipped": skipped,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
