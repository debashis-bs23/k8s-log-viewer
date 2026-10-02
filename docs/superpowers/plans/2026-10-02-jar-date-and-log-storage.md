# JAR Build Date & Log Storage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add JAR build-date display (pod sidebar badge + log header) and automatic log-to-disk saving with a downloadable saved-logs sidebar panel to the Horizon K8s Log Viewer.

**Architecture:** JAR date is fetched lazily on pod click via a new `/api/jar-date` endpoint that execs `ls -la /app/app.jar` inside the pod using the kubernetes stream API. Log saving is server-side: the WS `_worker` thread accumulates streamed lines into a list and writes a `.log` file to `./logs/` on thread exit. Two new endpoints serve the file list and file downloads.

**Tech Stack:** Python 3.12, FastAPI 0.110, kubernetes-python 29.0.0, vanilla JS + CSS, pytest 8.x (dev only)

## Global Constraints

- No new Python runtime packages — kubernetes SDK already provides exec stream API (`kubernetes.stream`)
- All new endpoints use the existing `_verify` auth dependency (Bearer token)
- `/api/jar-date` returns HTTP 200 even on failure — error info in JSON body so UI degrades gracefully for non-Java pods
- Log files stored in `./logs/`; filename format: `{namespace}__{pod}__{container}__{YYYY-MM-DDTHH-MM-SS}.log` (double underscore separator)
- `/api/saved-logs/download` rejects any filename where `os.path.basename(file) != file` — returns HTTP 400
- pytest and httpx are dev dependencies only — do not add to `requirements.txt`

---

### Task 1: `/api/jar-date` Backend Endpoint

**Files:**
- Modify: `server.py` — add `kube_stream` import and `get_jar_date` endpoint
- Create: `tests/__init__.py`
- Create: `tests/test_jar_date.py`

**Interfaces:**
- Produces: `GET /api/jar-date?context=&namespace=&pod=&container=` → `{"date": "Sep 30 10:56", "size_bytes": 185101464}` or `{"date": null, "error": "..."}`
- Produces: module-level name `server.kube_stream` (patchable in tests)

- [ ] **Step 1: Install pytest and httpx (dev only)**

```bash
pip install pytest httpx
```

Expected: installs successfully, no changes to `requirements.txt`

- [ ] **Step 2: Create test files**

Create `tests/__init__.py` as an empty file.

Create `tests/test_jar_date.py`:

```python
# tests/test_jar_date.py
from unittest.mock import patch
from fastapi.testclient import TestClient

from server import app, _TOKEN

client = TestClient(app)
HEADERS = {"Authorization": f"Bearer {_TOKEN}"}
PARAMS = {
    "context": "test-ctx",
    "namespace": "horizon",
    "pod": "api-abc123",
    "container": "api",
}


def test_jar_date_success():
    ls_output = "-rw-rw-r-- 1 root root 185101464 Sep 30 10:56 /app/app.jar\n"
    with patch("server.make_v1"), patch("server.kube_stream", return_value=ls_output):
        r = client.get("/api/jar-date", params=PARAMS, headers=HEADERS)
    assert r.status_code == 200
    body = r.json()
    assert body["date"] == "Sep 30 10:56"
    assert body["size_bytes"] == 185101464


def test_jar_date_no_such_file():
    ls_output = "ls: /app/app.jar: No such file or directory\n"
    with patch("server.make_v1"), patch("server.kube_stream", return_value=ls_output):
        r = client.get("/api/jar-date", params=PARAMS, headers=HEADERS)
    assert r.status_code == 200
    body = r.json()
    assert body["date"] is None
    assert "error" in body


def test_jar_date_malformed_output():
    with patch("server.make_v1"), patch("server.kube_stream", return_value="weird\n"):
        r = client.get("/api/jar-date", params=PARAMS, headers=HEADERS)
    assert r.status_code == 200
    assert r.json()["date"] is None


def test_jar_date_exec_exception():
    with patch("server.make_v1"), \
         patch("server.kube_stream", side_effect=Exception("exec failed")):
        r = client.get("/api/jar-date", params=PARAMS, headers=HEADERS)
    assert r.status_code == 200
    assert r.json()["date"] is None


def test_jar_date_unauthorized():
    r = client.get("/api/jar-date", params=PARAMS)
    assert r.status_code == 401
```

- [ ] **Step 3: Run tests — verify they FAIL**

