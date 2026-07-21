# Module: `reconcile` — Reconciliation Engine

**Path:** `masters_india_saas/reconcile/` · **Mounts:** `/v1/api/reconcile/`, `/v2/api/reconcile/`, `/v2/api/reconcile-v3/`, `/v2/api/vendor-followup/`

## Summary
The reconciliation engine matches a taxpayer's **purchase register (PR)** against **government portal data** (GSTR-2A, 2B, 8A, IMS) and lets users accept / reject / force-match / de-link invoices, run reco jobs, view supplier ledgers, download reports, and follow up with vendors over mismatches. It has **no Django models** — all data lives in MongoDB, accessed through `legacy_autotax.collections` (raw PyMongo). Results are grouped into **buckets**: Matched, Mismatched, NI2A (not in 2A), INA (not in purchase), PM (probable match), plus ITC-reversed and combined-record variants.

**Reco types:** `2A-PR`, `2B-PR`, `8A-PR`, `IMS-PR`.

**Layout:**
| Area | Purpose |
|---|---|
| `views.py` | v1 API (legacy) |
| `v2/views.py` | v2 API (headers-based auth, IMS sync, combine, ITC aging) |
| `v3/views.py` | v3 read APIs — performance-tuned bucket reads with Mongo index hints |
| `vendor_followup/` | Vendor follow-up: cases, emails (LLM-generated), escalation, dashboard |
| `actions/` | Action handlers: accept, reject, force_match, delink, gstr1_action, gstr3b_override |
| `ledger/`, `overview/`, `reports/` | Supplier ledger, overview reports, Excel report builders |
| `filter.py`, `constants.py` | Bucket filters and reco constants |

> ⚠️ **Security:** `Ledger.get_report` (in both `views.py` and `v2/views.py`) has **hard-coded AWS access/secret keys** in source — flagged in the RCA wiki `operations/committed-secrets.md`; values not reproduced here. Rotate & externalize.

---

## v1 — base `/v1/api/reconcile/`

### `auto/` — `AutoReconcileView` (router viewset) — buckets & match actions
| Method | Path | Handler | Summary |
|---|---|---|---|
| GET | `auto/gstr2aList/` | `gstr2aList` | Portal-side (GSTR-2A/2B) invoice list for a reco. |
| GET | `auto/purchaselist/` | `purchaselist` | Purchase-register invoice list for a reco. |
| GET | `auto/bucket-data/` | `bucket_data` | Matched-bucket (reco_cache) records. |
| GET | `auto/notIn2A/` | `notIn2A` | Purchase invoices absent from portal (INA/NI2A). |
| GET | `auto/accepted_records/` | `autorecoAcceptedRecords` | Accepted/matched records. |
| GET | `auto/linked_invoice/` | `autorecoLinkedInvoice` | Manually linked / force-matched invoices. |
| POST | `auto/accept-action/` | `autoRecoSwapStatus` | Accept / swap match status. |
| POST | `auto/accept-reject/` | `autoRecoAcceptRejectStatus` | Combined accept-then-reject transition. |
| POST | `auto/reject-action/` | `autoRecoRejeect` | Reject invoices. |
| GET | `auto/global_filter/` | `globalFilter` | Global search/filter across buckets. |
| GET | `auto/refresh-caching-table/` | `refreshCachingTables` | Rebuild reco cache tables for a job. |
| GET | `auto/supplier_detail_new/` | `supplierDetialNew` | Supplier-wise detail for a bucket. |
| GET | `auto/buyer_gstin/` | `buyerGstin` | Buyer GSTINs participating in the reco. |
| GET | `auto/consolidate_vendor_report/` | `downloadConsolidateVendorReport` | Download consolidated vendor report. |
| POST | `auto/auto_reco_dlink/` | `dlink_invoice` | De-link (unmatch) invoices. |
| POST | `auto/bulk_force_match/` | `bulkForceMatch` | Bulk force-match invoices. |
| GET | `auto/get_bulk_jobs/` | `getBulkJobs` | List bulk-operation jobs. |
| GET | `auto/bulk_job_invoice_data/` | `getBulkJobInvoiceData` | Invoice data for a bulk job. |
| POST | `auto/auto_linked_invoice/` | `autoLinkedInvoice` | Force-match linked invoices. |
| GET | `auto/auto_get_supplier_data/` | `autoGetSupplierData` | Suppliers for force-match popup. |
| GET | `auto/auto_get_invoice_data/` | `autoGetInvoiceData` | Invoices for force-match popup. |
| POST | `auto/send_remainder/` | `sendGstrTwoRemainder` | Email GSTR-2 reminders to suppliers. |
| GET | `auto/auto_reco/` | `auto_reco_list` | List auto-reco records. |
| GET | `auto/auto_reco_analytics/` | `auto_reco_analytics` | Reco analytics aggregates. |
| GET | `auto/get_analytics/` | `get_analytics` | Reco analytics summary. |
| GET | `auto/overview_report_dashboard/` | `reco_report_dashboard` | Overview dashboard data. |
| GET | `auto/return_all_jobs/` | `return_all_jobs_for_user` | All reco jobs for the user. |
| POST | `auto/auto_reco_update_sale/` | `autoRecoUpdateGstr1` | Push reco result into GSTR-1/sale. |
| POST | `auto/auto_reco_delete_gstr1/` | `autoRecoDeleteGstr1` | Delete a GSTR-1 entry from reco. |

