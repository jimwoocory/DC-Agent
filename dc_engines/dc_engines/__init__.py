"""DC-Agent business engines.

Subpackages here are the "business logic" we own:

- ``dc_engines.harness``      — Harness task sidecar (lifecycle, memory, workflows)
- ``dc_engines.ai_inbox``     — Employee request intake and communication bridge
- ``dc_engines.company_cognition`` — Cross-store cognition health checks
- ``dc_engines.case``         — Case aggregation layer (planned port from W0 2A-0)
- ``dc_engines.group_summary``— W1 group summary skill (planned port)

The design rule: keep these OUT of ``astrbot/core/`` so AstrBot upstream upgrades
don't silently overwrite or merge-conflict on our code.
"""

__version__ = "0.1.0"
