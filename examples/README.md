# Examples

## `sample_adp.csv`

The shape a custom ADP file needs. Required columns: `name` and `adp`. `position` is
optional but helps disambiguate.

```
configure_league(name="home", teams=10, scoring="ppr",
                 adp_csv_path="/path/to/my_league_adp.csv")
```

**Use your own league's export where you can.** Consensus is a reasonable default, but
ADP is league- and format-specific, and the opportunity-cost engine is only as good as
its estimate of when players actually go. Most platforms export this from their draft
tools.

Names run through the full alias resolver, so exports don't need to match nflverse
spelling. Check with `resolve_names` if you're unsure.

## `draft_day.md`

A full session from setup through the late rounds, with the kind of answers to expect.

## `session_snippets.md`

Short, copy-pasteable prompts for common situations.
