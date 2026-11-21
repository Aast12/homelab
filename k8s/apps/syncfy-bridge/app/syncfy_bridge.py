"""A SimpleFIN server backed by Syncfy, so Sure's SimpleFIN connection reads the
accounts Syncfy reaches (American Express Mexico).

Sure asks for accounts when it syncs: the first visit of the day, or the Sync
button. If Syncfy last ran more than MIN_SYNC_HOURS ago, the bridge starts a
Syncfy sync and waits for it; otherwise it answers with what Syncfy already
holds. Nothing runs on a timer.

    python syncfy_bridge.py serve          # the server
    python syncfy_bridge.py setup-token    # the token to paste into Sure, once
    python syncfy_bridge.py check          # what Sure would receive, without syncing

SimpleFIN protocol: https://www.simplefin.org/protocol.html
Syncfy API: https://github.com/Paybook/sync-py (its README documents the REST calls)
"""

import base64
import hmac
import http.server
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal

SYNCFY_URL = os.environ.get("SYNCFY_URL", "https://sync.paybook.com/v1").rstrip("/")
PUBLIC_URL = os.environ.get("PUBLIC_URL", "http://syncfy-bridge.finance.svc.cluster.local").rstrip("/")
MIN_SYNC_HOURS = float(os.environ.get("MIN_SYNC_HOURS", "24"))
SYNC_WAIT_SECONDS = float(os.environ.get("SYNC_WAIT_SECONDS", "90"))  # Sure gives up after 120
POLL_SECONDS = float(os.environ.get("POLL_SECONDS", "5"))
CACHE_SECONDS = float(os.environ.get("CACHE_SECONDS", "300"))
PAGE_SIZE = 500
# Cloudflare in front of Syncfy refuses Python's default User-Agent (error 1010).
USER_AGENT = "homelab-syncfy-bridge/1.0"

# Syncfy job codes (https://github.com/Paybook/sync-widget/blob/master/widget/codes.md):
# 1xx running, 2xx finished, 4xx needs the user, 5xx the bank or Syncfy failed.
CODE_HELP = {
    401: "the bank rejected the saved password; reconnect the account in Syncfy",
    410: "the bank asked for a one-time code; reconnect the account in Syncfy",
    411: "the bank asked for a one-time code; reconnect the account in Syncfy",
}


def env(name):
    value = os.environ.get(name, "").strip()
    if not value:
        sys.exit(f"{name} is not set (see secrets.env.example)")
    return value


class SyncfyError(RuntimeError):
    pass


class Syncfy:
    """The few Syncfy calls the bridge needs. Reads use the API key; starting a
    sync and following it need a session token, made fresh for each sync."""

    def __init__(self, api_key, user_id, url=SYNCFY_URL):
        self.api_key = api_key
        self.user_id = user_id
        self.url = url

    def _call(self, method, path, query=None, body=None, token=None):
        url = f"{self.url}{path}"
        if query:
            url += "?" + urllib.parse.urlencode(query)
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                payload = json.load(r)
        except urllib.error.HTTPError as e:
            try:
                message = json.load(e).get("message")
            except ValueError:
                message = None
            raise SyncfyError(f"{method} {path}: HTTP {e.code} {message or ''}".strip()) from None
        except (urllib.error.URLError, TimeoutError, ValueError) as e:
            raise SyncfyError(f"{method} {path}: {e}") from None
        return payload.get("response")

    def _read(self, path, **query):
        return self._call("GET", path, {"api_key": self.api_key, "id_user": self.user_id, **query})

    def credentials(self):
        return self._read("/credentials") or []

    def accounts(self):
        return self._read("/accounts") or []

    def transactions(self):
        out, skip = [], 0
        while True:
            page = self._read("/transactions", limit=PAGE_SIZE, skip=skip) or []
            out += page
            if len(page) < PAGE_SIZE:
                return out
            skip += PAGE_SIZE

    def session(self):
        resp = self._call("POST", "/sessions", body={"api_key": self.api_key, "id_user": self.user_id})
        return resp["token"]

    def start_sync(self, credential_id, token):
        resp = self._call("PUT", f"/credentials/{credential_id}/sync", body={}, token=token)
        return resp["id_job"]

    def job_codes(self, job_id, token):
        return [s.get("code") for s in self._call("GET", f"/jobs/{job_id}/status", token=token) or []]


