"""Qoyod API v2.0 client. Stdlib only, no pip install needed.

Company config lives in companies.json next to the run directory (or at
~/.claude/qoyod/companies.json). See SKILL.md for the shape.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = "https://api.qoyod.com/2.0"

# Cloudflare on api.qoyod.com blocks the default python-urllib agent (error 1010).
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"

TAX_15 = 1      # S  15%
TAX_ZERO = 2    # Z  صفرية
TAX_EXEMPT = 3  # E  معفاة
TAX_OUT = 4     # O  غير خاضعة


class Qoyod:
    def __init__(self, api_key):
        self.api_key = api_key
        self._cache = {}

    # ---------- transport ----------

    def request(self, method, path, payload=None, retries=2):
        url = f"{BASE}/{path.lstrip('/')}"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("API-KEY", self.api_key)
        req.add_header("Accept", "application/json")
        req.add_header("User-Agent", UA)
        if data:
            req.add_header("Content-Type", "application/json; charset=utf-8")
        for attempt in range(retries + 1):
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    body = resp.read().decode("utf-8")
                    return resp.status, (json.loads(body) if body else None)
            except urllib.error.HTTPError as e:
                text = e.read().decode("utf-8", "replace")
                # 5xx can mean "it failed" OR "it worked and then blew up" —
                # the caller must re-query before retrying a write.
                if e.code in (429, 502, 503, 504) and attempt < retries:
                    time.sleep(2 * (attempt + 1))
                    continue
                return e.code, text
            except (urllib.error.URLError, TimeoutError) as e:
                if attempt < retries:
                    time.sleep(2 * (attempt + 1))
                    continue
                return 0, str(e)

    def get_all(self, resource, key=None, use_cache=True):
        """Every page of a list endpoint, cached per process."""
        if use_cache and resource in self._cache:
            return self._cache[resource]
        key = key or resource
        out, seen, page = [], set(), 1
        while page <= 200:
            status, body = self.request("GET", f"{resource}?page={page}")
            if status != 200:
                raise RuntimeError(f"GET {resource} page {page} -> {status} {body}")
            items = body.get(key, [])
            # Some endpoints (/accounts) ignore ?page and return everything
            # every time, so a plain "full page means keep going" loop never
            # ends. Stop as soon as a page brings nothing new.
            fresh = [it for it in items if it.get("id") not in seen]
            if not fresh:
                break
            seen.update(it.get("id") for it in fresh)
            out.extend(fresh)
            if len(items) < 100:
                break
            page += 1
        self._cache[resource] = out
        return out

    def invalidate(self, resource):
        self._cache.pop(resource, None)

    # ---------- lookups ----------

    def account_by_code(self, code):
        for a in self.get_all("accounts"):
            if str(a.get("code")) == str(code):
                return a
        return None

    def inventories(self):
        return self.get_all("inventories")

    def vendors_by_vat(self, vat):
        return sorted(
            (v for v in self.get_all("vendors") if (v.get("tax_number") or "") == vat),
            key=lambda v: v["id"],
        )

    def vendors_by_name(self, *fragments):
        hits = []
        for v in self.get_all("vendors"):
            name = (v.get("name") or "").lower()
            if any(f and f.lower() in name for f in fragments):
                hits.append(v)
        return sorted(hits, key=lambda v: v["id"])

    def bill_by_reference(self, reference):
        return [b for b in self.get_all("simple_bills") if b.get("reference") == reference]

    # ---------- writes ----------

    def create_vendor(self, name_ar, tax_number):
        """Create a vendor with name + VAT only.

        POST /vendors wants a `contact` wrapper with type Vendor. A `vendor`
        wrapper returns 500 *but still creates the record*, which is how you
        end up with silent duplicates — so on any error we re-query by VAT
        instead of blindly retrying.
        """
        existing = self.vendors_by_vat(tax_number)
        if existing:
            return existing[0], "existing"

        status, body = self.request("POST", "vendors", {
            "contact": {"name": name_ar, "tax_number": tax_number, "type": "Vendor"}
        })
        self.invalidate("vendors")

        if status in (200, 201) and isinstance(body, dict):
            return body.get("contact") or body.get("vendor"), "created"

        # The write may have landed anyway. Look before leaping.
        again = self.vendors_by_vat(tax_number)
        if again:
            return again[0], "created"
        raise RuntimeError(f"create_vendor failed: {status} {body}")

    def create_draft_bill(self, contact_id, reference, issue_date, description,
                          line_items, inventory_id):
        """Create a Simple Bill as a DRAFT.

        Payload quirks that cost real time to discover:
          - lines go in `simple_bill_items_attributes`, not `line_items`
          - `inventory_id` belongs on the bill, not on each line
          - each line needs `expense_category_id` (not `expense_account_id`)
        """
        dupe = self.bill_by_reference(reference)
        if dupe:
            return dupe[0], "existing"

        status, body = self.request("POST", "simple_bills", {
            "simple_bill": {
                "contact_id": contact_id,
                "inventory_id": inventory_id,
                "reference": reference,
                "description": description,
                "issue_date": issue_date,
                "status": "Draft",
                "simple_bill_items_attributes": line_items,
            }
        })
        self.invalidate("simple_bills")

        if status in (200, 201) and isinstance(body, dict):
            return body["simple_bill"], "created"

        again = self.bill_by_reference(reference)
        if again:
            return again[0], "created"
        raise RuntimeError(f"create_draft_bill failed: {status} {body}")

    def get_bill(self, bill_id):
        status, body = self.request("GET", f"simple_bills/{bill_id}")
        if status != 200:
            raise RuntimeError(f"GET simple_bills/{bill_id} -> {status} {body}")
        return body["simple_bill"]


# ---------- company config ----------

def config_paths(run_dir=None):
    paths = []
    if run_dir:
        paths.append(os.path.join(run_dir, "companies.json"))
    paths.append(os.path.expanduser("~/.claude/qoyod/companies.json"))
    return paths


def load_company(slug, run_dir=None):
    for path in config_paths(run_dir):
        if os.path.exists(path):
            # utf-8-sig: anything written by PowerShell carries a BOM.
            with open(path, encoding="utf-8-sig") as f:
                data = json.load(f)
            companies = data.get("companies", data)
            if slug in companies:
                c = dict(companies[slug])
                c.setdefault("slug", slug)
                c["_config_path"] = path
                return c
            raise SystemExit(
                f"company '{slug}' not in {path}. Known: {', '.join(sorted(companies))}"
            )
    raise SystemExit(
        "no companies.json found. Looked in:\n  " + "\n  ".join(config_paths(run_dir))
    )


def list_companies(run_dir=None):
    for path in config_paths(run_dir):
        if os.path.exists(path):
            with open(path, encoding="utf-8-sig") as f:
                data = json.load(f)
            return path, (data.get("companies", data))
    return None, {}


def client_for(company):
    key = company.get("api_key") or os.environ.get(company.get("api_key_env", ""), "")
    if not key:
        raise SystemExit(
            f"no API key for {company.get('slug')}: set api_key in companies.json "
            "or api_key_env pointing at an environment variable"
        )
    return Qoyod(key)


def utf8_stdout():
    sys.stdout.reconfigure(encoding="utf-8")


def resolve_run_dir(path):
    """The run directory is the one that CONTAINS inbox/.

    Users name the inbox when they ask ("the invoices in .../inbox"), so --dir
    frequently arrives one level too deep. Left alone that silently produces an
    empty batch, which looks like "no invoices found" rather than a mistake.
    """
    p = os.path.abspath(path.rstrip("\\/"))
    if os.path.basename(p).lower() == "inbox" and os.path.isdir(p):
        return os.path.dirname(p)
    return p
