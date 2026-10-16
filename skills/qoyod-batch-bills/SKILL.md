---
name: qoyod-batch-bills
description: Record a folder of supplier purchase invoices (فواتير مشتريات) in Qoyod (قيود) as draft Simple Bills over the API, then attach each PDF through the user's Chrome. Built for volume — tens or hundreds of invoices in one run, across several client companies. Use this whenever the user points at a folder of purchase invoices and wants them entered, recorded, booked, posted or «تسجيل الفواتير / سجّل فواتير المشتريات / دخّل الفواتير على قيود», and also when they ask to re-run, resume or finish a batch that was interrupted, to check which invoices are still missing, or to attach PDFs to bills already recorded. Prefer this over entering bills through the browser UI — the API path is far faster and cheaper — and use it even when the user names only one or two invoices, since the same scripts handle any batch size.
---

# Qoyod batch purchase bills

Turn a folder of invoice PDFs into draft Simple Bills in Qoyod, with the PDF
attached to each one and a report at the end.

The point of this skill is throughput. Bundled scripts do everything
deterministic — API calls, duplicate checks, arithmetic, VAT rules, logging,
the report. Your only job is the part that genuinely needs judgment: turning
each invoice's text into structured JSON. That division is what keeps a
200-invoice batch from costing 200 times a 1-invoice batch.

## Non-negotiables

These protect the user's books. Everything else here is advice.

1. **Drafts only.** Never approve a bill, and never create a payment or
   receipt. The user decides what gets approved.
2. **Never invent a value.** If something is unreadable, say so and let the
   invoice be blocked. A plausible guess in an accounting system is worse than
   a gap, because nobody will catch it later.
3. **Never edit or delete** an existing supplier, bill or record. If you create
   something wrong, report it and ask — don't clean it up yourself.
4. **Never record the same invoice twice.** The scripts check `log.csv` and
   Qoyod before every write, so let them.

## Running the scripts

The scripts live beside this file, in the skill's own directory — **not** in
the run directory, and not in whatever folder you happen to be in. Every
command below writes `<skill>/scripts/…`; substitute the real path, e.g.

```bash
py -3 "<skill>\scripts\prep.py" --company acme --dir "C:\work\oct-batch"
```

On Windows use `py -3`; `python` is often a Microsoft Store stub that exits
without running anything. Quote both paths — invoice folders have spaces.

## Setup

Companies live in `companies.json`: `<run_dir>/companies.json` is checked
first, then `~/.claude/qoyod/companies.json`. The first file found wins
outright — a run-directory copy overrides the global one rather than merging
with it.

```json
{
  "companies": {
    "acme": {
      "name": "شركة أكمي للتجارة",
      "name_en": "Acme Trading Co.",
      "vat": "300000000000003",
      "expense_account": "5101",
      "api_key_env": "QOYOD_KEY_ACME"
    }
  }
}
```

