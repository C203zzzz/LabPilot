"""Keep a durable claim alive through slow OCR/HTTP calls."""
import threading


class LeaseHeartbeat:
    def __init__(self, repo, task_id, owner):
        self.repo, self.task_id, self.owner = repo, task_id, owner
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._renew, daemon=True)
        self.thread.start()

    def _renew(self):
        while not self.stopped.wait(30):
            try:
                if not self.repo.renew_analysis(self.task_id, self.owner):
                    return
            except Exception:
                return

    def close(self):
        self.stopped.set()
        self.thread.join(timeout=2)
