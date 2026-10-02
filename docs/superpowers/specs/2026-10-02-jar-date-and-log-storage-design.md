# JAR Build Date Display & Log Storage Design

Date: 2026-10-02
Project: k8s-log-viewer

---

## Overview

Two new features for the Horizon K8s Log Viewer:

1. **JAR Build Date** — display the `/app/app.jar` file timestamp from inside a running pod in both the pod list sidebar and the log pane header.
2. **Log Storage** — auto-save streamed logs to the viewer server's local disk when a stream ends; expose a saved-logs panel in the sidebar with per-file download.

---

## Feature 1: JAR Build Date

### Approach

Lazy fetch — the JAR date is retrieved only when a pod is selected (clicked). This avoids N concurrent `exec` calls on every pod list refresh.

### Backend

**New endpoint:** `GET /api/jar-date`

Query params: `context`, `namespace`, `pod`, `container` (all required; auth via existing `_verify` dependency).

Implementation:
- Uses `kubernetes.stream.stream(v1.connect_get_namespaced_pod_exec, ...)` to run `ls -la /app/app.jar` inside the pod.
- Parses the output line: `-rw-rw-r-- 1 root root 185101464 Sep 30 10:56 /app/app.jar`
- Returns:
  ```json
  {"date": "Sep 30 10:56", "size_bytes": 185101464}
  ```
- On failure (file not found, pod not Java-based, exec error): returns `{"date": null, "error": "<reason>"}` — HTTP 200, not an error response, so the UI degrades gracefully.

### Frontend

**Pod card (sidebar):**
- Each pod card gets a `<span class="jar-date">` element appended below the pod name.
- When a pod is clicked (`selectPod`), the span shows `⟳` (fetching indicator).
- On API response: shows `JAR Sep 30 10:56` or nothing if `date` is null.

**Log pane header:**
- A second line is added below the existing `pod-label` span: `<span id="jar-label">`.
- Shows `JAR built: Sep 30 10:56  (185 MB)` when date is available.
- Cleared when a different pod is selected or when no date is returned.

---

## Feature 2: Log Storage

### Approach

Server-side accumulation — the WS worker thread collects all streamed lines into a local list. When the thread exits (stream complete, stopped, or error), it writes the accumulated lines to a file on disk. The client requires no changes to trigger saving.

### Backend

**Directory:** `./logs/` — created at server startup with `os.makedirs('./logs', exist_ok=True)`.

**File naming:** `{namespace}__{pod}__{container}__{iso_timestamp}.log`
(double underscores as separator to reduce collision with names containing single underscores)
Example: `horizon__api-7bc59cd669-wg8mw__api__2026-10-02T14-35-22.log`

**`_worker()` changes:**
- Maintains a `captured = []` list.
- Appends every line received from the k8s watch stream to `captured`.
- In a `finally` block: if `captured` is non-empty, writes the joined lines to the log file.

**New endpoint: `GET /api/saved-logs`** (auth required)
- Lists all files in `./logs/`.
- Returns array sorted newest-first:
  ```json
  {"files": [{"name": "...", "size_bytes": 12345, "modified": "2026-10-02T14:35:22"}]}
  ```

**New endpoint: `GET /api/saved-logs/download`** (auth required)
- Query param: `file=<filename>` (basename only — no path separators allowed).
- Validates: `os.path.basename(file) == file` and no `..` components; raises HTTP 400 otherwise.
- Serves file as `text/plain` download with `Content-Disposition: attachment`.

### Frontend

**New "Saved Logs" sidebar section** (below Options):
- Collapsible `<div class="sidebar-sec">` with heading "Saved Logs" and a `↺` refresh button.
- Scrollable list of file rows: filename (truncated), human-readable size, date, and a `↓` download button.
- On download click: opens `GET /api/saved-logs/download?file=<name>&token=<authToken>` in a new tab.
- Auto-reloads the saved-logs list when WS status transitions to `done`, `stopped`, or `error`.
- Manual `↺` refresh button always available.

---

## Data Flow

```
User clicks pod
  → frontend calls /api/jar-date
  → backend execs ls -la /app/app.jar in pod
  → returns {date, size_bytes}
  → sidebar badge + header label updated

User clicks Connect
  → WS /ws/logs opens
  → _worker thread starts, captured=[]
  → lines stream to client, each appended to captured
  → stream ends (pod stops / user clicks Stop)
  → _worker finally: write ./logs/<name>.log
  → WS closes
  → frontend status → done/stopped
  → frontend calls /api/saved-logs
  → saved-logs panel refreshes
```

---

## Error Handling

- `/api/jar-date`: all exec errors caught, returned as `{"date": null, "error": "..."}` — UI shows nothing, no alert.
- Log save failure (disk full, permission error): logged server-side, stream still completes normally for the client.
- `/api/saved-logs/download` with invalid filename: HTTP 400.
- Saved-logs list fetch failure: UI shows inline error in the panel, does not affect log streaming.

---

## Out of Scope

- Deleting saved log files via the UI (can be done manually on the server).
- Log rotation / size limits on `./logs/` (operator responsibility).
- Fetching JAR date eagerly for all pods on list load.
- Storing logs in a Kubernetes resource (ConfigMap, PVC).
