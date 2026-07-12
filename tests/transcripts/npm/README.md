# Synthetic npm cache transcripts

These tests use synthetic npm cache keys and roots. The command contract targets direct
`node.exe npm-cli.js` installations for npm 8–11, with logging/update/audit/network behavior
disabled. `.cmd`, nvm, and Corepack shims are never invoked. Checked against official npm cache
documentation and npm 9.8.1 smoke behavior on 2026-07-11.
