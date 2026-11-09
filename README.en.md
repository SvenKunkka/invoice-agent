# PiaoJia — local Taobao / JD invoice assistant

[简体中文](README.md)

A macOS local app for personal Taobao and JD accounts purchasing on behalf of one company. It manages orders, prefers explicitly supported special VAT invoices, previews applications, drafts merchant requests, archives original invoice files and exports monthly CSV summaries.

Run `start.command` to open <http://127.0.0.1:8847>. The app requires macOS, uv, Python 3.13, and Google Chrome. Building the frontend also requires Node.js and npm. Stop the server with Control+C in its terminal. Use `INVOICE_AGENT_PORT` or `INVOICE_AGENT_DATA` to override the port or data directory.

The app opens the live workspace directly. Starting with `INVOICE_AGENT_PREVIEW_ONLY=1` keeps live review and previews available while pausing application entry and submission.

Enter company details, open each platform's dedicated browser, and sign in. Scan orders or import CSV/JSON, confirm business purchases and invoice evidence, then preview each eligible application. Unknown invoice types, refunds, ambiguous page layouts and unclear submission results stop automatic processing.

Importing an order does not classify it as a business purchase. The default work queue, purchase totals and monthly CSV contain confirmed business purchases only. Separate views show all imports, unreviewed orders and exclusions. Exclusions retain their reason across scans; restoring an excluded order requires an explicit save. Uncertain application warnings remain visible across these views.

Live data is stored under `~/Library/Application Support/InvoiceAgent/live/`. Order and company payloads are encrypted with a local file key. Back up the database, its `encryption.key`, and original invoice files together. Browser sessions remain in the dedicated local profile.

Implemented: local dashboard, deterministic special-first rules, durable single-use application snapshots, manual reconciliation, merchant message drafts, conservative browser adapters, original PDF/OFD/XML download and file hashing, monthly CSV exports. Historical order collection supports Taobao's all-orders filter and JD's annual filters, with coverage records for pagination. Detail inspection checks the exact order identity before reading. Real application and invoice-download forms still require verification. Fixture tests do not prove live compatibility. Merchant messages are not sent automatically. OCR, tax-authority invoice verification, invoice-number deduplication, automatic reply interpretation, scheduled follow-ups and XLSX export are not implemented. Displayed invoice amounts originate from order records and remain unverified against invoice contents.

Development: `uv sync --locked --python 3.13`, `uv run pytest -q`; then `npm ci --ignore-scripts` and `npm run build` inside `web/`. See the [Chinese README](README.md) and [source references](docs/sources.md) for detailed usage and platform references.
