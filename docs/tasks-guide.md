---
title: Background Tasks and Feature Flags
description: "Guide to fastware background tasks, feature flags, and TOML config: BackgroundTask protocol, TaskRegistry, feature gating, and Pydantic validation."
date: 2026-07-29
---

# Background Tasks and Feature Flags

fastware provides three complementary systems for application lifecycle management: a background task registry for long-running services, boolean feature flags with per-machine overrides, and a TOML configuration loader with optional Pydantic validation. Together, they let you define tasks that start and stop with the app, gate them behind feature flags, and drive both from a validated config file.

## Background task system

### The BackgroundTask protocol

Any class that implements `start()` and `stop()` methods satisfies the `BackgroundTask` protocol. Both methods take no arguments and return `None`. The protocol is runtime-checkable via `isinstance()`, so the `TaskRegistry` can verify compliance at registration time and raise a `TypeError` immediately if a factory returns a non-compliant object rather than failing silently at start:

```python
from fastware.tasks import BackgroundTask

class MetricsCollector:
    def __init__(self):
        self._running = False

    def start(self) -> None:
        self._running = True
        # Begin collecting metrics

    def stop(self) -> None:
        self._running = False
        # Flush remaining metrics and release resources
```

Both methods take no arguments and return `None`. The `start()` method initializes the task's resources and begins its work. The `stop()` method tears everything down. Cleanup in `stop()` should be graceful -- flush buffers, close connections, release file handles.

### Registering tasks with factories

Tasks are registered as zero-argument factory functions (callables returning a `BackgroundTask` instance). The registry does not instantiate tasks at registration time -- it stores the factory and calls it later during `start_all()`:

```python
from fastware.tasks import TaskRegistry

registry = TaskRegistry()

def create_metrics_collector():
    return MetricsCollector()

registry.register("metrics", create_metrics_collector)
```

Each task must have a unique name. Registering a second task with the same name raises `ValueError`:

```python
registry.register("metrics", create_metrics_collector)
# ValueError: Background task 'metrics' is already registered
```

### Starting and stopping tasks

Call `start_all()` to instantiate every registered task factory and invoke `start()` on each result. Call `stop_all()` to invoke `stop()` on all running tasks in reverse registration order and clear the running set. Both methods handle exceptions gracefully: if one task fails to start or stop, the error is logged and the remaining tasks continue processing normally:

```python
# During app startup
registry.start_all()

# During app shutdown
registry.stop_all()
```

If a task's factory or `start()` method raises an exception, the error is logged and the remaining tasks continue starting. A failing task does not block the rest of the registry. Similarly, if `stop()` raises, the error is logged and the remaining tasks are still stopped.

### Feature-gated tasks

Tasks can be gated behind a feature flag by passing the `feature` keyword argument at registration. When `start_all()` is called with a `FeatureFlags` instance, gated tasks whose flag is disabled are silently skipped:

```python
from fastware.features import FeatureFlags
from fastware.tasks import TaskRegistry

registry = TaskRegistry()
registry.register("metrics", create_metrics_collector, feature="enable_metrics")
registry.register("health_check", create_health_checker)

flags = FeatureFlags(defaults={"enable_metrics": False})

# Only health_check starts; metrics is skipped because enable_metrics is False
registry.start_all(features=flags)
```

If `start_all()` is called without a `FeatureFlags` instance (or with `None`), all feature-gated tasks are skipped -- only ungated tasks start. This is a safe default: gated tasks require explicit opt-in.

### Inspecting the registry

The `list_tasks()` method returns a list of dictionaries describing all registered tasks, including each task's name, whether it is currently running, and the feature flag it is gated behind (if any). Use `get_task()` to retrieve a specific running task instance by name for direct interaction, or `None` if the task is not currently running:

```python
for task in registry.list_tasks():
    print(f"{task['name']}: running={task['running']}, feature={task['feature']}")
```

To retrieve a running task instance by name (for example, to query its state), use `get_task()`:

```python
collector = registry.get_task("metrics")
if collector is not None:
    # Task is running, interact with it
    pass
```

`get_task()` returns `None` if the task is not currently running (either it was never started, it was gated out, or it has been stopped).

### Lifecycle integration with create_app

In a typical fastware application, you wire the task registry into the app's async lifespan context so tasks start when the server starts and stop when the server shuts down:

```python
from contextlib import asynccontextmanager
from fastware import Router, create_app, serve
from fastware.tasks import TaskRegistry
from fastware.features import FeatureFlags

registry = TaskRegistry()
registry.register("metrics", create_metrics_collector, feature="enable_metrics")

flags = FeatureFlags(
    defaults={"enable_metrics": True},
    overrides_path="config/features.json",
)

@asynccontextmanager
async def lifespan(app):
    registry.start_all(features=flags)
    try:
        yield {"registry": registry, "flags": flags}
    finally:
        registry.stop_all()

router = Router()

@router.get("/tasks")
async def list_tasks(request):
    return registry.list_tasks()

app = create_app(router, lifespan=lifespan)
```

The lifespan context manager starts all tasks on entry and stops them on exit. Yielding the registry and flags into the lifespan state makes them available to handlers through `request.state`.

## Feature flags

### Defining flags with defaults

Create a `FeatureFlags` instance with a `defaults` dictionary mapping flag names to their default boolean values. Every flag starts at its default unless overridden by a JSON overrides file or a runtime `set_override()` call. Unknown flags (names not in the defaults dict and not overridden) return `False` from `enabled()`, so new flags can be checked before they are formally defined:

```python
from fastware.features import FeatureFlags

flags = FeatureFlags(defaults={
    "enable_metrics": True,
    "dark_mode": False,
    "experimental_search": False,
})
```

### Checking flags at runtime

Use `enabled()` to check whether a flag is active. The method returns the override value if one exists, otherwise the default. Unknown flag names (not in defaults and not overridden) return `False`:

```python
if flags.enabled("dark_mode"):
    theme = "dark"
else:
    theme = "light"
```

To get the effective state of every known flag at once, use `all_flags()`:

```python
flags.all_flags()
# {"enable_metrics": True, "dark_mode": False, "experimental_search": False}
```

### JSON overrides

Overrides are loaded from a JSON file whose path is provided at construction. This lets you toggle flags per machine, per environment, or per deployment without changing code or redeploying:

```python
flags = FeatureFlags(
    defaults={"dark_mode": False, "experimental_search": False},
    overrides_path="/etc/myapp/features.json",
)
```

The JSON file is a flat object mapping flag names to booleans:

```json
{
  "dark_mode": true,
  "experimental_search": true
}
```

Override values take precedence over defaults. If the file does not exist or is malformed (invalid JSON, wrong structure), all flags use their defaults -- the application starts normally rather than crashing on a missing config file.

### Setting overrides at runtime

Use `set_override()` to toggle a flag in memory and persist the change to the JSON overrides file atomically via `LockedFileWriter`. The override takes effect immediately for all subsequent `enabled()` calls. This is useful for admin endpoints, feature toggle dashboards, gradual rollouts, and automated operations that need to gate features without restarting the application:

```python
flags.set_override("experimental_search", True)
```

The override is applied in memory immediately and written to the overrides file. File writes are serialized through a `LockedFileWriter` to prevent corruption from concurrent calls.

`set_override()` requires an `overrides_path` to have been configured at construction. Calling it without a path raises `RuntimeError`:

```python
flags = FeatureFlags(defaults={"x": False})  # no overrides_path
flags.set_override("x", True)
# RuntimeError: Cannot set override: no overrides_path configured
```

### Hot reload

Call `reload()` to re-read the overrides file from disk. This picks up changes made by external tools (another process, a deployment script, a config management system) without restarting the application:

```python
flags.reload()
```

All flag access is thread-safe -- `enabled()`, `set_override()`, and `reload()` are guarded by a threading lock, so concurrent handler threads cannot observe partially-updated state.

### Admin endpoint example

A common pattern is to expose feature flags through an admin API so operators can inspect current flag state via a GET endpoint, toggle individual flags via a POST endpoint, and force a reload from disk to pick up changes made by external tools such as deployment scripts or configuration management systems:

```python
from fastware import Router, HTTPError
from fastware.features import FeatureFlags

flags = FeatureFlags(
    defaults={
        "enable_metrics": True,
        "dark_mode": False,
    },
    overrides_path="config/features.json",
)

router = Router()

@router.get("/admin/flags")
async def get_flags(request):
    return flags.all_flags()

@router.post("/admin/flags/{name}")
async def toggle_flag(request, name: str):
    body = request.json
    if "enabled" not in body:
        raise HTTPError(400, "Missing 'enabled' field")
    flags.set_override(name, bool(body["enabled"]))
    return {"name": name, "enabled": flags.enabled(name)}

@router.post("/admin/flags/reload")
async def reload_flags(request):
    flags.reload()
    return flags.all_flags()
```

## Configuration

### Loading TOML config

The `load_config()` function reads a TOML file from the given path and returns the parsed data. Without a `schema` argument it returns a plain nested dictionary matching the TOML structure. With a Pydantic model class passed as `schema`, it validates the data and returns a typed model instance with attribute access:

```python
from fastware.config import load_config

# Returns a plain dict
config = load_config("config/app.toml")
```

Given a TOML file like:

```toml
[server]
host = "0.0.0.0"
port = 8000

[features]
enable_metrics = true
dark_mode = false
```

`load_config()` returns a nested dictionary matching the TOML structure:

```python
{
    "server": {"host": "0.0.0.0", "port": 8000},
    "features": {"enable_metrics": True, "dark_mode": False},
}
```

### Pydantic schema validation

Pass a Pydantic `BaseModel` subclass as the `schema` argument to validate the TOML data against the model's type annotations and constraints, returning a typed model instance with attribute access. Invalid data raises `pydantic.ValidationError` with detailed per-field error messages showing exactly which fields failed and why. Requires the `[pydantic]` extra to be installed:

```python
from pydantic import BaseModel
from fastware.config import load_config

class ServerConfig(BaseModel):
    host: str
    port: int

class FeaturesConfig(BaseModel):
    enable_metrics: bool = True
    dark_mode: bool = False

class AppConfig(BaseModel):
    server: ServerConfig
    features: FeaturesConfig

config = load_config("config/app.toml", schema=AppConfig)
# config is an AppConfig instance with validated, typed fields
print(config.server.port)  # 8000
print(config.features.dark_mode)  # False
```

Schema validation requires the `[pydantic]` extra (`pip install fastware[pydantic]`). Without Pydantic installed, omit the `schema` argument and work with raw dictionaries.

### Error handling

`load_config()` raises specific exceptions for each failure mode, all at startup time so misconfigurations are caught immediately rather than at request time. The three possible exceptions are `FileNotFoundError` for a missing TOML file, `tomllib.TOMLDecodeError` for invalid TOML syntax, and `pydantic.ValidationError` for data that does not match the provided schema:

- `FileNotFoundError` -- the TOML file does not exist
- `tomllib.TOMLDecodeError` -- the file is not valid TOML
- `pydantic.ValidationError` -- the data does not match the schema (only when `schema` is provided)

All three are raised at startup, so misconfigurations are caught immediately rather than at request time.

### Wiring config into the app

A common pattern is to load and validate the TOML config at module level, construct `FeatureFlags` and `TaskRegistry` instances from the validated config, and pass everything into the async lifespan context so handlers can access config, flags, and registry via `request.state` throughout the application lifecycle:

```python
from contextlib import asynccontextmanager
from pydantic import BaseModel
from fastware import Router, create_app, serve
from fastware.config import load_config
from fastware.features import FeatureFlags
from fastware.tasks import TaskRegistry

class AppConfig(BaseModel):
    class Server(BaseModel):
        host: str
        port: int

    class Features(BaseModel):
        enable_metrics: bool = True
        experimental_search: bool = False

    server: Server
    features: Features
    overrides_path: str = "config/features.json"

config = load_config("config/app.toml", schema=AppConfig)

flags = FeatureFlags(
    defaults=config.features.model_dump(),
    overrides_path=config.overrides_path,
)

registry = TaskRegistry()
registry.register("metrics", create_metrics_collector, feature="enable_metrics")

@asynccontextmanager
async def lifespan(app):
    registry.start_all(features=flags)
    try:
        yield {"config": config, "flags": flags, "registry": registry}
    finally:
        registry.stop_all()

router = Router()
app = create_app(router, lifespan=lifespan)

if __name__ == "__main__":
    serve(app, foreground=True, host=config.server.host, port=config.server.port)
```

This gives you a single validated config file driving the server, feature flags, and background tasks with typed access throughout the application.

## API reference

### BackgroundTask and TaskRegistry

Background task registry with feature-gated lifecycle management. Provides the `BackgroundTask` runtime-checkable protocol with `start()` and `stop()` methods, and the `TaskRegistry` class for registering factory functions, starting and stopping tasks, and querying task state.

:-: ref path="src.fastware.tasks"

### FeatureFlags

Boolean feature flags with per-machine JSON overrides, providing `enabled()` checks for conditional logic, `set_override()` for runtime toggling with atomic file persistence, `reload()` for picking up external changes, and `all_flags()` for snapshot inspection.

:-: ref path="src.fastware.features"

### load_config

Standalone TOML configuration loader with optional Pydantic-schema validation. Reads a TOML file from disk and returns either a plain nested dictionary or a validated Pydantic model instance, raising clear exceptions for missing files, invalid syntax, or schema violations.

:-: ref path="src.fastware.config"
