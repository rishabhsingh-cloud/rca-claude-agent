# Demo Platform — Architecture & API Reference (test fixture)

## RCA Quick Orientation (read this first)

Two services. Errors surface in the API gateway but often originate in the billing engine across an HTTP boundary.

| Symptom area | Most likely boundary | Where to look |
|---|---|---|
| Invoice total wrong / 500 on create | api -> billing engine HTTP call | billing/invoice.py compute_total |
| Tax rate missing for a region | tax-config load | billing/config.py load_tax_config |

## 1. Project Summaries

### billing-service
Computes invoices and exposes the billing API.

## 2. Inter-Project Dependencies (service-to-service map)

| From | To | How | What |
|---|---|---|---|
| api-gateway | billing-service | HTTP POST /invoices | invoice creation forwarded |

## Appendix — Cross-cutting RCA notes

- Tax rates are region-keyed; un-mapped regions can yield a null rate (TypeError downstream).
- Invoice totals are computed in billing/invoice.py, not the gateway.