class Bridge:
    def __init__(self, syncfy, now=time.time, sleep=time.sleep):
        self.syncfy = syncfy
        self.now = now
        self.sleep = sleep
        self.sync_lock = threading.Lock()
        self.cache_lock = threading.Lock()
        self.cache = None  # (fetched_at, accounts, transactions)

    def refresh(self):
        """Start a Syncfy sync for each credential whose last run is older than
        MIN_SYNC_HOURS, and wait for it. Returns (what happened, error messages)."""
        errors, events = [], []
        with self.sync_lock:  # Sure's several requests in one sync start one Syncfy sync
            names = self.credential_names()
            for cred in self.syncfy.credentials():
                name = names.get(cred.get("id_credential")) or cred.get("id_credential")
                last_run = cred.get("dt_execute") or 0
                age_hours = (self.now() - last_run) / 3600
                last_code = cred.get("code") or 0
                if 400 <= last_code < 500:
                    # needs the user (wrong password, one-time code); retrying a wrong
                    # password could get the bank account locked
                    events.append(f"{name}: waiting for a reconnect (code {last_code}), not syncing")
                    errors.append(self.describe_failure(name, last_code))
                    continue
                if age_hours < MIN_SYNC_HOURS or not cred.get("can_sync"):
                    events.append(f"{name}: last ran {age_hours:.1f}h ago, not syncing")
                    if last_code >= 500:
                        errors.append(self.describe_failure(name, last_code))
                    continue
                code = self.sync_one(cred["id_credential"])
                events.append(f"{name}: synced, code {code}")
                if code is None:
                    errors.append(f"{name}: Syncfy is still syncing; showing the previous sync")
                elif code >= 300:
                    errors.append(self.describe_failure(name, code))
                with self.cache_lock:
                    self.cache = None
        return events, errors

    def credential_names(self):
        """The bank's name for each credential, from its accounts (credentials carry only ids)."""
        return {a.get("id_credential"): (a.get("site") or {}).get("name") for a in self.snapshot()[0]}

    def sync_one(self, credential_id):
        token = self.syncfy.session()
        job = self.syncfy.start_sync(credential_id, token)
        deadline = self.now() + SYNC_WAIT_SECONDS
        while True:
            codes = self.syncfy.job_codes(job, token)
            if codes and (codes[-1] or 0) >= 200:
                return codes[-1]
            if self.now() >= deadline:
                return None
            self.sleep(POLL_SECONDS)

    @staticmethod
    def describe_failure(name, code):
        return f"{name}: Syncfy sync failed with code {code}" + (
            f" ({CODE_HELP[code]})" if code in CODE_HELP else "")

    def snapshot(self):
        with self.cache_lock:
            if self.cache and self.now() - self.cache[0] < CACHE_SECONDS:
                return self.cache[1], self.cache[2]
            accounts, transactions = self.syncfy.accounts(), self.syncfy.transactions()
            self.cache = (self.now(), accounts, transactions)
            return accounts, transactions


def money(value):
    return f"{Decimal(str(value or 0)):.2f}"


def simplefin_accounts(accounts, transactions, start=None, end=None, pending=False):
    """Syncfy accounts and transactions as a SimpleFIN account set. Both use the
    banking sign: a charge, or a card balance owed, is negative."""
    by_account = {}
    for t in transactions:
        # Syncfy disables a transaction that disappeared at the bank
        if t.get("is_disable") or t.get("is_deleted"):
            continue
        when = t.get("dt_transaction") or 0
        if (start is not None and when < start) or (end is not None and when >= end):
            continue
        if t.get("is_pending") and not pending:
            continue
        extra = t.get("extra") or {}
        tx = {
            "id": t["id_transaction"],
            "posted": when,
            "transacted_at": when,
            "amount": money(t.get("amount")),
            "description": (t.get("description") or "").strip(),
            "pending": bool(t.get("is_pending")),
        }
        info = {k: extra[s] for k, s in (("card", "card_number"), ("card_holder", "card_holder_name"),
                                         ("authorized_at", "dt_authorization")) if extra.get(s)}
        if info:
            tx["extra"] = info
        by_account.setdefault(t.get("id_account"), []).append(tx)

    out = []
    for a in accounts:
        if a.get("is_disable"):
            continue
        extra, site = a.get("extra") or {}, a.get("site") or {}
        number = "".join(ch for ch in str(a.get("number") or "") if ch.isdigit())
        account = {
            "id": a["id_account"],
            "name": a.get("name", "") + (f" {number[-4:]}" if number else ""),
            "currency": a.get("currency") or "MXN",
            "balance": money(a.get("balance")),
            "balance-date": a.get("dt_refresh") or 0,
            "type": (a.get("account_type") or "").strip().lower().replace(" ", "_") or None,
            "org": {"name": site.get("organization") or site.get("name") or "Syncfy",
                    "id": a.get("id_site_organization"), "sfin-url": PUBLIC_URL},
            "transactions": sorted(by_account.get(a["id_account"], []), key=lambda x: x["posted"]),
        }
        if extra.get("available") is not None:
            account["available-balance"] = money(extra["available"])
        out.append(account)
    return out


