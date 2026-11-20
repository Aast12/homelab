"""Import Nubank México statements (PDF) into Sure.

Two statement kinds are recognised:
  - the debit account ("Cuenta Nu"): every movement except moves between the
    account and its cajitas, plus the month's interest; balance = cuenta +
    cajitas + crédito con garantía.
  - the credit card ("Tarjeta de Crédito Nu"): every regular charge and payment,
    MSI as one charge per instalment; balance = regular balance + pending MSI.

Each statement is checked against its own summary before anything is written,
and every transaction carries a stable external id, so importing the same PDF
again changes nothing.

  python nubank_import.py parse statement.pdf     # print what would be imported
  python nubank_import.py serve                   # HTTP upload service
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import email.parser
import email.policy
import hashlib
import hmac
import io
import json
import os
import re
import sys
import urllib.error
import urllib.request
import uuid
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MONTHS = {m: i + 1 for i, m in enumerate(
    "ENE FEB MAR ABR MAY JUN JUL AGO SEP OCT NOV DIC".split())}
DATE = r"(\d{2}) ([A-Z]{3}) (\d{4})"
AMOUNT = r"([+-])\$([\d,]+\.\d{2})"
SOURCE = "nubank"


class StatementError(Exception):
    """The PDF is not a statement we know, or it does not add up."""


class SureError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


@dataclasses.dataclass
class Txn:
    date: dt.date
    name: str
    amount: Decimal          # positive = money out (expense), negative = money in
    notes: str = ""
    external_id: str = ""
    charged: dt.date | None = None   # credit card: the date it was charged to the card


@dataclasses.dataclass
class Statement:
    kind: str                # "debit" or "credit"
    account_ref: str         # account number or card last 4
    period_start: dt.date
    period_end: dt.date
    balance: Decimal         # debit: money held; credit: amount owed
    transactions: list[Txn]
    checks: dict[str, str]


def money(s: str) -> Decimal:
    return Decimal(s.replace(",", "").replace("$", "").strip())


def date_of(d: str, mon: str, y: str) -> dt.date:
    return dt.date(int(y), MONTHS[mon.upper()], int(d))


def pdf_text(data: bytes) -> str:
    import pypdf  # installed in the container; imported late so tests run without it

    reader = pypdf.PdfReader(io.BytesIO(data))
    return "\n".join(p.extract_text(extraction_mode="layout") for p in reader.pages)


WRAPPED_YEARS = re.compile(r"^(?P<line>.*)\n[ \t]*(?P<years>\d{4}(?:[ \t]+\d{4})?)[ \t]*$", re.M)
DATE_NO_YEAR = re.compile(r"\b(\d{2} [A-Z]{3})\b(?! \d{4})")


def unwrap_years(text: str) -> str:
    """Put years that the PDF wrapped onto the next line back after their dates.

    Some statements print "07 AGO" on one line and "2026" alone below it.
    """
    def fix(m: re.Match) -> str:
        years = m.group("years").split()
        line = m.group("line")
        if len(DATE_NO_YEAR.findall(line)) < len(years):
            return m.group(0)
        for y in years:
            line = DATE_NO_YEAR.sub(lambda d: f"{d.group(1)} {y}", line, count=1)
        return line
    return WRAPPED_YEARS.sub(fix, text)


def parse(text: str) -> Statement:
    text = unwrap_years(text)
    if "Tarjeta de Crédito Nu" in text:
        st = parse_credit(text)
    elif "Cuenta Nu:" in text and "Detalle de movimientos en tu cuenta" in text:
        st = parse_debit(text)
    else:
        raise StatementError("not a Nubank debit or credit card statement")
    assign_ids(st)
    return st


def assign_ids(st: Statement) -> None:
    # Same date, name and amount can repeat in one statement (two identical
    # coffees); the occurrence number keeps their ids apart and stable.
    seen: dict[tuple, int] = {}
    for t in st.transactions:
        key = (t.date, t.name, t.amount)
        seen[key] = seen.get(key, 0) + 1
        raw = f"{st.kind}|{st.account_ref}|{t.date}|{t.name}|{t.amount}|{seen[key]}"
        t.external_id = f"nu-{st.kind}-{hashlib.sha256(raw.encode()).hexdigest()[:24]}"


def summary_amount(text: str, label: str) -> Decimal:
    m = re.search(re.escape(label) + r"\s+([+-]?)\$([\d,]+\.\d{2})", text)
    if not m:
        raise StatementError(f"summary line not found: {label}")
    return money(m.group(2)) * (-1 if m.group(1) == "-" else 1)


# ------------------------------------------------------------------ debit

CAJITA_MOVE = re.compile(r"^(Depósito en Cajita|Retiro de Cajita):")


def parse_debit(text: str) -> Statement:
    acct = re.search(r"Cuenta Nu:\s*(\d+)", text).group(1)
    pm = re.search(r"Periodo: del (\d{2})(?: ([a-z]{3}))? al (\d{2}) ([a-z]{3}) (\d{4})", text)
    if not pm:
        raise StatementError("debit statement: period not found")
    d1, m1, d2, m2, y = pm.groups()
    end = date_of(d2, m2, y)
    start = date_of(d1, m1 or m2, y)
    if start > end:                       # "del 15 dic al 14 ene 2027"
        start = start.replace(year=start.year - 1)

    body = text.split("Detalle de movimientos en tu cuenta", 1)[1]
    body = body.split("Con estos movimientos", 1)[0]

    line = re.compile(rf"^\s*{DATE}\s{{2,}}(.+?)\s{{2,}}{AMOUNT}\s*$")
    txns: list[Txn] = []
    current: Txn | None = None
    skipped_cajita = 0
    for raw in body.splitlines():
        m = line.match(raw)
        if m:
            d, mon, y, name, sign, amt = m.groups()
            name = " ".join(name.split())
            if CAJITA_MOVE.match(name):
                skipped_cajita += 1
                current = None
                continue
            value = money(amt)
            current = Txn(date_of(d, mon, y), name, value if sign == "-" else -value)
            txns.append(current)
            continue
        detail = " ".join(raw.split())
        if not detail:
            continue
        if detail.startswith(("Nubank, S.A.", "FECHA")):
            current = None        # page footer or table header: details never cross a page
            continue
        if current:               # SPEI details, "Comisión … $34.80" (already in the amount)
            current.notes = f"{current.notes} {detail}".strip()

    deposits = summary_amount(text, "Depósitos")
    spending = summary_amount(text, "Gastos")
    fees = summary_amount(text, "Comisiones cobradas por Nu")
    interest = summary_amount(text, "Dinero generado este mes")
    opening = summary_amount(text, "Saldo inicial")
    closing = summary_amount(text, "Saldo al generar este estado de cuenta")

    got_in = -sum((t.amount for t in txns if t.amount < 0), Decimal(0))
    got_out = sum((t.amount for t in txns if t.amount > 0), Decimal(0))
    if got_in != deposits or got_out != -spending:
        raise StatementError(
            f"debit statement does not add up: movements in {got_in} vs Depósitos {deposits}, "
            f"out {got_out} vs Gastos {-spending}")
    if opening + deposits + spending - fees + interest != closing:
        raise StatementError("debit statement summary does not reconcile to its closing balance")

    if interest:
        txns.append(Txn(end, "Rendimientos Cuenta Nu", -interest,
                        "Dinero generado este mes, después de impuestos"))
    if fees:
        txns.append(Txn(end, "Comisiones cobradas por Nu", fees))

    return Statement("debit", acct, start, end, closing, txns, {
        "movements in = Depósitos": str(deposits),
        "movements out = Gastos": str(-spending),
        "opening + in - out - fees + interest = closing": str(closing),
        "cajita moves skipped": str(skipped_cajita),
    })


# ------------------------------------------------------------------ credit

def parse_credit(text: str) -> Statement:
    card = re.search(r"Número de tarjeta:\s*X{4}-X{4}-X{4}-(\d{4})", text).group(1)
    cut = re.search(rf"Fecha de corte:\s+{DATE}", text)
    start = re.search(rf"Periodo:\s+{DATE} al", text) or re.search(rf"{DATE} al \d{{2}} [A-Z]{{3}}", text)
    if not cut or not start:
        raise StatementError("credit statement: period or cut-off date not found")
    end = date_of(*cut.groups())
    begin = date_of(*start.groups())

    line = re.compile(rf"^\s*{DATE}\s+{DATE}\s+(.+?)\s{{2,}}{AMOUNT}\s*$")
    txns: list[Txn] = []
    current: Txn | None = None
    holder = ""
    # When a balance is not paid in full, Nubank moves it on the due date with a
    # "Saldo revolvente" charge (left out of Total de cargos) and sometimes a
    # matching "Crédito de saldo revolvente" credit (counted in Total de abonos).
    # Both are the debt already owed, not spending or payments: skipped.
    revolving_credits = Decimal(0)
    in_section = False
    for raw in text.splitlines():
        s = " ".join(raw.split())
        if "CARGOS, ABONOS Y COMPRAS REGULARES (NO A MESES)" in s:
            in_section = True
            current = None
            continue
        if not in_section:
            continue
        if s.startswith("Total de cargos"):
            break
        if s.startswith(("Tarjeta titular:", "Tarjeta adicional:")):
            holder = s if s.startswith("Tarjeta adicional:") else ""
            continue
        m = line.match(raw)
        if m:
            od, om, oy, cd, cm, cy, name, sign, amt = m.groups()
            op_date, charge_date = date_of(od, om, oy), date_of(cd, cm, cy)
            # MSI and payment plans carry the original purchase date; book them
            # on the date they were charged in this period.
            when = op_date if op_date >= begin else charge_date
            name = re.sub(r"\s*\|\s*RFC:.*$", "", " ".join(name.split()))
            value = money(amt)
            if name in ("Saldo revolvente", "Crédito de saldo revolvente"):
                if sign == "-":
                    revolving_credits -= value
                current = None
                continue
            current = Txn(when, name, value if sign == "+" else -value, holder, charged=charge_date)
            txns.append(current)
            continue
        if re.match(rf"^\s*{DATE}\s+{DATE}\s+", raw):
            current = None        # informational line without a signed amount
            continue
        if current and s.startswith(("Tarjeta virtual", "Tarjeta física", "Abono", "Tarjeta digital")):
            current.notes = f"{current.notes} | {s}".strip(" |")

    charges = summary_amount(text, "Total de cargos")
    credits = summary_amount(text, "Total de abonos")
    # "Total de cargos" leaves out charges dated after the cut-off (the IVA on a
    # payment plan, charged the next day) although the table lists them; they
    # are still real charges and are imported.
    got_charges = sum((t.amount for t in txns if t.amount > 0 and t.charged <= end), Decimal(0))
    got_credits = sum((t.amount for t in txns if t.amount < 0), Decimal(0))
    if got_charges != charges or got_credits + revolving_credits != credits:
        raise StatementError(
            f"credit statement does not add up: charges {got_charges} vs Total de cargos {charges}, "
            f"credits {got_credits} vs Total de abonos {credits}")

    regular = summary_amount(text, "Saldo cargos regulares")
    msi = summary_amount(text, "Saldo cargos a meses")
    return Statement("credit", card, begin, end, regular + msi, txns, {
        "charges = Total de cargos": str(charges),
        "credits = Total de abonos": str(credits),
        "revolving-balance credit skipped": str(revolving_credits),
        "balance = regular + pending MSI": f"{regular} + {msi}",
    })


# ------------------------------------------------------------------ Sure

class Sure:
    def __init__(self, url: str, key: str):
        self.url, self.key = url.rstrip("/"), key

    def call(self, method: str, path: str, body: dict | None = None,
             raw: bytes | None = None, ctype: str = "application/json") -> tuple[int, dict]:
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(self.url + path, data=data, method=method, headers={
            "X-Api-Key": self.key, "Accept": "application/json", "Content-Type": ctype})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:500]
            raise SureError(e.code, f"Sure {method} {path}: HTTP {e.code} {detail}") from None

    def account_id(self, name: str) -> str:
        _, res = self.call("GET", "/api/v1/accounts?per_page=100")
        for a in res.get("accounts", res.get("data", [])):
            if a.get("name") == name:
                return a["id"]
        raise RuntimeError(f"no Sure account named {name!r}; create it in Sure first")

    def upload_statement(self, account: str, filename: str, pdf: bytes) -> tuple[int, dict]:
        boundary = uuid.uuid4().hex
        parts = [
            f'--{boundary}\r\nContent-Disposition: form-data; name="account_id"\r\n\r\n{account}\r\n'.encode(),
            (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
             "Content-Type: application/pdf\r\n\r\n").encode() + pdf + b"\r\n",
            f"--{boundary}--\r\n".encode(),
        ]
        return self.call("POST", "/api/v1/account_statements", raw=b"".join(parts),
                         ctype=f"multipart/form-data; boundary={boundary}")


def import_statement(st: Statement, pdf: bytes, filename: str, sure: Sure,
                     names: dict[str, str]) -> dict:
    account = sure.account_id(names[st.kind])
    created = existing = 0
    for t in st.transactions:
        status, _ = sure.call("POST", "/api/v1/transactions", {"transaction": {
            "account_id": account,
            "date": t.date.isoformat(),
            "amount": str(abs(t.amount)),
            "nature": "expense" if t.amount > 0 else "income",
            "name": t.name,
            "notes": t.notes or None,
            "currency": "MXN",
            "external_id": t.external_id,
            "source": SOURCE,
        }})
        if status == 201:
            created += 1
        else:
            existing += 1
    sure.call("POST", "/api/v1/valuations", {"upsert": True, "valuation": {
        "account_id": account, "date": st.period_end.isoformat(), "amount": str(st.balance),
        "notes": f"Saldo según estado de cuenta Nu {st.period_start} a {st.period_end}"}})
    # The statement vault API exists only in newer Sure (not v0.7.6); without
    # it the transactions and balance are still in, so report and carry on.
    try:
        _, res = sure.upload_statement(account, filename, pdf)
        filed = "already in Sure" if (res.get("meta") or {}).get("duplicate") else "yes"
    except SureError as e:
        if e.status != 404:
            raise
        filed = "no (this Sure version has no statement vault)"
    return {
        "account": names[st.kind],
        "period": f"{st.period_start} to {st.period_end}",
        "transactions_created": created,
        "transactions_already_there": existing,
        "balance": str(st.balance),
        "statement_filed": filed,
    }


# ------------------------------------------------------------------ HTTP

PAGE = """<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Nubank a Sure</title>
<style>body{font-family:system-ui,sans-serif;max-width:520px;margin:0 auto;padding:16px;background:#fff;color:#111}
input,button{font-size:16px;width:100%;margin:8px 0;padding:10px;box-sizing:border-box}
pre{white-space:pre-wrap;background:#f3f3f3;padding:12px;border-radius:8px}
@media (prefers-color-scheme: dark){body{background:#111;color:#eee}pre{background:#222}}</style></head>
<body><h1>Estado de cuenta Nu → Sure</h1>
<form id="f"><input type="password" id="t" placeholder="Token" autocomplete="current-password">
<input type="file" id="p" accept="application/pdf"><button>Importar</button></form><pre id="out"></pre>
<script>
const t = document.getElementById('t');
try { t.value = localStorage.getItem('token') || ''; } catch (e) {}
document.getElementById('f').onsubmit = async (e) => {
  e.preventDefault();
  const out = document.getElementById('out'), file = document.getElementById('p').files[0];
  if (!file) { out.textContent = 'Elige un PDF.'; return; }
  try { localStorage.setItem('token', t.value); } catch (e) {}
  out.textContent = 'Importando…';
  const r = await fetch('import', {method: 'POST', body: file,
    headers: {'X-Token': t.value, 'Content-Type': 'application/pdf', 'X-Filename': encodeURIComponent(file.name)}});
  out.textContent = await r.text();
};
</script></body></html>"""


def pdf_from_request(ctype: str, body: bytes) -> tuple[bytes, str]:
    if ctype.startswith("multipart/form-data"):
        msg = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
            f"Content-Type: {ctype}\r\n\r\n".encode() + body)
        for part in msg.iter_parts():
            data = part.get_payload(decode=True) or b""
            if data.startswith(b"%PDF"):
                return data, part.get_filename() or "estado-de-cuenta.pdf"
        raise StatementError("no PDF in the upload")
    if not body.startswith(b"%PDF"):
        raise StatementError("the upload is not a PDF")
    return body, "estado-de-cuenta.pdf"


class Handler(BaseHTTPRequestHandler):
    server_version = "nubank-import"

    def send(self, status: int, body: str, ctype: str = "text/plain; charset=utf-8") -> None:
        data = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if self.path == "/healthz":
            return self.send(200, "ok")
        if self.path == "/":
            return self.send(200, PAGE, "text/html; charset=utf-8")
        self.send(404, "not found")

    def do_POST(self) -> None:
        if not self.path.startswith("/import"):
            return self.send(404, "not found")
        token = os.environ["UPLOAD_TOKEN"]
        if not hmac.compare_digest(self.headers.get("X-Token", ""), token):
            return self.send(401, "wrong or missing token")
        size = int(self.headers.get("Content-Length") or 0)
        if not 0 < size <= 25 * 1024 * 1024:
            return self.send(413, "send one PDF of at most 25 MB")
        body = self.rfile.read(size)
        named = urllib.request.unquote(self.headers.get("X-Filename", ""))
        st = None
        try:
            pdf, filename = pdf_from_request(self.headers.get("Content-Type", ""), body)
        except StatementError as e:   # kept as uploaded, to see what was sent
            return self.finish_upload(body, named, None, 422, f"Not imported: {e}")
        filename = named or filename
        try:
            st = parse(pdf_text(pdf))
            if "dry_run" in self.path:
                return self.finish_upload(pdf, filename, st, 200, render(st, None))
            result = import_statement(st, pdf, filename, Sure(os.environ["SURE_URL"], os.environ["SURE_API_KEY"]), {
                "debit": os.environ.get("SURE_DEBIT_ACCOUNT", "Nu Débito"),
                "credit": os.environ.get("SURE_CREDIT_ACCOUNT", "Nu Crédito")})
            self.finish_upload(pdf, filename, st, 200, render(st, result))
        except StatementError as e:
            self.finish_upload(pdf, filename, st, 422, f"Not imported: {e}")
        except Exception as e:  # noqa: BLE001 - reported to the person uploading
            self.finish_upload(pdf, filename, st, 502, f"Not imported: {e}")

    def finish_upload(self, data: bytes, filename: str, st: "Statement | None",
                      status: int, message: str) -> None:
        sha = keep_upload(data, filename, st, status, message)
        what = f"{st.kind} {st.account_ref} {st.period_start}..{st.period_end}" if st else "unparsed"
        self.log_message("upload %s %r (%s): %d %s", sha, filename, what, status,
                         " | ".join(message.strip().splitlines()))
        self.send(status, message)


def keep_upload(data: bytes, filename: str, st: "Statement | None", status: int, message: str) -> str:
    """Store every upload, imported or not, with the outcome of each attempt.

    STATEMENTS_DIR/<sha256[:16]>.pdf holds the file (.bin if it is not a PDF);
    the .json next to it lists every attempt. Sending the same file again adds
    an attempt, not a copy.
    """
    sha = hashlib.sha256(data).hexdigest()[:16]
    folder = os.environ.get("STATEMENTS_DIR", "/statements")
    try:
        os.makedirs(folder, exist_ok=True)
        base = os.path.join(folder, sha)
        ext = ".pdf" if data.startswith(b"%PDF") else ".bin"
        if not os.path.exists(base + ext):
            with open(base + ext, "wb") as f:
                f.write(data)
        record = {}
        if os.path.exists(base + ".json"):
            with open(base + ".json") as f:
                record = json.load(f)
        attempts = record.get("attempts", [])
        attempts.append({
            "at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "status": status,
            "message": message.strip(),
        })
        with open(base + ".json", "w") as f:
            json.dump({
                "filename": filename or record.get("filename", ""),
                # a later attempt that fails before parsing keeps what an earlier one read
                "statement": record.get("statement") if st is None else {
                    "kind": st.kind, "account": st.account_ref,
                    "period": [str(st.period_start), str(st.period_end)],
                    "balance": str(st.balance), "movements": len(st.transactions)},
                "attempts": attempts,
            }, f, ensure_ascii=False, indent=2)
    except OSError as e:          # storage trouble must not hide the import result
        print(f"could not keep upload {sha}: {e}", file=sys.stderr)
    return sha


def render(st: Statement, result: dict | None) -> str:
    kind = "Cuenta Nu (débito)" if st.kind == "debit" else f"Tarjeta de crédito ••{st.account_ref}"
    lines = [f"{kind}, {st.period_start} a {st.period_end}",
             f"Movimientos: {len(st.transactions)}", f"Saldo: ${st.balance:,}"]
    if result:
        lines += [f"Nuevos en Sure: {result['transactions_created']}",
                  f"Ya estaban: {result['transactions_already_there']}",
                  f"Estado de cuenta archivado: {result['statement_filed']}"]
    else:
        lines.append("(prueba: no se escribió nada en Sure)")
    return "\n".join(lines) + "\n"


def main(argv: list[str]) -> int:
    if len(argv) >= 3 and argv[1] == "parse":
        st = parse(pdf_text(open(argv[2], "rb").read()))
        print(render(st, None), end="")
        print(json.dumps(st.checks, indent=2, ensure_ascii=False))
        for t in st.transactions:
            print(f"{t.date}  {t.amount:>12}  {t.name}")
        return 0
    if len(argv) == 2 and argv[1] == "serve":
        port = int(os.environ.get("PORT", "8080"))
        ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
