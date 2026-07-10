# Explicit server engine parameter

## Current state

- `foreground=True` uses Granian's `MPServer` (multiprocess, blocking)
- `foreground=False` uses Granian's embed server (in-process, daemon thread)
- Both modes use the same Granian library, just different modes of operation

## Proposal

If we ever need a second ASGI server (e.g., uvicorn for specific use cases), add an explicit `engine` parameter:

```python
serve(target, foreground=True, engine="granian")
serve(target, foreground=True, engine="uvicorn")
```

The caller explicitly declares which server -- no implicit switching based on availability or fallback chains.

## Deferred

The current single-engine approach is clean. No second engine is needed today. This todo exists to document the design direction if the need arises, so the decision is not ad-hoc.