### `ledger/` — `Ledger` (router viewset) — supplier ledger
| Method | Path | Handler | Summary |
|---|---|---|---|
| GET | `ledger/supplier_list_ledger/` | `getSupplierListForLedger` | Suppliers available for the ledger view. |
| GET | `ledger/ledger_data/` | `getLedgerData` | Supplier-wise mismatch ledger. |
| GET | `ledger/ledger_details_data/` | `getLedgerDetailsData` | Detailed ledger rows for a supplier. |
| POST | `ledger/save_ledger_data/` | `saveLedgerData` | Save ledger/mail entries. |
| GET | `ledger/get_report/` | `getLedgerReport` | Download ledger report. ⚠️ hard-coded AWS creds. |
| GET | `ledger/ledger_mail_log/` | `getLedgerMailLog` | Ledger email send log. |
| GET | `ledger/ledger_mail_log_report/` | `getLedgerMailLogReport` | Download ledger mail-log report. |

### Standalone routes (explicit in `urls.py`, non-router classes)
| Method | Path | Handler | Summary |
|---|---|---|---|
| GET | `location_list/` | `BusinessLocation.getBusinessList` | Business/location list for the org. |
| GET | `overview_report/` | `OverviewRecoReport.get` | Build Excel overview report. |
| GET | `overview_report_new/` | `OverviewRecoReport.reco_overview_download` | Download overview report. |
| GET | `overview_report_dashboard_new/` | `OverviewRecoReport.reco_report_dashboard_new` | Overview dashboard data. |
| GET | `business_gstin_list/` | `AddRecoJob.getBusinessGstin` | Business GSTINs for job creation. |
| GET | `business_gstin_list_new/` | `AddRecoJob.getBusinessGstinV2` | Business GSTINs (date-range aware). |
| POST | `addJob/` | `AddRecoJob.createJob` | Create a reco job. |
| POST | `deleteJob/` | `AddRecoJob.deleteJob` | Delete a reco job. |
| GET | `get_job_detail/` | `AddRecoJob.getJobDetail` | Reco job detail (for edit). |
| GET | `get_job_log/` | `AddRecoJob.getJobLog` | Reco job / sub-job logs. |
| GET | `execute_now/` | `AddRecoJob.executeNow` | Trigger a reco job run immediately. |
| POST | `set_download_metadata/` | `ManageDownloadReports.save_download_metadata` | Save download-report metadata. |
| GET | `get_download_metadata/` | `ManageDownloadReports.get_download_metadata` | Get download-report metadata. |
| DELETE | `delete_download_metadata/` | `ManageDownloadReports.delete_download_metadata` | Delete download-report metadata. |

*(v1 `urls.py` also declares convenience aliases — `save_remarks/`, `bulk_force_match/`, `auto_linked_invoice/`, `auto_get_invoice_data/`, `send_remainder/`, `auto_reco_dlink/` — pointing at the same `AutoReconcileView` methods listed under `auto/` above.)*

---

## v2 — base `/v2/api/reconcile/`
Same shape as v1, headers-based auth (`Arap-User`, `Basic-Settings`), plus IMS-sync, invoice-combine, and ITC-aging additions.

### `auto/` — `AutoReconcileView` — additions/changes over v1
| Method | Path | Handler | Summary |
|---|---|---|---|
| GET | `auto/gstr2a-list/` | `gstr2aList` | Portal-side invoice list (renamed path). |
| GET | `auto/purchase-list/` | `purchaselist` | Purchase-register list (renamed path). |
| GET | `auto/not-in-gst/` | `notIn2A` | Purchase invoices absent from portal. |
| GET | `auto/reversed-records/` | `autoITCReverseRecords` | ITC-reversed records bucket. |
| POST | `auto/save_reference_number/` | `saveReferenceNumber` | Save reference number on force-matched records. |
| POST | `auto/reco-ims-bulk-status-and-upload/` | `reco_ims_bulk_status_and_upload` | Convert reco IDs → IMS ids, bulk status-update + queue upload. |
| POST | `auto/sync-reco-to-ims/` | `sync_reco_to_ims` | Start a reco→IMS sync job. |
| POST | `auto/ims-reco-sync-action/` | `ims_reco_sync_action` | Apply an IMS-reco sync action. |
| GET | `auto/ims-sync-audit-trail/` | `ims_sync_audit_trail` | Audit trail for IMS-reco sync. |
| GET | `auto/get_combine_options/` | `getCombineOptions` | Options for combining B2B/CDN invoices. |
| POST | `auto/combine_invoice/` | `combineInvoice` | Combine multiple invoices into one match unit. |
| GET | `auto/combined_records/` | `autorecoCombinedRecords` | Combined-records bucket. |
| GET | `auto/get_autofill_metadata/` | `get_autofill_metadata` | Autofill metadata for the reco period. |

