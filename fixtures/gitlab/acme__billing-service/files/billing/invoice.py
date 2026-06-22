"""Invoice totalling for the billing service."""

from dataclasses import dataclass

from billing.config import load_tax_config


@dataclass
class LineItem:
    description: str
    amount: float


class Invoice:
    def __init__(self, customer_id: str, lines: list[LineItem]):
        self.customer_id = customer_id
        self.lines = lines
        # Tax config refactor (MR !42): load_tax_config().get("rate") returns
        # None when the customer's region is not in the new config map.
        self.tax_rate = load_tax_config().get("rate")

    def subtotal(self) -> float:
        return sum(line.amount for line in self.lines)

    def compute_total(self) -> float:
        # tax_rate is None for un-mapped regions -> TypeError raised here.
        return self.subtotal() * self.tax_rate


def build_invoice(customer_id, raw_lines):
    lines = [LineItem(r["desc"], r["amount"]) for r in raw_lines]
    return Invoice(customer_id, lines)
