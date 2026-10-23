# Report catalogs

The JSON mappings in `models/models.json`, `tasks/tasks.json`, and
`harnesses/harnesses.json` supply optional presentation metadata. They do not
configure provider endpoints, credentials, installed tasks or runtime harnesses.

The public model catalog is intentionally empty: previous entries described a
particular private deployment and cannot serve as a portable model registry.
Unknown IDs fall back to their archive names. Add only public, accurate model
metadata keyed by the model IDs in your approved results, for example:

```json
{
  "example-model": {
    "name": "Example Model",
    "provider": "Example Provider",
    "description": "A deployment-independent display description."
  }
}
```

No logo image files are bundled. Reports work without logos. If you supply your
own appropriately licensed image, an optional `logo` field is relative to the
published static directory (for example `logos/your-model.svg`).
The task catalog retains neutral scientific/smoke-test
descriptions, and the harness catalog retains generic harness descriptions.
Adapt them to your installed benchmark IDs. Neither catalog promises that any
task, model or harness is installed. Do not include internal hostnames, local
paths, approval classifications, secrets or claims about private infrastructure.
