<!-- AUTO-GENERATED BLOCK (indexer owns this; do not hand-edit) -->
project: acme/billing-service
sha: abc1234d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b
generated_at: 2026-06-20

# billing-service

Auto-generated structural summary (symbols + areas from the code graph).

## Areas
- `billing/api.py` — `create_invoice_endpoint`
- `billing/invoice.py` — `LineItem`, `Invoice`, `Invoice.__init__`, `Invoice.subtotal`, `Invoice.compute_total`, `build_invoice`

## Entry points
- `Invoice.__init__`
- `create_invoice_endpoint`

## Keywords
api billing build compute create endpoint invoice lineitem subtotal total

<!-- END AUTO-GENERATED BLOCK -->

<!-- HUMAN BLOCK (agent never touches) -->
## Notes for QA
Tax rates are region-keyed. Regions outside us/eu have historically defaulted to 0.
<!-- END HUMAN BLOCK -->
