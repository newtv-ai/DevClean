# Ollama per-model maintenance audit

Audited: 2026-08-18

## Product conclusion

Ollama's model store is downloaded user content, not a disposable cache. Its internal manifest/blob layout can share blobs between multiple model names, so DevClean must never infer per-model deletion from filenames or recursively delete the model store.

Ollama provides an official model-list API, running-model API, and model-delete API. This gives DevClean an exact vendor-owned maintenance lane:

- list models through `/api/tags`;
- detect currently loaded models through `/api/ps`;
- delete one exact user-selected model through `DELETE /api/delete`;
- verify postcondition by listing models again.

Every model remains **USER_REVIEW**. DevClean never chooses a model to delete based on age, size, name, family, quantization, or model popularity, and AI is not needed.

## Why the existing raw model store remains protected

The previous Ollama storage audit deliberately classified the complete model store as user-owned downloaded content. That decision remains correct.

A model can reference blobs also used by another model/tag. Deleting raw files under `blobs` or `manifests` would bypass Ollama's own reference/lifecycle semantics and can corrupt other installed models.

This maintenance lane does not weaken the generic scan rule. It adds a separate vendor action whose subject is a model identity returned by Ollama, not a filesystem child selected by DevClean.

## Local API boundary

Ollama's API is served locally by default at `http://localhost:11434/api`. The API documents:

- `GET /api/version` — Ollama version;
- `GET /api/tags` — local models, including name/digest/size/details;
- `GET /api/ps` — models currently loaded in memory;
- `DELETE /api/delete` — delete a model by exact model name.

DevClean deliberately refuses a non-loopback `OLLAMA_HOST`. Per-model cleanup is a local disk-maintenance feature, not remote model-server administration.

Wildcard local server binds such as `0.0.0.0:<port>` are mapped back to the corresponding loopback endpoint for DevClean's request. A LAN/remote hostname or address receives no deletion authority.

## Local model-store ownership boundary

Ollama supports moving the model store through `OLLAMA_MODELS`. A configured path can in principle point to another volume or shared location.

Vendor API identity alone does not prove that a store is owned exclusively by the current machine/user. DevClean therefore requires the effective model root from the existing source-audited Ollama profile to remain on local fixed storage before enabling deletion.

A shared, remote, removable, or reparse-redirected model store can still be inventoried through Ollama, but the delete button remains unavailable.

## Running-model guard

Before presenting inventory DevClean reads `/api/ps`. A model currently loaded in memory is marked as such.

Before each deletion DevClean refreshes the full inventory again. If the selected model is now loaded, deletion is refused. This avoids changing on-disk model state while the selected identity is actively being served.

The user can retry after the model has left memory.

## Identity revalidation

The UI retains both the exact model name and digest from the completed inventory.

Immediately before mutation DevClean:

1. refreshes `/api/version`, `/api/tags`, and `/api/ps`;
2. requires the exact selected model name to still exist;
3. requires its digest to match the digest shown at selection time;
4. requires the model to be unloaded;
5. rechecks that the model store is still local fixed storage;
6. invokes `DELETE /api/delete` with only that exact model name;
7. lists `/api/tags` again and requires the selected name to be absent before reporting success.

A model that changed digest between selection and execution is treated as a replaced object and must be reselected.

## Size/reclaimed-space semantics

`/api/tags` reports a model `size`, which DevClean displays as the model's logical size. It is **not** claimed as guaranteed reclaimed disk space because another model can share some of the same blobs.

When the local model-store directory can be read, DevClean measures the store size before and after the vendor deletion and reports the observed directory delta. It never derives blob reference counts itself and never deletes leftover blobs independently.

## Multi-select behavior

The UI can delete multiple explicitly selected models, but it performs them one at a time. Each model gets its own fresh digest/running-state revalidation and vendor delete call.

If one deletion fails or becomes unsafe, DevClean stops immediately. Earlier successful model removals are reported; later selections are not attempted automatically.

## Explicit non-targets

This lane does not delete or edit:

- raw `blobs` files;
- raw `manifests` files;
- the complete Ollama model store;
- Ollama server configuration;
- remote Ollama servers;
- loaded/running models;
- shared/remote/removable model stores;
- model files selected from generic DevClean scan output.

## Primary sources

- Ollama Docs, **CLI Reference**: local model listing and `ollama rm <model>` vendor semantics.
  - https://docs.ollama.com/cli
- Ollama API, **List models**: `/api/tags` model identities, digest, size, and details.
  - https://docs.ollama.com/api/tags
- Ollama API, **List running models**: `/api/ps` loaded-model state.
  - https://docs.ollama.com/api/ps
- Ollama API, **Delete a model**: `DELETE /api/delete` with a model name.
  - https://docs.ollama.com/api/delete
- Ollama API, **Version**: `/api/version`.
  - https://docs.ollama.com/api/version
