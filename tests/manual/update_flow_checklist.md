# Manual E2E checklist -- update flow (Phase 12)

The Python side (endpoint serving, content substitution, SSE priming) is covered
by the automated suite. Browser-level behavior of the generated JavaScript can
only be verified in a real browser. Run this checklist against a real build
served by a fastware app before shipping changes to the browser assets.

Prerequisites: a fastware app with a hashed static build (`static_dir` +
`spa_fallback`), the page including both
`<script src="/__fastware/client.js"></script>` and (in cache mode)
`<script src="/__fastware/sw-register.js"></script>`.

## Blessed service worker -- cache mode (`sw_mode="cache"`)

- [ ] First load: DevTools > Application > Service Workers shows the worker
      registered at scope `/` (proves `Service-Worker-Allowed: /` took effect).
- [ ] Cache Storage contains a cache named `fastware-cache-<build_id>`.
- [ ] Hashed asset (`*-<hash>.js/css`) is served cache-first on reload (offline
      still serves it).
- [ ] A navigation / the shell is network-first (edit index.html on the server,
      reload -> new content appears without a hard refresh).
- [ ] `GET /api/...`, `/__fastware/events`, and WebSocket upgrades are NOT
      intercepted by the worker (Network tab shows them hitting the server).
- [ ] Rebuild the app with changed bytes and restart. Within one reload the new
      build activates; old `fastware-cache-*` caches are deleted; the page
      reloads exactly once (no reload loop).

## Foreign SW detection (cache mode)

- [ ] Register an undeclared worker at scope `/`. On next load, the console logs
      a `[fastware] FOREIGN service worker ...` error and the worker is
      unregistered.
- [ ] Add its path to `foreign_sw_paths`. Reload: no error, worker left alone.

## Self-destruct -- reset mode (`sw_mode="reset"`)

- [ ] Start from a state where a worker is registered at `/sw.js`.
- [ ] Switch the app to `sw_mode="reset"` and reload. The worker served at
      `/sw.js` (and `/__fastware/sw.js`) clears all caches, unregisters, and the
      window reloads once, ending controller-free.
- [ ] Confirm no service worker remains registered afterward.

## Off mode (`sw_mode="off"`)

- [ ] `/__fastware/sw.js` and `/__fastware/sw-register.js` return 404.
- [ ] An app-owned worker at `/sw.js` (served from static files) is reachable
      and untouched by the framework.

## Stale-client update client (`/__fastware/client.js`)

- [ ] Load the page, type into a form (text + checkbox), do NOT submit.
- [ ] Rebuild + restart the server (new build id).
- [ ] The page reloads exactly once; the form fields (except password) are
      restored from the pre-reload snapshot.
- [ ] `window.__fastwareStateProvider` / `window.__fastwareRestoreState`, when
      defined, are invoked to snapshot/rehydrate app state across the reload.
- [ ] No reload loop: after the single reload, the running build id matches the
      channel's and no further reload occurs.
