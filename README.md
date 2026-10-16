# qoyod-batch-bills

Record supplier purchase invoices in Qoyod as draft **Simple Bills** over the
API, then attach each invoice PDF through the user's logged-in Chrome. Built for
volume — tens or hundreds of invoices in one run, across several client
companies, one company per run.

## Install

**Requires `git`** on the machine doing the install — adding a marketplace
clones this repository. Without it the install stops at
`Failed to clone marketplace repository: Command 'git' not found`. On Windows:

```
winget install --id Git.Git -e
```

Then, in a **terminal** session (this command is not available in the Claude
desktop app's Code tab):

```
/plugin install qoyod-batch-bills --marketplace SoCalledBlackBurn/Qoyod-SB-P-A
```

Then `y` to add the marketplace, and Enter to accept the `user` scope.

## Updating

```
claude plugin update qoyod-batch-bills
```

Then `/reload-plugins` in the session. Updates reach installs once you bump
`version` in `.claude-plugin/plugin.json` and push the change.

## Setup

### 1. Companies

**This plugin contains no API keys.** Each person creates their own registry.
Copy `skills/qoyod-batch-bills/scripts/companies.example.json` to:

```
~/.claude/qoyod/companies.json
```

Every company needs `name`, `vat`, `expense_account`, and a key. Prefer
`api_key_env`, which names an environment variable holding the key, over
writing the key into the file.

Note that `name` must be the company's legal name exactly as Qoyod shows it,
which in practice means Arabic. It is matched against the customer name printed
on each invoice, so a translated or tidied-up version will cause invoices to be
blocked.

### 2. Requirements

```
py -3 -m pip install pypdf playwright
```

`playwright install` is **not** needed — attaching drives the user's own Chrome,
so there is no browser to download.

On Windows use `py -3`; `python` is often a Microsoft Store stub that exits
without running anything.

### 3. Chrome (for the attachment step only)

Needs Chrome listening on port 9222 and signed in to Qoyod. **Chrome 136+
ignores `--remote-debugging-port` on the default profile** and silently opens a
normal browser with nothing listening, so a dedicated profile is required:

```powershell
Stop-Process -Name chrome -Force
& "C:\Program Files\Google\Chrome\Application\chrome.exe" `
  --remote-debugging-port=9222 --remote-allow-origins=* `
  --user-data-dir="$env:USERPROFILE\chrome-qoyod"
```

That profile needs a one-time Qoyod sign-in, after which it persists.

## Usage

```
/qoyod-batch-bills record the invoices for <company> in <folder path>
```

Or just describe the job in your own words — in English or Arabic — and the
skill triggers on its own.

The full step-by-step is in [SKILL.md](skills/qoyod-batch-bills/SKILL.md).

## Non-negotiables

The skill is bound by these. They are written into it, not just documented
here:

- **Drafts only** — it does not approve a bill or create a payment on its own.
- **Never invent a value** — an unreadable invoice is blocked, not estimated.
- **Never edit or delete** an existing supplier or bill.
- **Never record the same invoice twice** — `log.csv` and Qoyod are both checked
  before every write.

## A note on Arabic

The documentation is English, but some Arabic here is deliberate and must not
be translated.

**The batch report is written in Arabic** (`report.py`). It is read by the
accountant reviewing the invoices, not by this repository's readers, so its
headings stay Arabic even though everything around them is English.

The rest is **functional** — translating it breaks the skill:

- The Arabic trigger phrases in the skill's `description` are what make it fire
  on Arabic requests. Removing them breaks that.
- Document titles such as `فاتورة ضريبية` are matched against the text printed
  on real invoices to decide VAT treatment.
- Words stripped from supplier names for matching (`شركة`, `مؤسسة`) and the
  Qoyod tax labels are data, not prose.
- The Arabic strings in `prep.py`'s comments *demonstrate* a text-extraction bug
  and only make sense in Arabic.

## API notes

[`references/api-notes.md`](skills/qoyod-batch-bills/references/api-notes.md)
covers the payload shapes, tax ids, the Cloudflare user-agent requirement, the
paging quirks, and the approve route — including an explicit warning that
**paying a bill over the API does not work** and leaves money recorded against
nothing.

## Security

- No keys in this repository. Keys live in the local `companies.json` or in
  environment variables, and never appear in reports, logs, or queues.
- `.gitignore` excludes `companies.json`, but check before committing anyway.
- The company names and VAT numbers in `companies.example.json` are invented
  examples, not real client data.
