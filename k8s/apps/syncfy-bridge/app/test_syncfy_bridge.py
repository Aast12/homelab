"""Tests for syncfy_bridge.py against a fake Syncfy. Run: python -m unittest -v"""

import base64
import http.server
import json
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request

import syncfy_bridge as sb

TOKEN = "bridge-secret"
HOUR = 3600


class FakeSyncfy:
    """Just enough of Syncfy's REST API: reads, sessions, sync, job status."""

    def __init__(self):
        now = int(time.time())
        self.fail_reads = False
        self.job_codes = [100, 102, 200]  # what a sync reports, one more code per poll
        self.puts = 0
        self.credentials = [{"id_credential": "cred1", "code": 200, "can_sync": 1,
                             "dt_execute": now - 25 * HOUR}]
        self.accounts = [{
            "id_account": "acc1", "id_credential": "cred1", "id_site_organization": "org1",
            "name": "Gold Elite Card", "number": "XXXX-XXXXXX-X1003", "balance": -36129.85,
            "currency": "MXN", "account_type": "Credit Card", "is_disable": 0,
            "dt_refresh": now - HOUR,
            "site": {"name": "American Express", "organization": "American Express"},
            "extra": {"available": 63870, "limit": 100000, "pending_balance": 0.0},
        }]
        day = lambda d: int(time.mktime((2026, 11, d, 7, 0, 0, 0, 0, -1)))
        self.transactions = [
            self.tx("t1", day(12), -521.97, "OXXO", auth=day(12)),
            self.tx("t2", day(15), 20.0, "AJUSTE"),
            self.tx("t3", day(15), -1589.0, "LIVERPOOL", is_disable=1),  # gone at the bank
            self.tx("t4", day(16), -99.0, "BORRADA", is_deleted=1),
            self.tx("t5", day(18), -250.5, "UBER EATS", is_pending=1),
            self.tx("t6", day(19), -1200.0, "CFE"),
        ]
        self.jobs = {}

    @staticmethod
    def tx(tid, when, amount, desc, auth=None, **flags):
        extra = {"card_number": "XXXX-XXXXXX-X1003", "card_holder_name": "TITULAR"}
        if auth:
            extra["dt_authorization"] = auth
        return {"id_transaction": tid, "id_account": "acc1", "dt_transaction": when, "amount": amount,
                "description": desc + "   ", "currency": "MXN", "extra": extra,
                "is_disable": 0, "is_deleted": 0, "is_pending": 0, **flags}

    def serve(self):
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def reply(self, status, response):
                body = json.dumps({"code": status, "response": response}).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                url = urllib.parse.urlsplit(self.path)
                q = dict(urllib.parse.parse_qsl(url.query))
                if url.path.startswith("/v1/jobs/"):
                    job = url.path.split("/")[3]
                    fake.jobs[job] += 1
                    return self.reply(200, [{"code": c} for c in fake.job_codes[:fake.jobs[job]]])
                if fake.fail_reads:
                    return self.reply(500, None)
                assert q.get("api_key") == "key" and q.get("id_user") == "user", q
                if url.path == "/v1/transactions":
                    skip, limit = int(q.get("skip", 0)), int(q.get("limit", 1000))
                    return self.reply(200, fake.transactions[skip:skip + limit])
                self.reply(200, {"/v1/credentials": fake.credentials,
                                 "/v1/accounts": fake.accounts}[url.path])

            def do_POST(self):
                self.reply(200, {"token": "session"})

            def do_PUT(self):
                assert self.headers["Authorization"] == "Bearer session"
                fake.puts += 1
                fake.credentials[0].update(dt_execute=int(time.time()), can_sync=0)
                job = f"job{fake.puts}"
                fake.jobs[job] = 0
                self.reply(200, {"id_job": job})

            def log_message(self, *a):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{self.server.server_port}/v1"


