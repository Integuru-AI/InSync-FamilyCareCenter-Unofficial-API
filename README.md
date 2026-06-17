# InSync FamilyCareCenter Unofficial API

Unofficial Python integrations for InSync FamilyCareCenter.

## Integrations

- `insync_familycarecenter_download_claim_by_id.py` - `download_claim_by_id`.
- `insync_familycarecenter_list_claim_ids.py` - `list_claim_ids`.
- `insync_familycarecenter_list_saved_queries.py` - `list_saved_queries`.

## Usage

Each file exposes a `run(input, context)` entrypoint. The runtime is expected to provide:

- `input`: integration-specific request fields.
- `context["headers"]`: authenticated request headers when required.
- `context["base_url"]`: the platform base URL when overriding the default.

Install dependencies:

```bash
pip install -r requirements.txt
```

## Info

This unofficial API is built by [Integuru](https://integuru.com).

For custom requests or hosted authentication, contact richard@integuru.com or [schedule time with us](https://calendly.com/d/cqb8-d9x-nbf/integuru).

See the [complete list of APIs by Integuru](https://github.com/Integuru-AI/APIs-by-Integuru).