*(v2 `auto/` also carries the v1 actions: `bucket-data`, `accepted_records`, `linked_invoice`, `accept-action`, `accept-reject`, `reject-action`, `global_filter`, `refresh-caching-table`, `supplier_detail_new`, `auto_reco_dlink`, `bulk_force_match`, `get_bulk_jobs`, `bulk_job_invoice_data`, `auto_linked_invoice`, `auto_get_supplier_data`, `auto_get_invoice_data`, `send_remainder`, `auto_reco`, `auto_reco_analytics`, `save_remarks`, `auto_reco_update_sale`, `auto_reco_delete_gstr1`.)*

### `ledger/` — `Ledger`
Same as v1, plus:
| Method | Path | Handler | Summary |
|---|---|---|---|
| GET | `ledger/ledger_mail_autofill/` | `getLedgerMailAutofill` | Autofill data for the ledger follow-up mail. |

### `gpt/` — `GptSyncView` (router viewset)
| Method | Path | Handler | Summary |
|---|---|---|---|
| POST | `gpt/sync_recent_jobs/` | `sync_recent_jobs` | Sync recent GST-portal-tool (GPT) reco jobs. |
| GET | `gpt/get_gpt_sync_status/` | `get_gpt_sync_status` | Status of a GPT sync run. |

### Standalone routes (explicit in `v2/urls.py`)
| Method | Path | Handler | Summary |
|---|---|---|---|
| GET | `location_list/` | `BusinessLocation.getBusinessList` | Business/location list. |
| GET | `overview_report_new/` | `OverviewRecoReport.reco_overview_download` | Download overview report. |
| GET | `overview_report_dashboard_new/` | `OverviewRecoReport.reco_report_dashboard_new` | Overview dashboard data. |
| GET | `itc_aging_data/` | `ItcAgingReport.itc_aging_data` | ITC-aging data (bucketed by amount). |
| GET | `itc_aging_report/` | `ItcAgingReport.itc_aging_report` | Download ITC-aging report. |
| GET | `itc_aging_days_data/` | `ItcAgingReport.itc_aging_days_data` | ITC-aging data bucketed by days. |
| GET | `itc_aging_days_report/` | `ItcAgingReport.itc_aging_days_report` | Download ITC-aging-days report. |
| GET | `business_gstin_list_new/` | `AddRecoJob.getBusinessGstinV2` | Business GSTINs (date-range aware). |
| POST | `reco_business_list/` | `AddRecoJob.getRecoParticipatingBusiness` | Filter businesses to those with a reco job. |
| POST | `addJob/` | `AddRecoJob.createJob` | Create a reco job. |
| POST | `deleteJob/` | `AddRecoJob.deleteJob` | Delete a reco job. |
| GET | `get_job_detail/` | `AddRecoJob.getJobDetail` | Reco job detail. |
| GET | `get_job_log/` | `AddRecoJob.getJobLog` | Reco job / sub-job logs. |
| POST | `get-action-sync-jobs/` | `AddRecoJob.getActionSyncJobs` | List action-sync jobs across reco types. |
| GET | `execute_now/` | `AddRecoJob.executeNow` | Trigger a reco job run immediately. |
| POST | `save_remarks/` | `AutoReconcileView.saveRemarks` | Save remarks. |
| POST | `save_reference_number/` | `AutoReconcileView.saveReferenceNumber` | Save reference number. |
| POST | `set_download_metadata/` | `ManageDownloadReports.save_download_metadata` | Save download metadata. |
| GET | `get_download_metadata/` | `ManageDownloadReports.get_download_metadata` | Get download metadata. |
| DELETE | `delete_download_metadata/` | `ManageDownloadReports.delete_download_metadata` | Delete download metadata. |
| GET | `get_autofill_metadata/` | `AutoReconcileView.get_autofill_metadata` | Autofill metadata (alias). |

---