`name`, `vat` and `expense_account` are required, plus a key: `api_key_env`
names an environment variable holding it, or `api_key` puts it inline if the
user prefers. Optional: `name_en`, and `inventory_id` to pin the location
(otherwise the tenant's first location is used). Keys never go in the report,
the log, or your replies.

`name` stays in Arabic deliberately: it is the legal name as Qoyod holds it, and
it is matched against the customer name printed on each invoice. A translated
version will block invoices that are in fact correct. `name_en` is for display
only and is never matched.

Run directory layout — `prep.py` creates all of it on first run:

```
<run_dir>/
├── inbox/      invoices to process
├── work/       extracted text (scripts write this)
├── extracted/  one JSON per invoice (you write this)
├── done/       processed PDFs
├── errors/     blocked PDFs + a .txt saying why
├── log.csv     the record of what happened
└── reports/
```

`--dir` is the directory that *contains* `inbox/`, not the inbox itself. The
scripts correct for it when a user points at the inbox, which they usually do.

Requirements: `py -3 -m pip install pypdf playwright`. Playwright is only
needed for the attachment step, and `playwright install` is not — attaching
drives the user's own Chrome, so there's no browser to download.

## The run

### 1. Pick the company

If the user didn't name one, show what's in `companies.json` and ask. Either
way — named or asked — echo it back in one line before doing anything else:
`company: … | VAT: … | account: …`. That line is the user's chance to
catch a wrong slug while it's still free. Don't carry a company over from an
earlier conversation; booking invoices against the wrong tenant is expensive
to undo.

### 2. Prepare

```bash
py -3 <skill>/scripts/prep.py --company <slug> --dir <run_dir>
```

This extracts each PDF's text layer to `work/`, repairs Arabic that extractors
emit as reversed presentation forms, skips anything already finished in
`log.csv`, and splits the batch into `text` and `scans`.

Read the files it lists under `text` — they are plain text and cheap. **Do not
open those PDFs as images.** A page read as a picture costs roughly 20-30x its
text, which is the difference between a batch that's comfortable and one that
isn't.

Expect the text to be ugly. `prep.py` repairs the Arabic letters and word
order, but it cannot rebuild the page layout: table rows arrive flattened and
often column-reversed, so a line may read
`gross, vat, rate, net, unit_price, qty, description` instead of the order
printed on the page. Don't assume the first number is the first column. Work
out which column is which from the one that sums to the printed subtotal, and
if you can't, treat the invoice as unreadable rather than guessing.

If `scans` is non-empty, finish the text invoices first, then tell the user how
many scans there are and ask whether to read them as images. That way the cheap
work is already done while they decide.

### 3. Extract to JSON

For each text file, write `extracted/<pdf-stem>.json`, where the stem is the
PDF's filename exactly as it sits on disk, spaces and `(1)` included — that
name is how the scripts tie the JSON back to the PDF:

```json
{
  "file": "INV-001.pdf",
  "doc_type": "tax_invoice",
  "supplier": {"name_ar": "شركة …", "name_en": "…", "vat": "311111111111113"},
  "customer": {"name": "شركة …", "vat": "300000000000003"},
  "reference": "INV/2026/00123",
  "issue_date": "2026-08-31",
  "lines": [
    {"description": "رسوم إستلام المنتجات / Receiving Fees",
     "net": 47.70, "vat": 7.16, "gross": 54.86,
     "qty": 1, "unit_price": 47.70}
  ],
  "subtotal": 10448.17,
  "vat_total": 1567.23,
  "grand_total": 12015.40,
  "notes": ["anything odd you noticed"]
}
```

`doc_type` drives the VAT decision, so pick it from what the document actually
says rather than from what it probably is:

| `doc_type` | The document | Effect |
|---|---|---|
| `tax_invoice` | titled فاتورة ضريبية / Tax Invoice | 15% if both parties' names and VAT numbers are present |
| `sales_invoice_with_qr` | فاتورة مبيعات with a QR code and VAT shown separately | same as above |
| `proforma`, `quotation`, `delivery_note` | not a tax document | always 0%, flagged "get the final tax invoice" |
| `other` | anything else | always 0% |

The Arabic titles above are the text printed on the documents themselves, which
is what you match against. They are data, not prose — don't translate them.

Nothing here blocks an invoice on its own — a 0% treatment is a legitimate
outcome, recorded gross so the Qoyod total still matches the paper. Blocking
is for arithmetic that doesn't reconcile, a customer that isn't this company,
or missing fields.

Optional per line: `qty` and `unit_price`, when the invoice prints them.
Qoyod has no quantity field so they aren't recorded, but keeping them makes
a later disagreement about a line traceable to the paper. When they're
inconsistent with `net` — suppliers duplicate the net into the unit-price
column surprisingly often — record what's printed and say so in `notes`
rather than reverse-engineering what the supplier meant.

Rules for filling it in:

- **`reference` is the supplier's invoice number exactly as printed**, suffixes
  and slashes included. It's how duplicates get caught, so a "tidied up"
  version defeats the check.
- **`issue_date`** is the printed Gregorian date. Ignore any Hijri date.
- **One entry per printed line.** Never merge or drop lines — the user needs
  the Qoyod bill to look like the paper.
- **`net`** is the line's taxable total (quantity × unit price as printed in
  the net column), not the unit price. Suppliers mislabel these columns
  surprisingly often; trust the column whose values sum to the printed
  subtotal.
