# Synthetic uv cache transcripts

The uv parser fixtures are synthetic. The `cache size` contract was checked against uv 0.11.6 on
2026-07-11: stdout is a bare integer, but upstream counts while ignoring filesystem errors, so the
normalized confidence remains `ESTIMATE`. No `clean`, `prune`, or `--force` command is captured.
