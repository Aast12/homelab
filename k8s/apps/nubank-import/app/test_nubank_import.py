"""Tests on statement text laid out like Nubank's PDFs (made-up people and amounts).

    python -m unittest test_nubank_import.py
"""

import unittest
from decimal import Decimal

import nubank_import as ni

DEBIT = """
                                                                             Ana Ejemplo
                                                                       Cuenta Nu: 00011112222
                                                                Periodo: del 01 al 30 sep 2026
  Saldo inicial                                                                      $1,000.00
  Depósitos                                                                          +$500.00
  Gastos                                                                             -$300.50
  Comisiones cobradas por Nu                                                            $0.00
  Dinero generado este mes                                                             $10.25
  Saldo al generar este estado de cuenta                                             $1,209.75
        Detalle de movimientos en tu cuenta
  FECHA                DEL 01 AL 30 SEP 2026 (30 DÍAS)                   MONTO EN PESOS MEXICANOS
 20 SEP 2026          Cajero N1 Retiro de efectivo                                   -$234.80
                      Comisión N1                                                       $34.80
 19 SEP 2026          Retiro de Cajita: Ahorro                                       +$900.00
 15 SEP 2026          Depósito en Cajita: Ahorro                                     -$900.00
 10 SEP 2026          Juan Ejemplo Renta                                              -$65.70
                      Transferencia SPEI, Hora: 10:00:00, Enviado a BANCO.
Nubank, S.A., Institución de Banca Múltiple Manuel Ávila Camacho #201
                                                                                        2 de 3
                                                                             Ana Ejemplo
                                                                       Cuenta Nu: 00011112222
  FECHA                DEL 01 AL 30 SEP 2026 (30 DÍAS)                   MONTO EN PESOS MEXICANOS
 02 SEP 2026          EMPRESA SA traspaso                                            +$500.00
         Con estos movimientos, tu saldo promedio del periodo fue de $1,100.00
        Detalle de movimientos de tus cajitas
 19 SEP 2026          Retiro de Cajita: Ahorro                                       -$900.00
"""

CREDIT = """
   Producto: Tarjeta de Crédito Nu, Oro
   C ALGUNA 1          Periodo:          06 SEP 2026 al 05 OCT
                       Fecha de corte:                    05 OCT 2026
   Número de tarjeta: XXXX-XXXX-XXXX-1234
                       Saldo cargos regulares                   $100.00
                       Saldo cargos a meses                     $250.00
                CARGOS, ABONOS Y COMPRAS REGULARES (NO A MESES)
                     Tarjeta titular: Ana Ejemplo
 12 MAY 2026    06 SEP 2026     Tienda A Meses - 5/6 | RFC: S.I.          +$50.00
                                 Tarjeta virtual **** 9999
 10 SEP 2026    10 SEP 2026     Cafe Ejemplo | RFC: ABC123              +$75.00
 10 SEP 2026    10 SEP 2026     Cafe Ejemplo | RFC: ABC123              +$75.00
 11 SEP 2026    11 SEP 2026     Aumentaste tu límite de crédito con garantía $1,000.00
 18 AGO 2025    06 OCT 2026     IVA sobre intereses | RFC: S.I.            +$5.00
 05 OCT 2026    06 OCT 2026     ¡Gracias por tu pago! | RFC: S.I.         -$400.00
                                 Abono (con cuenta Nu)
                                                     Total de cargos      +$200.00
                                                     Total de abonos      -$400.00
"""


class Debit(unittest.TestCase):
    def setUp(self):
        self.st = ni.parse(DEBIT)

    def test_reads_period_balance_and_account(self):
        self.assertEqual((self.st.kind, self.st.account_ref), ("debit", "00011112222"))
        self.assertEqual(str(self.st.period_start), "2026-09-01")
        self.assertEqual(str(self.st.period_end), "2026-09-30")
        self.assertEqual(self.st.balance, Decimal("1209.75"))

    def test_skips_cajita_moves_and_adds_interest(self):
        names = [t.name for t in self.st.transactions]
        self.assertEqual(names, ["Cajero N1 Retiro de efectivo", "Juan Ejemplo Renta",
                                 "EMPRESA SA traspaso", "Rendimientos Cuenta Nu"])
        self.assertEqual([t.amount for t in self.st.transactions],
                         [Decimal("234.80"), Decimal("65.70"), Decimal("-500.00"), Decimal("-10.25")])

    def test_details_become_notes_and_stop_at_page_footer(self):
        cajero, renta, empresa, _ = self.st.transactions
        self.assertEqual(cajero.notes, "Comisión N1 $34.80")
        self.assertEqual(renta.notes, "Transferencia SPEI, Hora: 10:00:00, Enviado a BANCO.")
        self.assertEqual(empresa.notes, "")

    def test_refuses_a_statement_that_does_not_add_up(self):
        with self.assertRaises(ni.StatementError):
            ni.parse(DEBIT.replace("-$65.70", "-$65.71"))


