# Practice Publication Query Engine Handover

Date: 2026-06-09
Repo: `bci-query-engine`
Branch: `main`

## What Changed

Query Engine now supports PDF outputs for artifact executions.

The Practice Publication can call:

```json
{
  "client_key": "srp",
  "artifact_key": "srp-practice-publication",
  "behavior": "display",
  "output_formats": ["pdf"]
}
```

When `pdf` is requested, Query Engine renders one PDF per row returned by the artifact view. For the SRP Practice Publication, the SRP render payload is shaped so each row is one burst slice, including the all-facilities slice and individual facility slices.

PDF filenames use the render date, not the data latest date:

```text
<Artifact Subject> - <Slice Label> - <YYYY-MM-DD>.pdf
```

## Main Files Changed

- `app/engine.py`
  - Added Chromium-backed HTML-to-PDF rendering.
  - Added PDF output generation when `output_formats` includes `pdf`.
  - Stores generated PDF metadata in `log.artifact_outputs`.
  - Sends generated PDF outputs as attachments when the execution behavior is `deliver` and the artifact delivery mode allows email.

- `app/mailer.py`
  - Adds attachment support to the email-service request payload.
  - Reads generated files from disk, base64-encodes them, and sends them as `attachments`.

- `Dockerfile`
  - Installs Chromium and `fonts-liberation`.
  - Sets writable `HOME`, `XDG_CONFIG_HOME`, and `XDG_CACHE_HOME` paths for Chromium in the container.

- `README.md`
  - Documents `output_formats: ["pdf"]`.
  - Documents output location and `log.artifact_outputs`.

## Where PDFs Are Written

Default location inside the Query Engine container:

```text
/tmp/bci-query-engine/artifact-outputs/<client_key>/<artifact_key>/<run_id>/
```

This can be changed with:

```text
ARTIFACT_OUTPUT_DIR
```

The execution response also returns the generated output metadata.

## Metadata Table

Generated files are logged in:

```text
metadata.log.artifact_outputs
```

Important columns:

- `run_id`
- `artifact_key`
- `client_key`
- `output_format`
- `slice_key`
- `slice_label`
- `filename`
- `storage_path`
- `content_type`
- `file_size_bytes`
- `sha256`
- `status`

## How To Test Locally

After the SRP stack is running and the SRP Practice Publication artifact has been deployed into metadata:

```bash
curl -X POST http://127.0.0.1:18300/artifact-executions \
  -H 'Content-Type: application/json' \
  -d '{
    "client_key": "srp",
    "artifact_key": "srp-practice-publication",
    "behavior": "display",
    "output_formats": ["pdf"]
  }'
```

Then inspect the returned `outputs` array or query:

```sql
SELECT run_id, slice_label, filename, storage_path, file_size_bytes, status, created_at
FROM log.artifact_outputs
WHERE client_key = 'srp'
  AND artifact_key = 'srp-practice-publication'
ORDER BY created_at DESC;
```

## Notes For The Next Person

- Rebuild/redeploy Query Engine locally before testing PDF export, because the container needs Chromium.
- The SRP repo owns the Practice Publication SQL views, template, artifact JSON, and deployment script.
- Query Engine owns the generic artifact execution, PDF rendering, output logging, and attachment handoff to email-service.
- If PDF generation fails, first check that Chromium exists in the Query Engine container and that `ARTIFACT_OUTPUT_DIR` is writable.