- **`description`** is the item text as written.
- Leave a field out rather than guessing, and put what you saw in `notes`.

Write all the JSON files, then move on. Don't narrate each invoice.

### 4. Review table

```bash
py -3 <skill>/scripts/record.py --company <slug> --dir <run_dir> --dry-run
```

Nothing is written. It validates every invoice — line sums against printed
totals, VAT against 15% of net, customer name and VAT against the company,
negative lines, missing fields — decides each invoice's tax treatment, and
reports which suppliers are new.

Each invoice comes back with an `action`:

| `action` | Meaning | What to do |
|---|---|---|
| `would record` | passed every check | goes in the table, ready to write |
| `blocked` | a `blockers` entry stopped it | move the PDF to `errors/` with a `.txt` note; never record it |
| `already recorded` | the reference exists in Qoyod | nothing to write; it may still need its PDF attached |

Show a compact table: supplier, reference, date, tax, total, and a short flag
for anything carrying a warning, a blocker, or an `already recorded`. Then say
plainly what you'll record and what you're holding back, and wait.

If every invoice comes back `already recorded`, say so instead of reporting an
empty batch — the useful next step is usually step 6, attaching PDFs to bills
that are already there. That's a fine way to run this skill on its own: `prep`
and `record --dry-run` to see where things stand, then straight to `attach`.

### 5. Record

```bash
py -3 <skill>/scripts/record.py --company <slug> --dir <run_dir>
```

Per invoice: find or create the supplier (Arabic legal name + VAT only, every
other field left empty), create the bill as a **draft**, read it back from
Qoyod, compare the saved total with the PDF, and append to `log.csv`.

A difference ≤ 0.01 SAR is per-line VAT rounding — fine, and flagged. Anything
larger leaves the bill as a draft and marks it `total_mismatch`; tell the user
and leave it alone.

### 6. Attach

```bash
py -3 <skill>/scripts/attach.py --dir <run_dir>
```

No `--company` here: this step only follows `log.csv`, which already records
which bill each PDF belongs to, and the browser is signed in to whichever
tenant the user left it on. Make sure that's the right one.

Needs Chrome listening on port 9222 and signed in to Qoyod. If it isn't, the
script prints the exact command to give the user — pass it along rather than
improvising, because **Chrome 136+ ignores `--remote-debugging-port` on the
default profile** and silently opens a normal browser with no port listening.
A separate `--user-data-dir` is the only way, and it needs a one-time Qoyod
sign-in.

Each upload is verified by finding a real `cdn.qoyod.com` link for the file —
the filename appearing on the page isn't proof.

### 7. Report and tidy

```bash
py -3 <skill>/scripts/report.py --company <slug> --dir <run_dir>
```

Then move attached invoices from `inbox/` to `done/`. Close with a few lines:
how many recorded, the totals, what went to `errors/`, and anything the user
has to decide. The report file holds the detail — don't repeat it in chat.

## Resuming

Every step is safe to re-run. `prep.py` skips what's in `log.csv`, `record.py`
checks Qoyod by reference before writing, `attach.py` skips files already
linked. After a crash or an interruption, just run the steps again — don't
reconstruct state by hand.

## When something fails mid-write

**A 5xx from a Qoyod write does not mean nothing happened.** `POST /vendors`
returns 500 on a malformed payload but still creates the contact, which is
exactly how duplicate suppliers appear. The scripts re-query before retrying
for this reason. If you ever call the API directly, do the same: query, then
decide.

If a duplicate does appear, use the oldest record, flag it in the report, and
ask the user before touching the extra one. Deleting is their call.

## API notes

`references/api-notes.md` has the payload shapes, tax ids, the Cloudflare
user-agent requirement and the attachment DOM. Read it if you need to go
outside the scripts or something returns an error they don't explain.
