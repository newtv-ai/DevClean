# Synthetic pip cache transcripts

Fixtures and inline test outputs use fabricated cache roots and wheel names. The parser contract is
based on the official pip cache CLI for pip 21 through 26 and is checked 2026-07-11. `cache info`
human units are evidence only; `cache list --format=abspath` covers locally built wheels only.