```bash
cd /home/bs01107/Desktop/BS23/HealthTech/k8s-log-viewer && pytest tests/test_jar_date.py -v
```

Expected: tests fail with 404 or missing route error — endpoint does not exist yet

- [ ] **Step 4: Add `kube_stream` import to `server.py`**

Find in `server.py`:
```python
from kubernetes import client, config, watch
from kubernetes.client.rest import ApiException
```

Replace with:
```python
from kubernetes import client, config, watch
from kubernetes.client.rest import ApiException
from kubernetes.stream import stream as kube_stream
```

- [ ] **Step 5: Add `get_jar_date` endpoint to `server.py`**

Find in `server.py`:
```python
@app.get("/api/logs/download")
```

Insert the following block immediately before that line:

```python
@app.get("/api/jar-date")
def get_jar_date(
    context: str = Query(...),
    namespace: str = Query(...),
    pod: str = Query(...),
    container: str = Query(...),
    _: None = Depends(_verify),
):
    v1 = make_v1(context)
    try:
        resp = kube_stream(
            v1.connect_get_namespaced_pod_exec,
            pod,
            namespace,
            command=["ls", "-la", "/app/app.jar"],
            container=container,
            stderr=True,
            stdin=False,
            stdout=True,
            tty=False,
        )
        line = (resp or "").strip()
        if not line or "No such file" in line:
            return {"date": None, "error": "not found"}
        parts = line.split()
        if len(parts) < 9:
            return {"date": None, "error": "unexpected output"}
        try:
            size_bytes = int(parts[4])
        except ValueError:
            return {"date": None, "error": "unexpected output"}
        date_str = f"{parts[5]} {parts[6]} {parts[7]}"
        return {"date": date_str, "size_bytes": size_bytes}
    except ApiException as exc:
        return {"date": None, "error": f"[K8s {exc.status}] {exc.reason}"}
    except Exception as exc:
        return {"date": None, "error": str(exc)}


```

- [ ] **Step 6: Run tests — verify they PASS**

```bash
pytest tests/test_jar_date.py -v
```

Expected: all 5 tests PASS

- [ ] **Step 7: Commit**

```bash
git add server.py tests/__init__.py tests/test_jar_date.py
git commit -m "feat: add /api/jar-date endpoint for JAR build timestamp"
```

---

### Task 2: JAR Date Frontend (Pod Badge + Log Header Label)

**Files:**
- Modify: `static/index.html` — add `#jar-label` span to log-meta
- Modify: `static/app.js` — add `fetchJarDate`, update `renderPods`, `selectPod`, `resetPodPanel`
- Modify: `static/style.css` — add `.pod-info`, `.jar-date`, `.jar-label` rules

**Interfaces:**
- Consumes: `GET /api/jar-date` (Task 1)
- Consumes: existing `apiFetch(path)`, `esc(s)`, `selectedCtx`, `selectedNs` from `app.js`

- [ ] **Step 1: Add `#jar-label` to `static/index.html`**

Find:
```html
      <div class="log-meta">
          <span id="pod-label" class="pod-label">No pod selected</span>
          <span id="status-badge" class="status-badge idle">● idle</span>
        </div>
```

Replace with:
```html
      <div class="log-meta">
          <span id="pod-label" class="pod-label">No pod selected</span>
          <span id="jar-label" class="jar-label"></span>
          <span id="status-badge" class="status-badge idle">● idle</span>
        </div>
```

- [ ] **Step 2: Update `renderPods` in `static/app.js` to include `.jar-date` span**

Find:
```javascript
    el.innerHTML  = `
      <div class="pod-dot ${dotCls}"></div>
      <div class="pod-name" title="${esc(pod.name)}">${esc(pod.name)}</div>
      <div class="pod-meta">${esc(pod.ready)}${restartHtml}</div>`;
```

Replace with:
```javascript
    el.innerHTML  = `
      <div class="pod-dot ${dotCls}"></div>
      <div class="pod-info">
        <div class="pod-name" title="${esc(pod.name)}">${esc(pod.name)}</div>
        <span class="jar-date"></span>
      </div>
      <div class="pod-meta">${esc(pod.ready)}${restartHtml}</div>`;
```

- [ ] **Step 3: Add `fetchJarDate` function to `static/app.js`**

Find:
```javascript
function resetPodPanel() {
```

Insert the following block immediately before that line:

```javascript
async function fetchJarDate(pod) {
  const jarLabel = $('jar-label');
  jarLabel.textContent = '';

  const podCard = $('pod-list').querySelector(`[data-pod="${CSS.escape(pod.name)}"]`);
  const jarSpan = podCard ? podCard.querySelector('.jar-date') : null;
  if (jarSpan) jarSpan.textContent = '⟳';

  try {
    const params = new URLSearchParams({
      context:   selectedCtx,
      namespace: selectedNs,
      pod:       pod.name,
      container: $('ctr-select').value,
    });
    const data = await apiFetch(`/api/jar-date?${params}`);
    if (data.date) {
      if (jarSpan) jarSpan.textContent = `JAR ${data.date}`;
      const sizePart = data.size_bytes
        ? ` (${(data.size_bytes / 1_048_576).toFixed(1)} MB)`
        : '';
      jarLabel.textContent = `JAR built: ${data.date}${sizePart}`;
    } else {
      if (jarSpan) jarSpan.textContent = '';
    }
  } catch {
    if (jarSpan) jarSpan.textContent = '';
  }
}

```

- [ ] **Step 4: Call `fetchJarDate` from `selectPod` and clear label in `resetPodPanel`**

In `selectPod`, find:
```javascript
  $('btn-connect').disabled = false;
  $('btn-dl').disabled      = false;
}
```

Replace with:
```javascript
  $('btn-connect').disabled = false;
  $('btn-dl').disabled      = false;

  fetchJarDate(pod);
}
```

In `resetPodPanel`, find:
```javascript
  $('pod-label').textContent = 'No pod selected';
```

Replace with:
```javascript
  $('pod-label').textContent = 'No pod selected';
  $('jar-label').textContent = '';
```

- [ ] **Step 5: Add CSS to `static/style.css`**

Append at the end of `style.css`:

```css
/* ── JAR date ── */
.pod-info {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
}

.pod-info .pod-name {
  flex: unset;
}

.jar-date {
  font-size: 10px;
  color: var(--tx2);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.jar-label {
  font-size: 11px;
  color: var(--tx2);
  white-space: nowrap;
  flex-shrink: 0;
}
```

- [ ] **Step 6: Manual verification**

```bash
python server.py
```

Open the browser → log in → select a namespace → click any pod.

Check:
- Pod card shows `⟳` briefly then `JAR Sep 30 10:56` (or blank for non-Java pods)
- Log toolbar shows `JAR built: Sep 30 10:56 (176.5 MB)` between the pod name and status badge
- Selecting a different pod clears the old label and fetches the new one
- `resetPodPanel` (triggered by namespace change) clears `jar-label`

- [ ] **Step 7: Commit**

```bash
git add static/index.html static/app.js static/style.css
git commit -m "feat: show JAR build date on pod card and log pane header"
```

---

### Task 3: Log Storage Backend

**Files:**
- Modify: `server.py` — add `datetime` import, `LOGS_DIR`, `_save_logs`, modified `_worker`, two new endpoints
- Create: `tests/test_log_storage.py`

**Interfaces:**
- Produces: `server._save_logs(namespace, pod_name, container, lines)` → writes `LOGS_DIR/{namespace}__{pod_name}__{container}__{ts}.log`
- Produces: `server.LOGS_DIR` (module-level str, patchable in tests)
- Produces: `GET /api/saved-logs` → `{"files": [{"name": str, "size_bytes": int, "modified": str}]}`
- Produces: `GET /api/saved-logs/download?file=<filename>` → `text/plain` download, or HTTP 400/404

- [ ] **Step 1: Write tests**

Create `tests/test_log_storage.py`:

```python
# tests/test_log_storage.py
from unittest.mock import patch
from fastapi.testclient import TestClient

import server
from server import _TOKEN

client = TestClient(server.app)
HEADERS = {"Authorization": f"Bearer {_TOKEN}"}


# ── _save_logs ─────────────────────────────────────────────────────────────

def test_save_logs_writes_file(tmp_path):
    with patch.object(server, 'LOGS_DIR', str(tmp_path)):
        server._save_logs("horizon", "api-abc", "api", ["line1", "line2", "line3"])
    files = list(tmp_path.iterdir())
    assert len(files) == 1
    content = files[0].read_text(encoding="utf-8")
    assert content == "line1\nline2\nline3"
    assert "horizon__api-abc__api__" in files[0].name


def test_save_logs_empty_does_nothing(tmp_path):
    with patch.object(server, 'LOGS_DIR', str(tmp_path)):
        server._save_logs("horizon", "api-abc", "api", [])
    assert list(tmp_path.iterdir()) == []


# ── /api/saved-logs ────────────────────────────────────────────────────────

def test_list_saved_logs_sorted_newest_first(tmp_path, monkeypatch):
    monkeypatch.setattr(server, 'LOGS_DIR', str(tmp_path))
    (tmp_path / "ns__pod__ctr__2026-10-02T10-00-00.log").write_text("a")
    (tmp_path / "ns__pod__ctr__2026-10-02T11-00-00.log").write_text("b")
    r = client.get("/api/saved-logs", headers=HEADERS)
    assert r.status_code == 200
    files = r.json()["files"]
    assert len(files) == 2
    assert "11-00-00" in files[0]["name"]   # newest first
    assert "10-00-00" in files[1]["name"]


def test_list_saved_logs_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(server, 'LOGS_DIR', str(tmp_path))
    r = client.get("/api/saved-logs", headers=HEADERS)
    assert r.status_code == 200
    assert r.json()["files"] == []


def test_list_saved_logs_unauthorized():
    r = client.get("/api/saved-logs")
    assert r.status_code == 401


# ── /api/saved-logs/download ───────────────────────────────────────────────

def test_download_valid_file(tmp_path, monkeypatch):
    monkeypatch.setattr(server, 'LOGS_DIR', str(tmp_path))
    fname = "ns__pod__ctr__2026-10-02T10-00-00.log"
    (tmp_path / fname).write_text("hello logs", encoding="utf-8")
    r = client.get(f"/api/saved-logs/download?file={fname}", headers=HEADERS)
    assert r.status_code == 200
    assert r.text == "hello logs"
    assert "attachment" in r.headers["content-disposition"]


def test_download_path_traversal_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(server, 'LOGS_DIR', str(tmp_path))
    r = client.get("/api/saved-logs/download?file=../etc/passwd", headers=HEADERS)
    assert r.status_code == 400


def test_download_not_found(tmp_path, monkeypatch):
    monkeypatch.setattr(server, 'LOGS_DIR', str(tmp_path))
    r = client.get("/api/saved-logs/download?file=nonexistent.log", headers=HEADERS)
    assert r.status_code == 404


def test_download_unauthorized():
    r = client.get("/api/saved-logs/download?file=something.log")
    assert r.status_code == 401
```

- [ ] **Step 2: Run tests — verify they FAIL**

```bash
pytest tests/test_log_storage.py -v
```

Expected: `AttributeError: module 'server' has no attribute '_save_logs'` and 404s for the new endpoints

- [ ] **Step 3: Add `datetime` import, `LOGS_DIR`, and `_save_logs` to `server.py`**

Find in `server.py`:
```python
import asyncio
import base64
```

Replace with:
```python
import asyncio
import base64
from datetime import datetime, timezone
```

Find in `server.py`:
```python
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get("PORT", "8080"))
```

Replace with:
```python
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get("PORT", "8080"))

LOGS_DIR = os.environ.get("LOGS_DIR", "./logs")
os.makedirs(LOGS_DIR, exist_ok=True)


def _save_logs(namespace: str, pod_name: str, container: str, lines: list) -> None:
    if not lines:
        return
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
    fname = f"{namespace}__{pod_name}__{container}__{ts}.log"
    path = os.path.join(LOGS_DIR, fname)
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines))
        logger.info("Saved %d lines → %s", len(lines), path)
    except Exception as exc:
        logger.error("Failed to save logs: %s", exc)
```

- [ ] **Step 4: Modify `_worker` in `ws_logs` to accumulate and save lines**

Find in `server.py` (inside `ws_logs`):
```python
    loop = asyncio.get_event_loop()
    q: asyncio.Queue = asyncio.Queue(maxsize=20_000)
    stop = threading.Event()

    def _worker():
```

Replace with:
```python
    loop = asyncio.get_event_loop()
    q: asyncio.Queue = asyncio.Queue(maxsize=20_000)
    stop = threading.Event()
    captured: list = []

    def _worker():
```

