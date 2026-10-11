"""Helpers for the shopping receipt. Every public function here can be used
in template.j2, both as a function and as a filter."""

from decimal import Decimal


def money(value):
    """12.5 -> "12,50" (Dutch notation)."""
    return "{:.2f}".format(Decimal(value)).replace(".", ",")


def epc_qr(payee, iban, amount, description="", bic=""):
    """The text of an EPC QR code: a SEPA transfer request banking apps can scan.

    See the European Payments Council's "Quick Response Code" guidelines
    (EPC069-12), version 002.
    """
    amount = Decimal(amount).quantize(Decimal("0.01"))
    if not Decimal("0.01") <= amount <= Decimal("999999999.99"):
        raise ValueError("an EPC QR amount must be between 0.01 and 999999999.99")
    if not payee or len(payee) > 70:
        raise ValueError("the payee name must be 1 to 70 characters")
    iban = iban.replace(" ", "").upper()
    _check_iban(iban)
    lines = [
        "BCD",           # service tag
        "002",           # version
        "1",             # character set: UTF-8
        "SCT",           # SEPA credit transfer
        bic,
        payee,
        iban,
        "EUR%s" % amount,
        "",              # purpose code
        "",              # structured reference
        description,     # free text reference
    ]
    return "\n".join(lines).rstrip("\n")


def _check_iban(iban):
    """Catch typos with the IBAN check digits (ISO 13616 mod 97)."""
    rearranged = iban[4:] + iban[:4]
    digits = "".join(str(int(char, 36)) for char in rearranged if char.isalnum())
    if len(iban) < 15 or not iban.isalnum() or int(digits) % 97 != 1:
        raise ValueError("%r is not a valid IBAN" % iban)
