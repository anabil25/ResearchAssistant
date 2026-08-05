---
name: screening-protocol
description: How to run a screening pass — batching, conflict handling, and when to answer `unclear`.
---

# Screening protocol

Advertised by name and description only. The body below is loaded on demand, so
it costs nothing until a screening run actually needs it.

## Order of work

1. Read the criteria and the paper list from the request digest.
2. Call `screen_papers` in batches of 10–20 evidence ids. Batching is for your
   own tracking; the tool fans out internally either way.
3. Reconcile the returned decisions against the paper list. Anything without a
   decision goes in `unresolved`.

## Deciding

Apply exclusion criteria before inclusion criteria. A paper that trips any
exclusion criterion is excluded regardless of how well it fits inclusion.

Name exactly one criterion per decision — the one that settled it. If two
criteria would each settle it, cite the exclusion criterion.

## When to answer `unclear`

`unclear` is correct, not a failure, when:

- the abstract does not report the population, design, or outcome a criterion asks about;
- the paper is a protocol, editorial, or conference abstract with no results;
- the criterion needs full text and only an abstract was supplied.

Do not infer a study design from the title. Do not treat the absence of an
exclusion signal as evidence of inclusion.

## Conflicts

Re-screen a paper only when two passes disagree. Record the disagreement in
`conflicts` with both readings and the criterion at issue. If re-screening does
not settle it, leave the paper `unclear` and say why.

## Reporting

`summary` states the counts, the criteria applied, and what the screen cannot
settle. It never claims a paper was assessed that carries no decision.
