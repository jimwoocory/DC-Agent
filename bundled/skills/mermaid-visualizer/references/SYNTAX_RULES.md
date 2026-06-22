# Mermaid Syntax Rules

Use these rules when generating or troubleshooting Mermaid diagrams for Obsidian and GitHub.

## Critical Error Prevention

### Ordered List Conflicts

Mermaid can parse `number. space` inside node labels as Markdown ordered-list syntax.

Avoid:

```mermaid
graph TB
    bad["1. Intake"]
```

Use one of these instead:

```mermaid
graph TB
    step_one["1.Intake"]
    step_two["(2) Classify"]
    step_three["Step 3: Review"]
```

### Subgraph Names

Subgraphs with spaces need an ID plus display label.

```mermaid
graph TB
    subgraph governance["Governance Loop"]
        export["Export task"] --> review["Human review"]
    end
```

Reference the ID, not the display name:

```mermaid
graph TB
    start["Start"] --> governance
```

### Node References

Define readable labels inside node declarations, then reference IDs.

```mermaid
graph TB
    source["Source document"]
    memory["Approved memory"]
    source --> memory
```

Do not write edges between display text labels.

## Node Syntax

```mermaid
graph TB
    rectangle["Rectangle"]
    rounded("Rounded")
    stadium(["Stadium"])
    decision{"Decision?"}
    database[("Database")]
```

Keep label text short. Use separate annotation nodes when details do not fit.

## Arrows

```mermaid
graph TB
    A --> B
    B -.-> C
    C ==>|Important| D
    D -->|Labeled transition| E
```

Use dashed arrows for optional, supporting, or feedback paths. Use thick arrows sparingly for the main emphasized path.

## Styling

```mermaid
graph TB
    start["Start"] --> result["Result"]
    style start fill:#d3f9d8,stroke:#2f9e44,stroke-width:2px
    style result fill:#c5f6fa,stroke:#0c8599,stroke-width:2px
```

Prefer hex colors and consistent category meanings across the diagram.

## Validation Checklist

- Direction is declared after `graph`: `TB`, `LR`, `BT`, or `RL`.
- No edge points to a display label.
- Every node ID is unique.
- Every edge endpoint is defined.
- Every subgraph with spaces uses `subgraph id["Display name"]`.
- Labels avoid unescaped quotes, raw parentheses in fragile contexts, and long paragraphs.
- The diagram is readable without relying on Mermaid Live Editor-only features.
