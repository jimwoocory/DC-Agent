# DC-Agent NAS Memory Archive Policy

## Primary Rule

Files are archived by project first, not by person.

Scheduled DC memory indexing is read-only by default. The cron watchdog invokes
`dc_memory_indexer.py --once --inbox-only --no-move`; moving files into
`projects/` must be an explicit operator action, or enabled with
`KNOWLEDGE_DC_MEMORY_MOVE_SUCCESS=1`.

```text
nas_kb/projects/
  <project_name>/
    <doc_type>/
      <original_file>
```

Low-confidence files go to:

```text
nas_kb/projects/_待确认/<project_name>/<doc_type>/<original_file>
```

## Index Views

People, departments, and document types are query/index dimensions, not primary
folder owners. One document can relate to multiple people and departments.

Core tables in `data/nas_memory.db`:

- `documents`: one row per indexed file.
- `projects`: normalized project candidates.
- `project_items`: row-level items extracted from project summary tables.
- `people`: people mentioned in indexed documents.
- `document_people`: document to person relations: `owner`, `initiator`, `participant`.
- `document_projects`: document to project relations.
- `review_queue`: low-confidence metadata that needs human confirmation.

## Confidence Rules

The indexer marks a file as `need_review` when any critical metadata is weak:

- no initiator found;
- no owner found;
- people are detected but cannot be mapped to a department;
- the file is a project summary table and requires row-level interpretation.

Manual corrections live in:

```text
data/config/nas_memory_overrides.json
```

## Useful Commands

```bash
.venv/bin/python nas_sync/dc_memory_indexer.py --once --inbox-only --no-move
.venv/bin/python nas_sync/dc_memory_indexer.py --query "五菱" --limit 5
.venv/bin/python nas_sync/dc_memory_indexer.py --project-query "" --owner "谭媛尹" --limit 10
.venv/bin/python nas_sync/dc_memory_indexer.py --review-list --limit 10
.venv/bin/python nas_sync/dc_memory_indexer.py --unknown-people --limit 20
.venv/bin/python nas_sync/dc_memory_indexer.py --set-person "韦欢" --set-department "策划" --set-role "舆情执行"
.venv/bin/python nas_sync/dc_memory_indexer.py --confirm-review "<review_id>" --set-owner "韦欢" --set-initiator "谭媛尹" --set-departments "策划" --set-doc-type "SOP"
```

`--confirm-review` will write the correction to
`data/config/nas_memory_overrides.json`, reindex the document when the file is
still present, and resolve the matching review queue item after metadata becomes
confirmed.
