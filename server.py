#!/usr/bin/env python3
"""K8s Log Viewer – FastAPI backend.

Exposes:
  GET  /api/contexts           – list kubeconfig contexts
  GET  /api/namespaces         – list namespaces in a context
  GET  /api/pods               – list pods in a namespace
  GET  /api/logs/download      – download pod logs as a file
  WS   /ws/logs                – stream pod logs in real-time
  GET  /                       – serve static frontend
"""

import asyncio
import base64
import hashlib
import hmac
import logging
import os
import secrets
import sys
import tempfile
import threading
from typing import Optional

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from kubernetes import client, config, watch
from kubernetes.client.rest import ApiException
from kubernetes.stream import stream as kube_stream
from pydantic import BaseModel

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("k8s-log-viewer")

def _resolve_kubeconfig() -> str:
    """Return a path to a valid kubeconfig file.

    Priority:
      1. KUBECONFIG_B64 env var  – base64-encoded kubeconfig content
         (use this on hosted platforms where you can't commit the file)
      2. KUBECONFIG_PATH env var – explicit file path
      3. ./kubeconfig.yaml       – local default
    """
    b64 = os.environ.get("KUBECONFIG_B64", "").strip()
    if b64:
        content = base64.b64decode(b64)
        tmp = tempfile.NamedTemporaryFile(suffix=".yaml", delete=False)
        tmp.write(content)
        tmp.close()
        logger.info("kubeconfig loaded from KUBECONFIG_B64 env var → %s", tmp.name)
        return tmp.name
    return os.environ.get("KUBECONFIG_PATH", "./kubeconfig.yaml")


KUBECONFIG_PATH = _resolve_kubeconfig()
HOST = os.environ.get("HOST", "0.0.0.0")
try:
    PORT = int(sys.argv[1])
except (IndexError, ValueError):
    PORT = int(os.environ.get("PORT", "8080"))


# ── Auth ──────────────────────────────────────────────────────────────────────

APP_USERNAME = os.environ.get("APP_USERNAME", "horizon")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "Hzn@K8s!2025#Lx7")
_TOKEN = hmac.new(
    APP_PASSWORD.encode(),
    APP_USERNAME.encode(),
    hashlib.sha256,
).hexdigest()


class LoginRequest(BaseModel):
    username: str
    password: str


def _verify(
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
) -> None:
    """Accept Bearer token in Authorization header or ?token= query param."""
    t = None
    if authorization and authorization.lower().startswith("bearer "):
        t = authorization[7:]
    elif token:
        t = token
    if not t or not secrets.compare_digest(t, _TOKEN):
        raise HTTPException(401, "Unauthorized")


# ── Kubernetes helpers ────────────────────────────────────────────────────────

def make_v1(context: str) -> client.CoreV1Api:
    """Create a CoreV1Api client bound to *context*."""
    cfg = client.Configuration()
    try:
        config.load_kube_config(
            config_file=KUBECONFIG_PATH,
            context=context,
            client_configuration=cfg,
        )
    except Exception as exc:
        raise HTTPException(500, f"kubeconfig error: {exc}") from exc
    return client.CoreV1Api(client.ApiClient(configuration=cfg))


# ── FastAPI app ───────────────────────────────────────────────────────────────

app = FastAPI(title="K8s Log Viewer", docs_url=None, redoc_url=None)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── REST endpoints ────────────────────────────────────────────────────────────

@app.get("/api/health")
def health():
    return {"ok": True}


@app.post("/api/login")
def login(req: LoginRequest):
    if secrets.compare_digest(req.username, APP_USERNAME) and \
       secrets.compare_digest(req.password, APP_PASSWORD):
        return {"token": _TOKEN}
    raise HTTPException(401, "Invalid username or password")


@app.get("/api/contexts")
def list_contexts(_: None = Depends(_verify)):
    try:
        ctxs, active = config.list_kube_config_contexts(config_file=KUBECONFIG_PATH)
        return {
            "contexts": [c["name"] for c in (ctxs or [])],
            "active": active["name"] if active else None,
        }
    except Exception as exc:
        raise HTTPException(500, str(exc)) from exc


