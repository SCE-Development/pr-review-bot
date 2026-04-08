import threading
import time
import uuid
from copy import deepcopy


class DashboardState:
    def __init__(self):
        self._lock = threading.Lock()
        self._runs = {}
        self._events = []
        self._max_events = 200

    def create_run(
        self, repo: str, pr_number: int, installation_id: int, action: str
    ) -> str:
        run_id = str(uuid.uuid4())
        now = time.time()
        run = {
            "run_id": run_id,
            "repo": repo,
            "pr_number": pr_number,
            "installation_id": installation_id,
            "action": action,
            "status": "running",
            "started_at": now,
            "updated_at": now,
            "ended_at": None,
            "current_step": "queued",
            "error": None,
            "summary": {
                "files_total": 0,
                "files_skipped": 0,
                "files_reviewed": 0,
                "comments_generated": 0,
                "comments_posted": 0,
            },
            "steps": [
                {
                    "name": "queued",
                    "status": "done",
                    "message": "Webhook accepted",
                    "at": now,
                },
            ],
        }

        with self._lock:
            self._runs[run_id] = run
            self._add_event_unlocked(
                {
                    "type": "run_created",
                    "run_id": run_id,
                    "repo": repo,
                    "pr_number": pr_number,
                    "message": f"Queued PR #{pr_number} in {repo}",
                    "at": now,
                }
            )

        return run_id

    def step_update(
        self, run_id: str, step: str, status: str, message: str = ""
    ) -> None:
        now = time.time()
        with self._lock:
            run = self._runs.get(run_id)
            if not run:
                return
            run["current_step"] = step
            run["updated_at"] = now
            run["steps"].append(
                {"name": step, "status": status, "message": message, "at": now}
            )
            self._add_event_unlocked(
                {
                    "type": "step",
                    "run_id": run_id,
                    "repo": run["repo"],
                    "pr_number": run["pr_number"],
                    "message": f"{step}: {message or status}",
                    "at": now,
                }
            )

    def update_summary(self, run_id: str, **metrics: int) -> None:
        now = time.time()
        with self._lock:
            run = self._runs.get(run_id)
            if not run:
                return
            for key, value in metrics.items():
                if key in run["summary"]:
                    run["summary"][key] = value
            run["updated_at"] = now

    def complete_run(self, run_id: str, status: str, error: str = None) -> None:
        now = time.time()
        with self._lock:
            run = self._runs.get(run_id)
            if not run:
                return
            run["status"] = status
            run["ended_at"] = now
            run["updated_at"] = now
            run["error"] = error
            self._add_event_unlocked(
                {
                    "type": "run_finished",
                    "run_id": run_id,
                    "repo": run["repo"],
                    "pr_number": run["pr_number"],
                    "message": "Failed" if status == "failed" else "Completed",
                    "at": now,
                }
            )

    def _add_event_unlocked(self, event):
        self._events.append(event)
        if len(self._events) > self._max_events:
            self._events = self._events[-self._max_events :]

    def snapshot(self):
        with self._lock:
            runs = list(self._runs.values())
            runs.sort(key=lambda r: r["started_at"], reverse=True)
            overview = {
                "total": len(runs),
                "running": sum(1 for r in runs if r["status"] == "running"),
                "completed": sum(1 for r in runs if r["status"] == "completed"),
                "failed": sum(1 for r in runs if r["status"] == "failed"),
            }
            return {
                "overview": overview,
                "runs": deepcopy(runs[:50]),
                "events": deepcopy(self._events[-80:]),
                "now": time.time(),
            }


dashboard_state = DashboardState()