## v3 — base `/v2/api/reconcile-v3/`
Read-optimized bucket APIs. `AutoReconcileV3View` applies **Mongo compound-index hints** per reco_type and supports multi-select `invoice_type` filtering (fixing v2's hardcoded combine behavior).

### `auto/` — `AutoReconcileV3View` (router viewset)
| Method | Path | Handler | Summary |
|---|---|---|---|
| GET | `auto/bucket-data/` | `bucket_data` | Matched bucket (index-hinted read). |
| GET | `auto/gstr2a-list/` | `gstr2a_list` | Portal-side invoice list. |
| GET | `auto/purchase-list/` | `purchase_list` | Purchase-register list. |
| GET | `auto/not-in-gst/` | `not_in_2a` | Purchase invoices absent from portal. |
| GET | `auto/accepted_records/` | `accepted_records` | Accepted/matched records. |
| GET | `auto/linked_invoice/` | `linked_invoice` | Linked / force-matched invoices. |
| GET | `auto/reversed-records/` | `reversed_records` | ITC-reversed records. |
| GET | `auto/combined_records/` | `combined_records` | Combined records (multi-select invoice_type). |
| GET | `auto/global_filter/` | `global_filter` | Global search/filter. |
| GET | `auto/supplier_detail_new/` | `supplier_list` | Supplier-wise detail. |

---

## vendor-followup — base `/v2/api/vendor-followup/`
Vendor follow-up over mismatches: raise **cases**, generate follow-up **emails via LLM** (`llm_services.py`, `prompts.py`), run scheduled **jobs**, and track **escalation**. Class-based `APIView`s; all read org/user from the `Arap-User` header.

### Config
| Method | Path | Handler | Summary |
|---|---|---|---|
| GET/POST | `email-config/` | `EmailConfigView` | Get or save the org's follow-up email (SMTP/IMAP) config. |
| POST | `email-config/test-connection/` | `TestEmailConnectionView` | Test the configured email connection. |

### Jobs
| Method | Path | Handler | Summary |
|---|---|---|---|
| GET | `jobs/` | `JobListView` | List follow-up jobs. |
| POST | `jobs/trigger/` | `JobTriggerView` | Trigger a follow-up job run. |
| GET | `jobs/<job_id>/` | `JobDetailView` | Job detail. |
| POST | `jobs/<job_id>/pause/` | `JobPauseView` | Pause a job. |
| POST | `jobs/<job_id>/resume/` | `JobResumeView` | Resume a job. |
| POST | `jobs/<job_id>/cancel/` | `JobCancelView` | Cancel a job. |

### Cases
| Method | Path | Handler | Summary |
|---|---|---|---|
| POST | `cases/initiate/` | `CaseInitiateView` | Initiate follow-up case(s) for mismatched invoices. |
| GET | `cases/` | `CaseListView` | List/aggregate cases. |
| GET | `cases/<case_id>/` | `CaseDetailView` | Case detail. |
| GET | `cases/<case_id>/communications/` | `CaseCommunicationsView` | Email/communication thread for a case. |
| GET | `cases/<case_id>/mismatch-data/` | `CaseMismatchDataView` | Mismatch invoice data behind a case. |
| POST | `cases/<case_id>/reply/` | `CaseReplyView` | Record/send a reply on a case. |
| POST | `cases/<case_id>/generate-email/` | `CaseGenerateEmailView` | LLM-generate a follow-up email draft. |
| POST | `cases/<case_id>/override-category/` | `CaseOverrideCategoryView` | Override the AI-assigned mismatch category. |
| POST | `cases/<case_id>/update-details/` | `CaseUpdateDetailsView` | Update case details. |
| POST | `cases/<case_id>/update-bucket/` | `CaseUpdateBucketView` | Move case to a different bucket. |
| POST | `cases/<case_id>/update-cc/` | `CaseUpdateCCView` | Update CC recipients. |
| POST | `cases/<case_id>/pause/` | `CasePauseView` | Pause a case. |
| POST | `cases/<case_id>/resume/` | `CaseResumeView` | Resume a case. |
| POST | `cases/<case_id>/resolve/` | `CaseResolveView` | Resolve a case. |
| POST | `cases/<case_id>/escalate/` | `CaseEscalateView` | Escalate a case per the escalation matrix. |

### Invoices & Dashboard
| Method | Path | Handler | Summary |
|---|---|---|---|
| GET | `invoices/` | `InvoiceListView` | List invoices in follow-up (date-range filterable). |
| GET | `dashboard/summary/` | `DashboardSummaryView` | Follow-up summary + resolution/AI-accuracy metrics. |
| GET | `dashboard/priority-queue/` | `DashboardPriorityQueueView` | Priority queue of cases needing attention. |
| GET | `dashboard/vendor-summary/` | `DashboardVendorSummaryView` | Per-vendor follow-up summary. |

---

**Related:** RCA wiki `domains/reconcile.md` (failure modes), `operations/committed-secrets.md` (the `get_report` keys), and `reference/` (once built).
