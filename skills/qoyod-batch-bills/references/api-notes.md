# Qoyod API v2.0 — field notes

> The Arabic in this file is data, not prose: tax labels as Qoyod returns them,
> a button's own caption, and the strings that demonstrate the text-extraction
> bug at the end. Translating any of it would make the notes wrong.

Everything here was confirmed against a live tenant, first on 2026-10-15 and
again on 2026-10-16. The public docs at apidoc.qoyod.com are a JS app and don't
render these payloads, which is why this file exists.

## Contents
- [Transport](#transport)
- [Reads](#reads)
- [Tax ids](#tax-ids)
- [Creating a vendor](#creating-a-vendor)
- [Creating a simple bill](#creating-a-simple-bill)
- [Approving a bill](#approving-a-bill)
- [Payments and allocations](#payments-and-allocations)
- [Attachments](#attachments)
- [Chrome / CDP](#chrome--cdp)

## Transport

Base URL `https://api.qoyod.com/2.0`, auth via an `API-KEY` header.

**`https://www.qoyod.com/api/2.0` is retired.** It answers `401` with
`{"error": "This API endpoint has been retired."}` for any key, which looks
exactly like a bad key and sends you off checking credentials. If a script
suddenly 401s everywhere, check its base URL before anything else.

Cloudflare sits in front and bans unknown user agents with **error 1010**
("browser_signature_banned"). Python's default `python-urllib/3.x` is banned;
send a normal browser `User-Agent` instead. The error says "do not retry" and
means it — retrying with the same agent will not help.

List endpoints page at 100 per page via `?page=N`. There is **no `meta` or
`pagination` block at all** on these responses, so page until a short page
comes back.

**`GET /accounts` ignores `?page` and returns the whole chart in one response**
(117 rows on the reference tenant, i.e. more than 100). A "page until short
page" loop therefore never terminates on it — it re-fetches the same full list
forever and silently multiplies the row count. Either special-case it or cap
the loop.

## Reads

| Path | Notes |
|---|---|
| `GET /accounts` | chart of accounts; match on `code` |
| `GET /vendors` | `name`, `tax_number`; **no VAT search**, filter client-side |
| `GET /simple_bills` | includes `status`, `reference`, `total`, `line_items` |
| `GET /simple_bills/{id}` | read-back for verification |
| `GET /taxes` | tax ids |
| `GET /inventories` | locations |
| `GET /products` | products |
| `GET /bills` | the *other* bill kind; separate id space from simple bills |
| `GET /receipts` | payments in and out |
| `GET /bill_payments` | payments out only; returns them under a `receipts` key |
| `GET /credit_notes`, `GET /debit_notes` | |
| `GET /journal_entries` | |

404 on this tenant: `/organization`, `/company`, `/organizations`, `/users`,
`/tax_types`, `/locations`, `/product_categories`, `/payments`, `/employees`,
`/assets`, `/delivery_notes`, `/purchase_orders`, `/quotations`,
`/stock_transactions`, `/account_statements`.

There is no endpoint that returns the tenant's own name or VAT number, so the
company identity has to be configured, not discovered. Locations come from
`/inventories`, not `/locations`.

## Tax ids

| id | code | name | % |
|---|---|---|---|
| 1 | S | ضريبة القيمة المضافة | 15.0 |
| 2 | Z | الضريبة الصفرية | 0.0 |
| 3 | E | معفاة من الضريبة | 0.0 |
| 4 | O | غير خاضعة | 0.0 |

## Creating a vendor

```json
POST /2.0/vendors
{"contact": {"name": "شركة …", "tax_number": "311111111111113", "type": "Vendor"}}
```

Response is `201` with a `contact` object.

**A `vendor` wrapper returns `500 Internal Server Error` and creates the
contact anyway.** So does an otherwise-malformed payload. Treat any non-2xx
from this endpoint as "unknown outcome": re-query `GET /vendors` by VAT before
retrying, or you get silent duplicate suppliers.

## Creating a simple bill

```json
POST /2.0/simple_bills
{
  "simple_bill": {
    "contact_id": 170,
    "inventory_id": 1,
    "reference": "INV/2026/00459",
    "description": "INV/2026/00459",
    "issue_date": "2026-08-31",
    "status": "Draft",
    "simple_bill_items_attributes": [
      {
        "description": "رسوم إستلام المنتجات",
        "total_amount": 47.70,
        "tax_id": 1,
        "is_inclusive": false,
        "expense_category_id": 12
      }
    ]
  }
}
```

The errors you get while guessing this, in the order they appear:

| Error | Fix |
|---|---|
| `inventory_id is missing` | goes on the bill, not on the lines |
| `invalid simple_bill_items_attributes json body` | the key is `simple_bill_items_attributes`, not `line_items` |
| `expense_category_id missing in a line item` | lines take `expense_category_id`, even though reads return `expense_account` |

`status: "Draft"` is accepted and is what we always want. There is no quantity
field: `total_amount` is the line's net total (or gross with
`is_inclusive: true`). Qoyod computes VAT and the grand total itself, so always
read the bill back and compare with the PDF.

## Approving a bill

Only relevant if the user explicitly asks — the skill's default is drafts only.

```json
PUT /2.0/simple_bills/{id}
{"simple_bill": {"status": "Approved"}}
```

Returns `200` with the updated bill. It is **draft-only and self-guarding**: an
already-approved bill comes back `422 {"errors": "Not update other than draft"}`,
which makes the call safe to retry across a batch.

Two sibling routes look right and are not:

| Route | Result |
|---|---|
| `POST /2.0/simple_bills/{id}/approve` | `403` "You have no access to this resource" |
| `POST /2.0/simple_bills/{id}/approved` | `422`, and **does nothing** |

The `422` from `/approved` is the dangerous one: it reads like a payload
problem and invites retrying with different bodies, none of which work.

## Payments and allocations

**Do not pay a bill over this API.** The write half works and the matching half
does not, so the realistic outcome is a payment sitting in the ledger against
nothing. Record payments in the web UI.

What happens if you try:

```json
POST /2.0/receipts
{"receipt": {"date": "...", "amount": 1075.25, "kind": "paid",
             "contact_id": 172, "contact_type": "Vendor",
             "account_id": 120, "description": "...",
             "allocations": [ ... ]}}
```

Returns `200` and creates a real receipt against the cash/petty-cash account —
but **`allocations` is silently dropped**. The receipt lands `status: "unused"`
with `unallocated_amount` equal to the full amount, and the bill stays
unpaid. The money has left the account and is matched to nothing.

Then it gets worse:

- `PUT /2.0/receipts/{id}` with `allocations_attributes` returns `200`, still
  produces `allocations: []`, **and overwrites `description` with the
  reference** while bumping the reference number (`PYT42` → `PYT43`).
- `POST /2.0/bill_payments` → `404` "Invalid bill payment ID". `GET` works;
  it is read-only in practice.
- `/2.0/allocations` and `/2.0/receipts/{id}/allocations` → `404`.

For reading, note that allocations distinguish the two bill kinds:
`allocatee_type` is `"SimpleBill"` for simple bills and `"Bill"` for regular
ones. `GET /2.0/bill_payments` returns payments under a `receipts` key with
`kind: "paid"`.

## Attachments

There is no attachments endpoint in the API — `GET /simple_bills` returns no
attachment data at all. Uploads go through the web app.

On `https://app.qoyod.com/tenant/simple_bills/<id>`:

Two independent traces of this flow disagree, and both were reported working.
`qoyod_attach.py`'s `SEL` dict keeps both as fallback chains rather than
guessing which is current — **this needs one live re-trace to settle.**

| Step | Oct 2026 trace | Sep 2026 trace |
|---|---|---|
| open the modal | `button.comment-attach-btn` | `.fa-paperclip` |
| wait for modal | `._attach-modal` visible | (none) |
| switch to upload tab | `#up-new-tab-link` | `a[href='#upload']` |
| the file input | `#dropFileInput` | `#dropFileInput` |
| confirm | `._attach-modal button.btn-primary` ("متابعة") | `button#attachment-submit` |

The likely explanation is that `.fa-paperclip` is the icon *inside*
`button.comment-attach-btn`, so both click the same control, while the two
submit buttons are genuinely different nodes. Verification differs too, and
both checks now run: the real-attachment count must go up by exactly one **and**
a `cdn.qoyod.com` link must carry the filename.

The form posts to `/tenant/simple_bills/<id>/attachments` and does **not**
touch the bill's data, which is why the view page is safer than the edit page.

Submitting triggers a full page reload — reading `document.body` right after
the click throws `Cannot read properties of null`. Wait for
`domcontentloaded` first.

Verify by looking for an `<a>` whose href is on `cdn.qoyod.com` and whose text
contains the filename. The filename alone can appear without a successful
upload.

## Chrome / CDP

`page.goto(..., wait_until="networkidle")` never settles — the app polls. Use
`domcontentloaded` plus a short fixed wait.

**Chrome 136+ ignores `--remote-debugging-port` when launched on the default
user data directory.** Chrome starts normally, nothing listens on the port, and
the only symptom is a connection refusal. A dedicated profile is required:

```powershell
Stop-Process -Name chrome -Force
& "C:\Program Files\Google\Chrome\Application\chrome.exe" `
  --remote-debugging-port=9222 --user-data-dir="$env:USERPROFILE\chrome-qoyod"
```

That profile needs a one-time Qoyod sign-in, after which it persists.

## PDF text extraction

`pypdf` returns Arabic as shaped presentation forms (U+FB50–U+FEFF) in visual
order, so `شركة` comes out as `ﺔﻛﴍ`. Normalising with NFKC and reversing the
string is *almost* right, but NFKC expands ligatures (`ﴍ` → `شر`) and reversing
the expanded text scrambles them into `رشكة`.

Reverse the original characters and expand each one in place. `prep.py`'s
`unshape_arabic` does this.