def setup_token(bridge_token):
    return base64.b64encode(f"{PUBLIC_URL}/claim/{bridge_token}".encode()).decode()


def access_url(bridge_token):
    scheme, rest = PUBLIC_URL.split("://", 1)
    return f"{scheme}://sure:{bridge_token}@{rest}/simplefin"


def make_handler(bridge, bridge_token):
    class Handler(http.server.BaseHTTPRequestHandler):
        def send(self, status, body, content_type="application/json"):
            data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def authorised(self):
            auth = self.headers.get("Authorization", "")
            if not auth.startswith("Basic "):
                return False
            try:
                _, _, password = base64.b64decode(auth[6:]).decode().partition(":")
            except ValueError:
                return False
            return hmac.compare_digest(password, bridge_token)

        def do_POST(self):
            path = urllib.parse.urlsplit(self.path).path
            if path.startswith("/claim/"):
                if hmac.compare_digest(path[len("/claim/"):], bridge_token):
                    return self.send(200, access_url(bridge_token), "text/plain")
                return self.send(403, "unknown setup token", "text/plain")
            self.send(404, "not found", "text/plain")

        def do_GET(self):
            url = urllib.parse.urlsplit(self.path)
            if url.path == "/healthz":
                return self.send(200, "ok", "text/plain")
            if not url.path.startswith("/simplefin/"):
                return self.send(404, "not found", "text/plain")
            if not self.authorised():
                return self.send(403, "forbidden", "text/plain")
            if url.path == "/simplefin/info":
                return self.send(200, {"versions": ["1.0", "2.0"]})
            if url.path != "/simplefin/accounts":
                return self.send(404, "not found", "text/plain")

            q = urllib.parse.parse_qs(url.query)
            num = lambda k: int(q[k][0]) if q.get(k) and q[k][0].lstrip("-").isdigit() else None
            start, end = num("start-date"), num("end-date")
            pending = "pending" in q  # SimpleFIN: present means include
            try:
                events, errors = bridge.refresh()
                accounts, transactions = bridge.snapshot()
            except SyncfyError as e:
                log(f"accounts {start}..{end}: Syncfy unreachable: {e}")
                return self.send(200, {"errors": [f"Syncfy unreachable: {e}"], "accounts": []})
            body = {"errors": errors,
                    "accounts": simplefin_accounts(accounts, transactions, start, end, pending)}
            count = sum(len(a["transactions"]) for a in body["accounts"])
            log(f"accounts {start}..{end} pending={pending}: {'; '.join(events) or 'no credentials'}"
                f" -> {len(body['accounts'])} accounts, {count} transactions, {len(errors)} errors")
            self.send(200, body)

        def log_message(self, fmt, *args):
            pass  # one line per request comes from do_GET instead

    return Handler


def log(message):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), message, flush=True)


def main(argv):
    command = argv[1] if len(argv) > 1 else "serve"
    if command == "setup-token":
        print(setup_token(env("BRIDGE_TOKEN")))
        return
    bridge = Bridge(Syncfy(env("SYNCFY_API_KEY"), env("SYNCFY_USER_ID")))
    if command == "check":
        accounts, transactions = bridge.snapshot()
        names = bridge.credential_names()
        for cred in bridge.syncfy.credentials():
            last = time.strftime("%Y-%m-%d %H:%M", time.localtime(cred.get("dt_execute") or 0))
            print(f"credential {names.get(cred['id_credential'])}: last ran {last}, code {cred.get('code')}")
        for a in simplefin_accounts(accounts, transactions):
            dates = [t["posted"] for t in a["transactions"]]
            span = (" from " + time.strftime("%Y-%m-%d", time.localtime(min(dates))) + " to "
                    + time.strftime("%Y-%m-%d", time.localtime(max(dates)))) if dates else ""
            print(f"account {a['name']!r} ({a['type']}, {a['currency']}): balance {a['balance']}, "
                  f"available {a.get('available-balance')}, {len(dates)} transactions{span}")
        return
    if command != "serve":
        sys.exit(__doc__)
    port = int(os.environ.get("PORT", "8080"))
    token = env("BRIDGE_TOKEN")
    log(f"listening on :{port}; Syncfy syncs at most every {MIN_SYNC_HOURS:g}h")
    http.server.ThreadingHTTPServer(("", port), make_handler(bridge, token)).serve_forever()


if __name__ == "__main__":
    main(sys.argv)