class Credit(unittest.TestCase):
    def setUp(self):
        self.st = ni.parse(CREDIT)

    def test_reads_card_period_and_balance(self):
        self.assertEqual((self.st.kind, self.st.account_ref), ("credit", "1234"))
        self.assertEqual((str(self.st.period_start), str(self.st.period_end)), ("2026-09-06", "2026-10-05"))
        self.assertEqual(self.st.balance, Decimal("350.00"))

    def test_rows_dates_and_signs(self):
        rows = [(str(t.date), t.name, t.amount) for t in self.st.transactions]
        self.assertEqual(rows, [
            ("2026-09-06", "Tienda A Meses - 5/6", Decimal("50.00")),     # instalment: charge date
            ("2026-09-10", "Cafe Ejemplo", Decimal("75.00")),
            ("2026-09-10", "Cafe Ejemplo", Decimal("75.00")),
            ("2026-10-06", "IVA sobre intereses", Decimal("5.00")),       # charged after the cut-off
            ("2026-10-05", "¡Gracias por tu pago!", Decimal("-400.00")),
        ])
        self.assertEqual(self.st.transactions[0].notes, "Tarjeta virtual **** 9999")
        self.assertEqual(self.st.transactions[-1].notes, "Abono (con cuenta Nu)")

    def test_identical_rows_get_distinct_stable_ids(self):
        ids = [t.external_id for t in self.st.transactions]
        self.assertEqual(len(set(ids)), len(ids))
        self.assertEqual(ids, [t.external_id for t in ni.parse(CREDIT).transactions])

    def test_refuses_a_statement_that_does_not_add_up(self):
        with self.assertRaises(ni.StatementError):
            ni.parse(CREDIT.replace("+$200.00", "+$205.00"))


class WrappedYearsAndRevolvingBalance(unittest.TestCase):
    def test_a_year_on_the_next_line_belongs_to_the_date_above(self):
        wrapped = DEBIT.replace(" 10 SEP 2026          Juan Ejemplo Renta",
                                " 10 SEP               Juan Ejemplo Renta").replace(
            "-$65.70\n", "-$65.70\n 2026\n", 1)
        self.assertIn(" 10 SEP               Juan", wrapped)
        st = ni.parse(wrapped)
        self.assertEqual([t.name for t in st.transactions].count("Juan Ejemplo Renta"), 1)
        self.assertEqual(st.transactions[0].notes, "Comisión N1 $34.80")

    def test_revolving_balance_lines_are_skipped(self):
        text = CREDIT.replace(
            " 05 OCT 2026    06 OCT 2026     ¡Gracias",
            " 16 SEP 2026    16 SEP 2026     Saldo revolvente | RFC: S.I.          +$900.00\n"
            " 16 SEP 2026    16 SEP 2026     Crédito de saldo revolvente | RFC: S.I.  -$900.00\n"
            " 05 OCT 2026    06 OCT 2026     ¡Gracias").replace("Total de abonos      -$400.00",
                                                            "Total de abonos      -$1,300.00")
        st = ni.parse(text)
        self.assertNotIn("Saldo revolvente", [t.name for t in st.transactions])
        self.assertNotIn("Crédito de saldo revolvente", [t.name for t in st.transactions])
        self.assertEqual(len(st.transactions), 5)


class KeepUploads(unittest.TestCase):
    def test_every_upload_is_kept_once_with_each_attempt(self):
        import json, os, tempfile
        with tempfile.TemporaryDirectory() as d:
            os.environ["STATEMENTS_DIR"] = d
            st = ni.parse(DEBIT)
            sha = ni.keep_upload(b"%PDF-1.7 a", "oct.pdf", st, 200, "ok")
            self.assertEqual(ni.keep_upload(b"%PDF-1.7 a", "oct.pdf", None, 422, "Not imported: x"), sha)
            ni.keep_upload(b"hello", "", None, 422, "Not imported: not a PDF")
            files = sorted(os.listdir(d))
            self.assertEqual(len(files), 4)                       # two uploads, each file + json
            self.assertTrue(any(f.endswith(".bin") for f in files))
            with open(os.path.join(d, sha + ".json")) as f:
                rec = json.load(f)
            self.assertEqual([a["status"] for a in rec["attempts"]], [200, 422])
            self.assertEqual(rec["statement"]["period"], ["2026-09-01", "2026-09-30"])


class Upload(unittest.TestCase):
    def test_raw_pdf_and_multipart(self):
        pdf = b"%PDF-1.7 test"
        self.assertEqual(ni.pdf_from_request("application/pdf", pdf)[0], pdf)
        body = (b"--b\r\nContent-Disposition: form-data; name=\"file\"; filename=\"oct.pdf\"\r\n"
                b"Content-Type: application/pdf\r\n\r\n" + pdf + b"\r\n--b--\r\n")
        self.assertEqual(ni.pdf_from_request("multipart/form-data; boundary=b", body), (pdf, "oct.pdf"))

    def test_rejects_non_pdf(self):
        with self.assertRaises(ni.StatementError):
            ni.pdf_from_request("application/pdf", b"hello")

    def test_rejects_unknown_document(self):
        with self.assertRaises(ni.StatementError):
            ni.parse("some other bank")


if __name__ == "__main__":
    unittest.main()
