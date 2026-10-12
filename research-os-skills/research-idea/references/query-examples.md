# paper2graph research queries

Read the live `get_graph_schema` response first and adapt these templates if the application contract differs. They illustrate task scope and provenance, not a second authoritative schema. Use `search_graph` with `cypher`, `parameters` and a bounded tool-level `limit`; omit `query` and the separate `task_root_id` input. Do not use `CALL` subqueries. The server supplies the outer row limit.

## Resolve tasks

Use when a task ID is unknown or ambiguous, not on every investigation:

```cypher
MATCH (task:TaskRoot)
RETURN task.id AS task_root_id, task.name AS name, task.definition AS definition
ORDER BY task.name
```

After identifying the task, `browse_task` provides its method overview. The task's stored `method_guidelines` can also be projected if its abstraction boundaries matter to the comparison.

## Survey methods actually adopted by a task's papers

Parameters: `task_root_id` is the resolved task ID.

```cypher
MATCH (paper:Paper)-[:FOR_TASK]->(:TaskRoot {id: $task_root_id})
MATCH (paper)-[:HAS_CLAIM]->(:Claim {kind: 'mechanism'})-[:ABOUT_METHOD]->(method:MethodLeaf)
RETURN method.id AS method_id, method.name AS name, method.mechanism AS mechanism,
       count(DISTINCT paper) AS paper_count
ORDER BY paper_count DESC, name
```

Frequency is the number of distinct imported papers associated with a mechanism, not the number of Claims or an importance score. Raw text mentions in Related Work do not establish adoption. Verify the relevant claims before making that assertion.

## Retrieve method-specific findings and supporting evidence

Parameters: `method_ids` from previous results and `kinds`, such as `["mechanism", "result", "limitation", "addresses"]`.

```cypher
MATCH (paper:Paper)-[:HAS_CLAIM]->(claim:Claim)-[:ABOUT_METHOD]->(method:MethodLeaf)
WHERE method.id IN $method_ids AND claim.kind IN $kinds
OPTIONAL MATCH (paper)-[:FOR_TASK]->(task:TaskRoot)
OPTIONAL MATCH (claim)-[:ABOUT_PROBLEM]->(problem:Problem)
OPTIONAL MATCH (claim)-[:ON_DATASET]->(dataset:Dataset)
OPTIONAL MATCH (paper)-[:HAS_EVIDENCE]->(evidence:Evidence)-[:SUPPORTS]->(claim)
RETURN paper.id AS paper_id, paper.title AS paper_title, task.id AS task_root_id,
       method.id AS method_id, claim.id AS claim_id, claim.kind AS kind,
       claim.text AS finding, problem.name AS problem, dataset.name AS dataset,
       evidence.id AS evidence_id, evidence.parse_version AS parse_version,
       evidence.chunk_no AS chunk_no, evidence.block_ids AS block_ids
ORDER BY paper_id, claim_id, evidence_id
```

This can return papers from other tasks that share the canonical method. Keep that provenance visible. Multiple evidence records can support one Claim; do not count them as independent findings. If optional matches produce repeated rows, deduplicate by Claim and Evidence IDs for analysis without dropping their associations.

## Inspect paper-level findings that were not assigned to a method

Parameters: `paper_ids` from relevant results and `kinds` to investigate.

```cypher
MATCH (paper:Paper)-[:HAS_CLAIM]->(claim:Claim)
WHERE paper.id IN $paper_ids AND claim.kind IN $kinds
OPTIONAL MATCH (claim)-[:ABOUT_METHOD]->(method:MethodLeaf)
OPTIONAL MATCH (claim)-[:ABOUT_PROBLEM]->(problem:Problem)
OPTIONAL MATCH (claim)-[:ON_DATASET]->(dataset:Dataset)
OPTIONAL MATCH (paper)-[:HAS_EVIDENCE]->(evidence:Evidence)-[:SUPPORTS]->(claim)
RETURN paper.id AS paper_id, paper.title AS paper_title, claim.id AS claim_id,
       claim.kind AS kind, claim.text AS finding, method.id AS method_id,
       problem.name AS problem, dataset.name AS dataset,
       evidence.id AS evidence_id, evidence.parse_version AS parse_version,
       evidence.chunk_no AS chunk_no, evidence.block_ids AS block_ids
ORDER BY paper_id, claim_id, evidence_id
```

A null method ID is a paper-level finding. Do not assign it to all methods in that paper; original context is required to establish a more specific association.

## Find candidate mechanisms in other imported tasks

Parameters: the target `task_root_id` and grounded `terms` describing the desired mechanism. This is lexical graph filtering, not vector similarity retrieval.

```cypher
MATCH (paper:Paper)-[:FOR_TASK]->(task:TaskRoot)
MATCH (paper)-[:HAS_CLAIM]->(:Claim {kind: 'mechanism'})-[:ABOUT_METHOD]->(method:MethodLeaf)
WHERE task.id <> $task_root_id
  AND any(term IN $terms WHERE
      toLower(method.name + ' ' + coalesce(method.mechanism, '')) CONTAINS toLower(term))
RETURN DISTINCT method.id AS method_id, method.name AS name,
       method.mechanism AS mechanism, paper.id AS paper_id,
       paper.title AS paper_title, task.id AS source_task_id
ORDER BY source_task_id, name
```

An empty result may simply mean the imported corpus has no other relevant tasks. Do not describe an unobserved cross-task transfer as an established result.

## Retrieve actual datasets and their evidence

Parameters: the selected `paper_ids`.

```cypher
MATCH (paper:Paper)-[:USES_DATASET]->(dataset:Dataset)
WHERE paper.id IN $paper_ids
OPTIONAL MATCH (paper)-[:HAS_EVIDENCE]->(evidence:Evidence)-[:SUPPORTS]->(dataset)
RETURN paper.id AS paper_id, paper.title AS paper_title,
       dataset.id AS dataset_id, dataset.name AS name, dataset.description AS description,
       evidence.id AS evidence_id, evidence.parse_version AS parse_version,
       evidence.chunk_no AS chunk_no, evidence.block_ids AS block_ids
ORDER BY paper_id, name, evidence_id
```

Do not infer paper adoption or evaluation compatibility from `Dataset.FOR_TASK` alone. Confirm splits, metrics and experimental settings in original context.

## Complete the source chain

- For an Evidence row, call `get_paper_context` with its `paper_id`, `parse_version`, `chunk_nos: [chunk_no]` and nonempty `block_ids` when available. If the Evidence locator is incomplete or missing, do not guess its values.
- To find additional context, call `search_paper_chunks` with grounded terms, optional `paper_ids` and a bounded `limit`. It has no `parse_version` input. Use each hit's returned paper ID, version and chunk number to fetch context.
- When limiting a search to a task, first retrieve that task's Paper IDs with `search_graph`. Passing `paper_ids: []` matches nothing, whereas omitting it searches the entire ES corpus. Do not silently remove an empty task filter.
- Search previews are discovery aids, not verified quotes or exact block locators. Read the complete returned chunk, then choose supporting blocks without losing their identifiers. Cite their returned `viewer_url` verbatim.
- `p3-b4` uses zero-based page index 3; returned `page_number` is physical PDF page 4. Neither is necessarily the printed page label. Do not construct bbox values, viewer URLs or page labels from those identifiers yourself.
- If reference lookup fails, keep the error/coverage gap visible. Do not substitute another parsing version or fabricated quote to make the idea appear supported.