@app.get("/api/namespaces")
def list_namespaces(context: str = Query(...), _: None = Depends(_verify)):
    v1 = make_v1(context)
    try:
        items = v1.list_namespace().items
        return {"namespaces": sorted(n.metadata.name for n in items)}
    except ApiException as exc:
        raise HTTPException(exc.status, exc.reason) from exc


@app.get("/api/pods")
def list_pods(context: str = Query(...), namespace: str = Query(...), _: None = Depends(_verify)):
    v1 = make_v1(context)
    try:
        raw = v1.list_namespaced_pod(namespace=namespace).items
    except ApiException as exc:
        raise HTTPException(exc.status, exc.reason) from exc

    pods = []
    for p in raw:
        cs = p.status.container_statuses or []
        ready = sum(1 for c in cs if c.ready)
        total = len(p.spec.containers)
        pods.append({
            "name": p.metadata.name,
            "status": p.status.phase or "Unknown",
            "ready": f"{ready}/{total}",
            "restarts": sum(c.restart_count for c in cs),
            "containers": [c.name for c in p.spec.containers],
            "init_containers": [c.name for c in (p.spec.init_containers or [])],
        })
    return {"pods": sorted(pods, key=lambda x: x["name"])}


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


@app.get("/api/logs/download")
def download_logs(
    context: str = Query(...),
    namespace: str = Query(...),
    pod: str = Query(...),
    container: Optional[str] = Query(None),
    tail_lines: Optional[int] = Query(5000),
    previous: bool = Query(False),
    _: None = Depends(_verify),
):
    v1 = make_v1(context)
    kw: dict = {"name": pod, "namespace": namespace, "previous": previous}
    if container:
        kw["container"] = container
    if tail_lines:
        kw["tail_lines"] = tail_lines
    try:
        logs = v1.read_namespaced_pod_log(**kw) or ""
    except ApiException as exc:
        raise HTTPException(exc.status, exc.reason) from exc

    fname = f"{pod}_{container or 'default'}.log"
    return Response(
        content=logs,
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


# ── WebSocket log streaming ───────────────────────────────────────────────────

@app.websocket("/ws/logs")
async def ws_logs(
    websocket: WebSocket,
    context: str,
    namespace: str,
    pod: str,
    container: Optional[str] = None,
    tail_lines: int = 100,
    previous: bool = False,
    _: None = Depends(_verify),
):
    """Stream pod logs as JSON messages:
      {"t": "log",  "line": "..."}
      {"t": "err",  "msg":  "..."}
      {"t": "done"}
      {"t": "ping"}
    """
    await websocket.accept()
    logger.info("WS open  %s/%s/%s (ctr=%s)", context, namespace, pod, container)

    loop = asyncio.get_event_loop()
    q: asyncio.Queue = asyncio.Queue(maxsize=20_000)
    stop = threading.Event()

    def _worker():
        v1 = make_v1(context)
        w = watch.Watch()
        kw: dict = {
            "name": pod,
            "namespace": namespace,
            "follow": True,
            "tail_lines": tail_lines,
            "previous": previous,
        }
        if container:
            kw["container"] = container
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

    threading.Thread(target=_worker, daemon=True).start()

    try:
        while True:
            try:
                item = await asyncio.wait_for(q.get(), timeout=120.0)
            except asyncio.TimeoutError:
                await websocket.send_json({"t": "ping"})
                continue
            if item is None:
                await websocket.send_json({"t": "done"})
                break
            await websocket.send_json(item)
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.error("WS error: %s", exc)
    finally:
        stop.set()
        logger.info("WS close %s/%s/%s", context, namespace, pod)


# ── Static files (frontend) ───────────────────────────────────────────────────

app.mount("/", StaticFiles(directory="static", html=True), name="static")


if __name__ == "__main__":
    if os.path.exists(KUBECONFIG_PATH):
        logger.info("kubeconfig OK → %s", KUBECONFIG_PATH)
    else:
        logger.warning(
            "kubeconfig not found at '%s'. "
            "Set KUBECONFIG_B64 env var (base64 of kubeconfig.yaml) or KUBECONFIG_PATH.",
            KUBECONFIG_PATH,
        )
    logger.info("Starting K8s Log Viewer → http://%s:%d", HOST, PORT)
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