Find in `server.py` (inside `_worker`):
```python
        try:
            for line in w.stream(v1.read_namespaced_pod_log, **kw):
                if stop.is_set():
                    w.stop()
                    return
                asyncio.run_coroutine_threadsafe(
                    q.put({"t": "log", "line": line}), loop
                ).result(timeout=10)
        except ApiException as exc:
            asyncio.run_coroutine_threadsafe(
                q.put({"t": "err", "msg": f"[K8s {exc.status}] {exc.reason}"}), loop
            )
        except Exception as exc:
            if not stop.is_set():
                asyncio.run_coroutine_threadsafe(
                    q.put({"t": "err", "msg": str(exc)}), loop
                )
        finally:
            asyncio.run_coroutine_threadsafe(q.put(None), loop)
            w.stop()
```

Replace with:
```python
        try:
            for line in w.stream(v1.read_namespaced_pod_log, **kw):
                if stop.is_set():
                    w.stop()
                    return
                captured.append(line)
                asyncio.run_coroutine_threadsafe(
                    q.put({"t": "log", "line": line}), loop
                ).result(timeout=10)
        except ApiException as exc:
            asyncio.run_coroutine_threadsafe(
                q.put({"t": "err", "msg": f"[K8s {exc.status}] {exc.reason}"}), loop
            )
        except Exception as exc:
            if not stop.is_set():
                asyncio.run_coroutine_threadsafe(
                    q.put({"t": "err", "msg": str(exc)}), loop
                )
        finally:
            asyncio.run_coroutine_threadsafe(q.put(None), loop)
            w.stop()
            _save_logs(namespace, pod, container or "default", captured)
```

- [ ] **Step 5: Add `/api/saved-logs` and `/api/saved-logs/download` endpoints to `server.py`**

Find in `server.py`:
```python
# ── WebSocket log streaming ───────────────────────────────────────────────────
```

Insert the following block immediately before that line:

```python
@app.get("/api/saved-logs")
def list_saved_logs(_: None = Depends(_verify)):
    files = []
    try:
        for fname in os.listdir(LOGS_DIR):
            fpath = os.path.join(LOGS_DIR, fname)
            if not os.path.isfile(fpath):
                continue
            stat = os.stat(fpath)
            mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%S"
            )
            files.append({"name": fname, "size_bytes": stat.st_size, "modified": mtime})
    except Exception as exc:
        raise HTTPException(500, str(exc)) from exc
    files.sort(key=lambda x: x["modified"], reverse=True)
    return {"files": files}


@app.get("/api/saved-logs/download")
def download_saved_log(file: str = Query(...), _: None = Depends(_verify)):
    if os.path.basename(file) != file or ".." in file:
        raise HTTPException(400, "Invalid filename")
    path = os.path.join(LOGS_DIR, file)
    if not os.path.isfile(path):
        raise HTTPException(404, "File not found")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            content = fh.read()
    except Exception as exc:
        raise HTTPException(500, str(exc)) from exc
    return Response(
        content=content,
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{file}"'},
    )


```

- [ ] **Step 6: Run tests — verify they PASS**

```bash
pytest tests/test_log_storage.py -v
```

Expected: all 9 tests PASS

- [ ] **Step 7: Run full test suite**

```bash
pytest tests/ -v
```

Expected: all 14 tests PASS

- [ ] **Step 8: Commit**

```bash
git add server.py tests/test_log_storage.py
git commit -m "feat: auto-save streamed logs to disk and add saved-logs API"
```

---

### Task 4: Saved Logs Frontend Panel

**Files:**
- Modify: `static/index.html` — add Saved Logs sidebar section
- Modify: `static/app.js` — add `loadSavedLogs`, `refreshSavedLogs`, `downloadSavedLog`, `formatBytes`; hook into `setStatus` and `initAuth`/`doLogin`
- Modify: `static/style.css` — add saved-logs panel styles

**Interfaces:**
- Consumes: `GET /api/saved-logs` (Task 3)
- Consumes: `GET /api/saved-logs/download` (Task 3)
- Consumes: existing `apiFetch(path)`, `esc(s)`, `authToken`, `setStatus(s)` from `app.js`

- [ ] **Step 1: Add Saved Logs section to `static/index.html`**

Find:
```html
    </aside>
```

Replace with:
```html
      <!-- Saved Logs -->
      <div class="sidebar-sec saved-logs-sec">
        <div class="sec-hd">
          Saved Logs
          <button class="icon-btn" onclick="refreshSavedLogs()" title="Refresh">↺</button>
        </div>
        <div id="saved-logs-list" class="saved-logs-list">
          <div class="empty-msg">No saved logs</div>
        </div>
      </div>

    </aside>
```

- [ ] **Step 2: Add saved-logs functions to `static/app.js`**

