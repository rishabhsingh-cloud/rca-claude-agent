"""Billing HTTP API."""

from flask import Blueprint, request, jsonify

from billing.invoice import build_invoice

bp = Blueprint("billing", __name__)


@bp.route("/invoices", methods=["POST"])
def create_invoice_endpoint():
    payload = request.get_json()
    customer_id = payload["customer_id"]
    raw_lines = payload["lines"]
    invoice = build_invoice(customer_id, raw_lines)
    total = invoice.compute_total()
    return jsonify({"customer_id": customer_id, "total": total})
