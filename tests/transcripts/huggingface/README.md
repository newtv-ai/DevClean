# Synthetic Hugging Face transcripts

These fixtures are synthetic and contain no user cache output. Shape A represents the
`huggingface_hub` 1.0–1.10 JSON family; Shape B represents the 1.11+ output migration verified
against 1.23.0 source behavior. Paths, repository IDs, hashes, sizes, and times are fabricated.

Source family: official `huggingface_hub` CLI documentation and tagged upstream source. Checked
2026-07-11. The fixture bytes themselves are the parser input; tests intentionally mutate them for
schema, containment, control-character, duplicate, and truncation failures.