Find:
```javascript
// ── Download ───────────────────────────────────────────────────────────────
```

Insert the following block immediately before that line:

```javascript
// ── Saved logs ─────────────────────────────────────────────────────────────
function formatBytes(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1_048_576) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1_048_576).toFixed(1)} MB`;
}

async function loadSavedLogs() {
  const list = $('saved-logs-list');
  list.innerHTML = '<div class="empty-msg">Loading…</div>';
  try {
    const { files } = await apiFetch('/api/saved-logs');
    if (!files.length) {
      list.innerHTML = '<div class="empty-msg">No saved logs</div>';
      return;
    }
    const frag = document.createDocumentFragment();
    for (const f of files) {
      const el = document.createElement('div');
      el.className = 'saved-log-item';
      const date = f.modified.replace('T', ' ');
      el.innerHTML = `
        <div class="sl-info">
          <span class="sl-name" title="${esc(f.name)}">${esc(f.name)}</span>
          <span class="sl-meta">${formatBytes(f.size_bytes)} · ${date}</span>
        </div>
        <button class="icon-btn" onclick="downloadSavedLog('${esc(f.name)}')" title="Download">↓</button>`;
      frag.appendChild(el);
    }
    list.innerHTML = '';
    list.appendChild(frag);
  } catch (e) {
    list.innerHTML = `<div class="empty-msg" style="color:var(--red)">Error: ${e.message}</div>`;
  }
}

function refreshSavedLogs() { loadSavedLogs(); }

function downloadSavedLog(name) {
  const params = new URLSearchParams({ file: name, token: authToken });
  window.open(`/api/saved-logs/download?${params}`, '_blank');
}

```

- [ ] **Step 3: Hook `loadSavedLogs` into `setStatus` in `static/app.js`**

Find:
```javascript
  const live = s === 'streaming';
  $('btn-connect').disabled = live || s === 'connecting';
  $('btn-stop').disabled    = !live;
```

Replace with:
```javascript
  const live = s === 'streaming';
  $('btn-connect').disabled = live || s === 'connecting';
  $('btn-stop').disabled    = !live;
  if (s === 'done' || s === 'stopped' || s === 'error') loadSavedLogs();
```

- [ ] **Step 4: Call `loadSavedLogs` on auth in `static/app.js`**

In `initAuth`, find:
```javascript
  if (authToken) {
    $('login-overlay').classList.add('hidden');
    loadContexts();
  }
```

Replace with:
```javascript
  if (authToken) {
    $('login-overlay').classList.add('hidden');
    loadContexts();
    loadSavedLogs();
  }
```

In `doLogin`, find:
```javascript
    $('login-overlay').classList.add('hidden');
    loadContexts();
```

Replace with:
```javascript
    $('login-overlay').classList.add('hidden');
    loadContexts();
    loadSavedLogs();
```

- [ ] **Step 5: Add CSS for saved-logs panel to `static/style.css`**

Append at the end of `style.css`:

```css
/* ── Saved Logs panel ── */
.saved-logs-sec {
  flex-shrink: 0;
  max-height: 200px;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.saved-logs-list {
  overflow-y: auto;
  scrollbar-width: thin;
  scrollbar-color: var(--bg3) transparent;
  flex: 1;
}

.saved-log-item {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 3px 4px;
  border-radius: 4px;
}
.saved-log-item:hover { background: var(--bg2); }

.sl-info {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
}

.sl-name {
  font-size: 11px;
  color: var(--tx1);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.sl-meta {
  font-size: 10px;
  color: var(--tx2);
}
```

- [ ] **Step 6: Manual verification**

```bash
python server.py
```

1. Log in → "Saved Logs" section appears at the bottom of the sidebar showing "No saved logs"
2. Select a pod → click Connect → wait for logs to stream → click Stop
3. Saved Logs panel auto-refreshes and shows the new `.log` file with size and timestamp
4. Click `↓` on the file entry → browser downloads the `.log` file with correct content
5. Click `↺` refresh button → list reloads from server
6. Switching namespace → saved logs panel is unaffected (it's global, not per-pod)

- [ ] **Step 7: Run full test suite**

```bash
pytest tests/ -v
```

Expected: all 14 tests PASS

- [ ] **Step 8: Commit**

```bash
git add static/index.html static/app.js static/style.css
git commit -m "feat: add saved logs sidebar panel with auto-refresh and download"
```
