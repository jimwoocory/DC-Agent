# JSON Canvas Layout Patterns

Use these patterns when turning prose, outlines, governed memory, SOPs, or
reports into an Obsidian `.canvas` file. The goal is a readable first draft that
opens cleanly in Obsidian and gives a human reviewer obvious places to inspect.

## General Placement Rules

- Use integer coordinates and align to a 20px grid.
- Treat `x` and `y` as the top-left corner, not the center.
- Keep at least 80px of visible whitespace between bounding boxes.
- For generated diagrams, use wider default spacing: 320px columns and 200px
  rows before adding node width and height.
- Check overlap with bounding boxes:
  - horizontal overlap exists when `a.x < b.x + b.width` and
    `b.x < a.x + a.width`
  - vertical overlap exists when `a.y < b.y + b.height` and
    `b.y < a.y + a.height`
  - two nodes overlap only when both are true
- Put group nodes before contained nodes in the `nodes` array so they render
  behind child nodes.

## Mind Map Layout

Use for hierarchical content, brainstorming, topic exploration, and knowledge
maps.

1. Place the root at `(0, 0)` with a larger text node.
2. Split primary branches into left and right sides for balance.
3. Place first-level children around the root using generous vertical spacing.
4. Place second-level children near their parent, farther away from the root.
5. Use edges from root to primary branches and from each branch to its details.

Suggested coordinates:

| Level | Left side x | Right side x | Width x height |
|-------|-------------|--------------|----------------|
| Root | 0 | 0 | 360 x 180 |
| Primary | -520 | 520 | 300 x 140 |
| Secondary | -900 | 900 | 260 x 120 |
| Detail | -1240 | 1240 | 240 x 100 |

For vertical positioning, center the branch group around the root. If a side has
`n` primary nodes, use `y = (index - (n - 1) / 2) * 240`.

## Freeform Zone Layout

Use for non-hierarchical material: articles, audits, relationship networks,
customer/project maps, or mixed evidence and conclusions.

1. Identify 3-6 clusters.
2. Create a group node for each cluster with a label.
3. Place clusters in distinct zones:
   - left: sources, inputs, constraints
   - center: core analysis or main entities
   - right: outputs, decisions, actions
   - bottom: risks, open questions, review items
4. Place cross-group edges only where they explain a real dependency.
5. Use edge labels for relationship types such as `supports`, `blocks`,
   `requires review`, `derived from`, or `owner`.

## Flow Layout

Use for SOPs, routing, lifecycle stages, import/export processes, and governance
loops.

- Horizontal flow: use `x += 420` per stage and keep `y` stable.
- Vertical flow: use `y += 220` per stage and keep `x` stable.
- Branches should move away from the main line by at least 300px.
- Use `fromSide: "right"` / `toSide: "left"` for horizontal flows.
- Use `fromSide: "bottom"` / `toSide: "top"` for vertical flows.
- Give decision or review nodes a distinct color, usually preset `"3"` or `"6"`.

## Evidence Map Layout

Use when connecting source material to conclusions, governed memories, or
review decisions.

1. Put raw source or file nodes on the left.
2. Put extracted facts, claims, or entities in the center.
3. Put approved conclusions, decisions, or action nodes on the right.
4. Put unresolved or human-review nodes below the center line.
5. Label edges with provenance words such as `cites`, `supports`, `conflicts`,
   `promotes`, or `needs review`.

Suggested columns:

| Column | x | Typical node type |
|--------|---|-------------------|
| Source | -760 | `file`, `link`, source text |
| Extracted fact | -260 | text |
| Governed memory | 260 | text |
| Decision/action | 760 | text |

## Group Bounds

When creating a group around child nodes:

1. Find the minimum child `x` and `y`.
2. Find the maximum `x + width` and `y + height`.
3. Add 40-60px padding on each side.
4. Set the group `x`, `y`, `width`, and `height` to the padded bounds.
5. Ensure the group label is short and meaningful.

## Node Text Rules

- Prefer headings plus 1-3 bullets for text nodes.
- Use `\n` for line breaks in JSON strings.
- Avoid raw long paragraphs; split them into sibling nodes.
- Avoid emoji in generated canvas text. Use color presets for visual distinction.
- For quotes in JSON strings, escape English double quotes as `\"` or rewrite
  them as plain text.
