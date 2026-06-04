# DC Router

The router is the single decision surface for DC-Agent model and workflow routing.

Run the focused router tests with:

```bash
uv run pytest tests/router -q
```

For coverage on the router package:

```bash
uv run pytest tests/router -q --cov=router
```

The key entrypoint is `router.entrypoint.DCRouter.decide()`. It switches between:

- business routing for normal employee-facing platforms
- ops routing when `MessageEnvelope.metadata["platform_id"]` is `巅池-技术（DevOps）` or `巅池-技术`

Rule ordering is intentional. Strong safety or cost signals such as public opinion,
deep tasks, quota state, and queue state must stay ahead of broader creative,
realtime, or code keywords.
