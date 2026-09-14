"""PreviewManager: the lifecycle of live previews (Phase 6).

Built on RuntimeManager -- a preview IS a runtime started with
`preview=True` -- plus only what a browser-facing preview needs:

  * an opaque capability id, one per runtime generation (restarting a
    preview issues a NEW id, so every earlier URL stops working);
  * readiness proven end to end: READY only once the app answers HTTP
    through its relay, never merely because a container started;
  * limits on how many previews run, how long they idle and how long they
    live, enforced by `sweep()`;
  * the private map preview id -> relay upstream that the gateway consults.
    It is the gateway's ONLY way to find an upstream: nothing a browser
    sends can name a host or port.

In memory, like RuntimeManager: after a Redstone restart every preview URL
is dead, and orphan reconciliation destroys the containers.
"""

from __future__ import annotations

import http.client
import logging
import threading
import time
from dataclasses import replace
from datetime import datetime, timezone

from ..config import PreviewConfig
from ..domain.models import EventType, Framework, Preview, PreviewStatus
from ..events.bus import EventBus
from ..runtime.errors import RedstoneRuntimeError, RuntimeErrorCode
from ..runtime.manager import RuntimeManager
from ..sandbox.models import RELAY_STATUS_HEADER, RELAY_TOKEN_HEADER, PreviewUpstream
from ..workspace.manager import Workspace
from .errors import PreviewError, PreviewErrorCode
from .models import PreviewEndpoint, new_preview_id

__all__ = ["PreviewManager", "probe_upstream"]

_ACTIVE = frozenset({PreviewStatus.STARTING, PreviewStatus.READY})


def probe_upstream(upstream: PreviewUpstream, timeout: float = 2.0) -> bool:
    """True once the app itself answers HTTP through its relay. A response the
    relay made itself (it marks those) -- app not listening yet, token
    refused -- is not readiness."""
    connection = http.client.HTTPConnection(upstream.host, upstream.port, timeout=timeout)
    try:
        connection.request("GET", "/", headers={RELAY_TOKEN_HEADER: upstream.token})
        response = connection.getresponse()
        response.read(4096)
        return response.getheader(RELAY_STATUS_HEADER) is None
    except (OSError, http.client.HTTPException):
        return False
    finally:
        connection.close()


