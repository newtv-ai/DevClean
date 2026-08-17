# Docker storage source notes

Primary Docker documentation reviewed for this audit:

- Docker Desktop WSL 2 backend: default Windows data location is `%LOCALAPPDATA%\Docker\wsl` and disk image location is managed by Docker Desktop settings.
- Docker Desktop settings: Windows persistent Desktop settings are stored under `%APPDATA%\Docker\settings-store.json`.
- Docker CLI configuration: client configuration defaults to `%USERPROFILE%\.docker` on Windows and can be redirected with `DOCKER_CONFIG`; `config.json` can reference credential stores and other client state.
- `docker system df --format json` is the read-only daemon disk-usage interface used for inventory.
- `docker builder prune` is the vendor-supported build-cache cleanup command and supports an `until` filter.
- `docker system prune` also removes stopped containers, unused networks, dangling/unused images and build cache; volumes require separate opt-in. DevClean does not expose that broad action in this profile.
- `docker volume prune` targets unused local volumes. Volumes are deliberately outside DevClean's generic cleanup authority because they can contain persistent user/application data even when no container currently references them.