class BridgeTest(unittest.TestCase):
    def setUp(self):
        sb.POLL_SECONDS, sb.SYNC_WAIT_SECONDS, sb.MIN_SYNC_HOURS = 0.01, 2, 24
        self.fake = FakeSyncfy()
        self.bridge = sb.Bridge(sb.Syncfy("key", "user", self.fake.serve()))
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), sb.make_handler(self.bridge, TOKEN))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        sb.PUBLIC_URL = self.url

    def tearDown(self):
        for server in (self.server, self.fake.server):
            server.shutdown()
            server.server_close()

    def get(self, path, password=TOKEN):
        req = urllib.request.Request(self.url + path)
        if password is not None:
            req.add_header("Authorization", "Basic " + base64.b64encode(f"sure:{password}".encode()).decode())
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            return e.code, None

    def accounts(self, query=""):
        status, body = self.get("/simplefin/accounts" + query)
        self.assertEqual(status, 200)
        return body

    # setup and access

    def test_setup_token_claims_the_access_url(self):
        claim_url = base64.b64decode(sb.setup_token(TOKEN)).decode()
        self.assertEqual(claim_url, f"{self.url}/claim/{TOKEN}")
        with urllib.request.urlopen(urllib.request.Request(claim_url, method="POST")) as r:
            access = r.read().decode()
        self.assertEqual(access, f"http://sure:{TOKEN}@127.0.0.1:{self.server.server_port}/simplefin")

    def test_wrong_setup_token_is_refused(self):
        req = urllib.request.Request(f"{self.url}/claim/guess", method="POST")
        with self.assertRaises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req)
        self.assertEqual(e.exception.code, 403)

    def test_accounts_need_the_token(self):
        self.assertEqual(self.get("/simplefin/accounts", password=None)[0], 403)
        self.assertEqual(self.get("/simplefin/accounts", password="guess")[0], 403)
        self.assertEqual(self.fake.puts, 0)

    # what Sure receives

    def test_card_maps_to_a_simplefin_account(self):
        body = self.accounts("?version=2")
        self.assertEqual(body["errors"], [])
        [acct] = body["accounts"]
        self.assertEqual(acct["name"], "Gold Elite Card 1003")
        self.assertEqual(acct["type"], "credit_card")
        self.assertEqual(acct["balance"], "-36129.85")       # owed: negative, as SimpleFIN expects
        self.assertEqual(acct["available-balance"], "63870.00")
        self.assertEqual(acct["org"]["name"], "American Express")
        txs = {t["id"]: t for t in acct["transactions"]}
        self.assertEqual(list(txs), ["t1", "t2", "t6"])       # disabled, deleted and pending left out
        self.assertEqual(txs["t1"]["amount"], "-521.97")      # a charge stays negative
        self.assertEqual(txs["t2"]["amount"], "20.00")
        self.assertEqual(txs["t1"]["description"], "OXXO")
        self.assertEqual(txs["t1"]["extra"]["card"], "XXXX-XXXXXX-X1003")
        self.assertIn("authorized_at", txs["t1"]["extra"])
        self.assertEqual(txs["t1"]["posted"], txs["t1"]["transacted_at"])

    def test_date_window_and_pending(self):
        nov = lambda d: int(time.mktime((2026, 11, d, 0, 0, 0, 0, 0, -1)))
        ids = lambda b: [t["id"] for t in b["accounts"][0]["transactions"]]
        self.assertEqual(ids(self.accounts(f"?start-date={nov(15)}&end-date={nov(19)}")), ["t2"])
        self.assertEqual(ids(self.accounts(f"?start-date={nov(15)}&pending=1")), ["t2", "t5", "t6"])

    def test_transactions_are_read_in_pages(self):
        sb.PAGE_SIZE = 2
        try:
            self.assertEqual(len(self.accounts()["accounts"][0]["transactions"]), 3)
        finally:
            sb.PAGE_SIZE = 500

    # when Syncfy syncs

    def test_recent_sync_is_not_repeated(self):
        self.fake.credentials[0]["dt_execute"] = int(time.time()) - 2 * HOUR
        self.assertEqual(len(self.accounts()["accounts"]), 1)
        self.assertEqual(self.fake.puts, 0)

    def test_old_sync_runs_once_for_requests_arriving_together(self):
        results = []
        threads = [threading.Thread(target=lambda: results.append(self.accounts())) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(self.fake.puts, 1)
        self.assertEqual([r["errors"] for r in results], [[], [], []])
        self.assertEqual(self.accounts()["errors"], [])  # and Sure's next request doesn't sync again
        self.assertEqual(self.fake.puts, 1)

    def test_rejected_password_is_reported_with_the_data_already_held(self):
        self.fake.job_codes = [100, 401]
        body = self.accounts()
        self.assertEqual(self.fake.puts, 1)
        self.assertEqual(len(body["accounts"]), 1)
        self.assertEqual(len(body["errors"]), 1)
        self.assertIn("rejected the saved password", body["errors"][0])
        self.fake.credentials[0]["code"] = 401   # later requests keep showing it, without syncing
        self.assertIn("code 401", self.accounts()["errors"][0])
        self.assertEqual(self.fake.puts, 1)

    def test_rejected_password_is_not_retried_a_day_later(self):
        # a wrong password tried again and again can get the bank account locked
        self.fake.credentials[0].update(code=401, can_sync=1, dt_execute=int(time.time()) - 48 * HOUR)
        body = self.accounts()
        self.assertEqual(self.fake.puts, 0)
        self.assertIn("rejected the saved password", body["errors"][0])
        self.assertEqual(len(body["accounts"]), 1)

    def test_slow_sync_answers_with_the_previous_data(self):
        self.fake.job_codes = [100]
        sb.SYNC_WAIT_SECONDS = 0.1
        body = self.accounts()
        self.assertEqual(len(body["accounts"]), 1)
        self.assertIn("still syncing", body["errors"][0])

    def test_syncfy_down_is_an_error_not_a_crash(self):
        self.fake.fail_reads = True
        body = self.accounts()
        self.assertEqual(body["accounts"], [])
        self.assertIn("Syncfy unreachable", body["errors"][0])


if __name__ == "__main__":
    unittest.main()
