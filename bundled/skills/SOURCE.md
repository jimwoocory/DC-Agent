# Bundled Agent Skills

These skills are bundled for DC-Agent and synced into the runtime
`data/skills/` directory by `scripts/sync_bundled_skills.py`.

Imported source: https://github.com/kepano/obsidian-skills
Imported commit: `a1dc48e68138490d522c04cbf5822214c6eb1202`
Imported date: 2026-06-13

Imported skills:

- `obsidian-markdown`
- `obsidian-bases`
- `json-canvas`

Adapted visual authoring skills:

- `mermaid-visualizer` from https://github.com/axtonliu/axton-obsidian-visual-skills
  - Adapted date: 2026-06-22
  - Scope: Mermaid authoring guidance only; no execution-oriented tooling.
- `json-canvas` layout guidance adapted from
  https://github.com/axtonliu/axton-obsidian-visual-skills
  - Adapted date: 2026-06-22
  - Scope: content-to-canvas layout patterns only; existing JSON Canvas schema
    guidance remains from the imported `json-canvas` skill.

License: MIT. Each imported skill directory includes the upstream `LICENSE`.

Deferred skills:

- `obsidian-cli`
- `defuddle`

The deferred skills are execution-oriented and should not be bundled until
DC-Agent has an explicit execution policy for the required local CLIs.
