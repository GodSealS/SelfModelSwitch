# AGX Thor Model Scheduler

A resource-aware Python scheduler in front of llama-swap.

## Architecture

Client -> ModelScheduler -> llama-swap -> Docker model servers

The scheduler decides *when* a model may be resident. llama-swap remains responsible
for model routing, Docker lifecycle, health checks, and proxying.

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Start llama-swap separately, using its own config.

Then:

```bash
python run.py
```

The scheduler API is:

- `GET /health`
- `GET /api/status`
- `GET /api/models`
- `POST /api/models/{model_id}/unload`
- `POST /v1/chat/completions`

Point your applications to:

`http://127.0.0.1:8090/v1`

instead of directly to llama-swap.

## Important llama-swap configuration

Because this scheduler performs resource-based eviction itself, do NOT configure
llama-swap to automatically swap models out according to an exclusive group.

All models that this scheduler manages should be allowed to coexist. A minimal
pattern is:

```yaml
routing:
  router:
    use: group
    settings:
      groups:
        all-managed:
          swap: false
          exclusive: false
          members:
            - embedding
            - reranker
            - qwen-small
            - qwen-large
```

You can still use model TTL carefully, but it is usually better to let this scheduler
own eviction so there is only one policy.

## Warm-up

Current llama-swap exposes `/running` and targeted unload APIs. It does not require a
separate load API in the standard flow: dispatching a request to a model causes it to
be loaded. For llama.cpp-backed models, this project uses:

`GET /props?model=<id>`

as a token-free dispatch/warm-up operation.

If you add a non-llama.cpp backend, implement a backend-specific warm-up method in
`llama_swap_client.py`.

## Resource model

The scheduler uses a reserved-memory estimate per model plus a safety margin.

Example:

```yaml
models:
  qwen-small:
    memory:
      reserved_bytes: 10737418240
```

For AGX Thor, use unified/system memory as the primary resource. The resource monitor
tries tegrastats first, then nvidia-smi, then psutil.

Do not treat GGUF file size as runtime memory. Measure actual model usage and set
`reserved_bytes` conservatively.

## Production notes

1. Put the model files on local NVMe.
2. Run llama-swap and this scheduler on the host for the first deployment.
3. Do not expose either service directly to the LAN without authentication.
4. Add persistent heat metrics/database after the scheduling policy is validated.
5. Add request cancellation and per-model concurrency limits before exposing it to
multiple users.
