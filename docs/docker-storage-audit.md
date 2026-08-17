# Docker Desktop storage audit

DevClean treats Docker Desktop storage as mixed persistent state, not as a folder-shaped cache.

- Docker Desktop WSL data is inventory-only and never receives raw recursive deletion authority.
- Images, stopped containers, networks, volumes, and the Docker Desktop data disk are not deleted by the generic cleanup pipeline.
- Docker CLI configuration, contexts, certificates, credentials, and Docker Desktop settings are protected state.
- The only automated maintenance action added by this audit is old build-cache pruning through `docker builder prune`.
- The build-cache action refuses retention windows shorter than 24 hours and blocks while an active Docker/BuildKit build client is detected.
- No `docker system prune`, `docker image prune`, `docker container prune`, `docker network prune`, or `docker volume prune` action is exposed by this profile.