class PreviewManager:
    def __init__(
        self,
        runtime_manager: RuntimeManager,
        config: PreviewConfig | None = None,
        *,
        event_bus: EventBus | None = None,
        logger: logging.Logger | None = None,
        probe=probe_upstream,
        probe_interval: float = 0.25,
    ) -> None:
        self._runtimes = runtime_manager
        self._config = config or PreviewConfig()
        self._events = event_bus
        self._logger = logger
        self._probe = probe
        self._probe_interval = probe_interval

        self._lock = threading.Lock()
        self._sessions: dict[str, Preview] = {}
        self._by_project: dict[str, str] = {}
        self._upstreams: dict[str, PreviewUpstream] = {}
        self._project_locks: dict[str, threading.Lock] = {}
        self._sweeper: threading.Thread | None = None
        self._sweeper_stop = threading.Event()

    # ------------------------------------------------------------- views

    @property
    def config(self) -> PreviewConfig:
        return self._config

    def endpoint(self, preview: Preview) -> PreviewEndpoint:
        return PreviewEndpoint(preview.id, self._config.scheme, self._config.domain,
                               self._config.public_port)

    def describe(self, preview: Preview) -> dict:
        url = self.endpoint(preview).url if preview.status is PreviewStatus.READY else None
        return preview.to_dict(url=url)

    def get_for_project(self, project_id: str) -> Preview:
        with self._lock:
            preview_id = self._by_project.get(project_id)
            preview = self._sessions.get(preview_id) if preview_id else None
        if preview is None:
            raise PreviewError(PreviewErrorCode.NOT_FOUND)
        return preview

    def status_of(self, preview_id: str) -> PreviewStatus | None:
        with self._lock:
            preview = self._sessions.get(preview_id)
        return preview.status if preview else None

    def resolve(self, preview_id: str) -> PreviewUpstream | None:
        """The gateway's lookup: an upstream only for a READY preview, and
        each hit counts as activity for the idle timeout."""
        with self._lock:
            preview = self._sessions.get(preview_id)
            if preview is None or preview.status is not PreviewStatus.READY:
                return None
            upstream = self._upstreams.get(preview_id)
            if upstream is not None:
                self._sessions[preview_id] = preview.touched()
            return upstream

    # --------------------------------------------------------- internals

    def _emit(self, event_type: EventType, preview: Preview, **payload) -> None:
        # Deliberately no preview id: it is a capability. Status and error
        # codes only -- never an address, port, token or container id.
        if self._events is not None:
            self._events.publish(event_type, project_id=preview.project_id,
                                 payload={"status": preview.status.value, **payload})
        if self._logger is not None:
            self._logger.info("preview_event type=%s status=%s", event_type.value,
                              preview.status.value)

    def _project_lock(self, project_id: str) -> threading.Lock:
        with self._lock:
            return self._project_locks.setdefault(project_id, threading.Lock())

    def _put(self, preview: Preview) -> Preview:
        with self._lock:
            self._sessions[preview.id] = preview
        return preview

    def _discard_runtime(self, runtime_id: str | None, project_id: str) -> None:
        if not runtime_id:
            return
        try:
            self._runtimes.destroy(runtime_id, project_id)
        except RedstoneRuntimeError:
            if self._logger is not None:
                self._logger.warning("preview_runtime_destroy_failed")

    def _fail(self, preview: Preview, runtime_id: str | None, error: PreviewError) -> Preview:
        self._discard_runtime(runtime_id, preview.project_id)
        with self._lock:
            self._upstreams.pop(preview.id, None)
        failed = self._put(preview.with_runtime(None).with_status(PreviewStatus.FAILED)
                           .with_error(error.code.value, error.safe_message))
        self._emit(EventType.PREVIEW_FAILED, failed, error_code=error.code.value)
        return failed

    def _wait_until_serving(self, upstream: PreviewUpstream) -> bool:
        deadline = time.monotonic() + self._config.ready_timeout_seconds
        while time.monotonic() < deadline:
            if self._probe(upstream):
                return True
            time.sleep(self._probe_interval)
        return False

    # ---------------------------------------------------------- lifecycle

    def start(self, project_id: str, workspace: Workspace, framework: Framework) -> Preview:
        """Create and start a preview, or return the READY one (idempotent)."""
        lock = self._project_lock(project_id)
        if not lock.acquire(blocking=False):
            raise PreviewError(PreviewErrorCode.BUSY)
        try:
            return self._start_locked(project_id, workspace, framework)
        finally:
            lock.release()

    def _start_locked(self, project_id: str, workspace: Workspace, framework: Framework) -> Preview:
        with self._lock:
            old_id = self._by_project.get(project_id)
            old = self._sessions.get(old_id) if old_id else None
            if old is not None and old.status is PreviewStatus.READY:
                return old
            if not getattr(self._runtimes.provider, "is_isolated", False):
                raise PreviewError(PreviewErrorCode.UNAVAILABLE,
                                   internal="sandbox provider does not isolate")
            active = sum(1 for p in self._sessions.values() if p.status in _ACTIVE)
            if active >= self._config.max_active:
                raise PreviewError(PreviewErrorCode.LIMIT_REACHED)
            # A new capability for every runtime generation: nothing that
            # addressed an earlier (stopped/failed) preview reaches this one.
            if old is not None:
                self._sessions.pop(old.id, None)
            preview = Preview(id=new_preview_id(), project_id=project_id,
                              workspace_id=workspace.id, status=PreviewStatus.STARTING)
            self._sessions[preview.id] = preview
            self._by_project[project_id] = preview.id

        self._emit(EventType.PREVIEW_CREATED, preview)
        self._emit(EventType.PREVIEW_STARTING, preview)

        runtime_id: str | None = None
        try:
            runtime = self._runtimes.create(project_id, workspace, framework)
            runtime_id = runtime.id
            preview = self._put(preview.with_runtime(runtime_id))
            self._runtimes.start(runtime_id, project_id, workspace, preview=True)
            upstream = self._runtimes.preview_upstream(runtime_id, project_id)
            if upstream is None:
                raise PreviewError(PreviewErrorCode.UNAVAILABLE, internal="no preview upstream")
            if not self._wait_until_serving(upstream):
                raise PreviewError(
                    PreviewErrorCode.START_FAILED,
                    safe_message="The preview did not answer HTTP on its preview port (5173).",
                )
        except RedstoneRuntimeError as exc:
            code = PreviewErrorCode.BUSY if exc.code is RuntimeErrorCode.BUSY else PreviewErrorCode.START_FAILED
            error = PreviewError(code, safe_message=None if code is PreviewErrorCode.BUSY else exc.safe_message,
                                 internal=exc.internal)
            self._fail(preview, runtime_id, error)
            raise error from exc
        except PreviewError as error:
            self._fail(preview, runtime_id, error)
            raise

        with self._lock:
            self._upstreams[preview.id] = upstream
            ready = replace(preview.with_status(PreviewStatus.READY), last_error=None)
            self._sessions[preview.id] = ready
        self._emit(EventType.PREVIEW_READY, ready)
        return ready

    def stop(self, project_id: str) -> Preview:
        """Stop serving and destroy the runtime; the record stays (STOPPED).
        Idempotent. Waits for an in-flight start to finish first."""
        with self._project_lock(project_id):
            preview = self.get_for_project(project_id)
            with self._lock:
                self._upstreams.pop(preview.id, None)
            self._discard_runtime(preview.runtime_id, project_id)
            if preview.status is PreviewStatus.STOPPED:
                return preview
            stopped = self._put(preview.with_runtime(None).with_status(PreviewStatus.STOPPED))
            self._emit(EventType.PREVIEW_STOPPED, stopped)
            return stopped

    def destroy(self, project_id: str) -> bool:
        """Stop and forget. Idempotent: False if there was nothing to destroy."""
        with self._project_lock(project_id):
            with self._lock:
                preview_id = self._by_project.get(project_id)
                preview = self._sessions.get(preview_id) if preview_id else None
                if preview is None:
                    return False
                self._upstreams.pop(preview.id, None)
            self._discard_runtime(preview.runtime_id, project_id)
            with self._lock:
                self._sessions.pop(preview.id, None)
                self._by_project.pop(project_id, None)
            self._emit(EventType.PREVIEW_DESTROYED, preview.with_status(PreviewStatus.DESTROYED))
            return True

    # ------------------------------------------------------------- sweep

    def sweep(self, now: datetime | None = None) -> dict:
        """Enforce idle timeout and maximum lifetime, and notice crashes."""
        now = now or datetime.now(timezone.utc)
        with self._lock:
            ready = [p for p in self._sessions.values() if p.status is PreviewStatus.READY]
        report: dict[str, list[str]] = {"expired": [], "idle": [], "crashed": []}
        for preview in ready:
            age = (now - preview.created_at).total_seconds()
            idle = (now - preview.last_activity).total_seconds()
            if age > self._config.max_lifetime_seconds:
                self.stop(preview.project_id)
                report["expired"].append(preview.project_id)
            elif idle > self._config.idle_timeout_seconds:
                self.stop(preview.project_id)
                report["idle"].append(preview.project_id)
            elif preview.runtime_id and not self._runtimes.health_check(preview.runtime_id,
                                                                        preview.project_id):
                self._fail(preview, preview.runtime_id, PreviewError(
                    PreviewErrorCode.UNAVAILABLE, safe_message="The preview stopped responding."))
                report["crashed"].append(preview.project_id)
        return report

    def start_sweeper(self, interval: float = 30.0) -> None:
        if self._sweeper is not None:
            return
        self._sweeper_stop.clear()

        def loop() -> None:
            while not self._sweeper_stop.wait(interval):
                try:
                    self.sweep()
                except Exception:   # noqa: BLE001 -- a sweep failure must not kill the sweeper
                    if self._logger is not None:
                        self._logger.exception("preview_sweep_failed")

        self._sweeper = threading.Thread(target=loop, name="preview-sweeper", daemon=True)
        self._sweeper.start()

    def stop_sweeper(self) -> None:
        self._sweeper_stop.set()
        self._sweeper = None
