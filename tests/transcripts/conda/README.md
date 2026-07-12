# Synthetic Conda clean dry-run JSON

These four minimal fixtures reproduce the field nesting in conda 26.1–26.5 official
`main_clean.py`. They are fabricated and were never produced by running `conda clean`. Source and
official tests were reviewed 2026-07-11. Reclaimer additionally enforces types, containment,
warning emptiness, and total-size equality that upstream tests do not currently assert.
