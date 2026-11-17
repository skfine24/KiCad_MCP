from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

import requests

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9",
}


class RateLimitedSession:
    """Requests session with a small delay between calls and optional cookie persistence."""

    def __init__(
        self,
        state_dir: Path | None = None,
        cookie_file: str | None = None,
        min_interval: float = 0.8,
    ) -> None:
        self._session = requests.Session()
        self._session.headers.update(DEFAULT_HEADERS)
        self._lock = threading.Lock()
        self._last_request = 0.0
        self._min_interval = min_interval
        self._cookie_path = state_dir / cookie_file if state_dir and cookie_file else None
        self._load_cookies()

    def _load_cookies(self) -> None:
        if not self._cookie_path or not self._cookie_path.exists():
            return
        try:
            cookies = json.loads(self._cookie_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        for cookie in cookies:
            name = cookie.get("name")
            value = cookie.get("value")
            if not name or value is None:
                continue
            self._session.cookies.set(
                name,
                value,
                domain=cookie.get("domain"),
                path=cookie.get("path"),
            )

    def _save_cookies(self) -> None:
        if not self._cookie_path:
            return
        self._cookie_path.parent.mkdir(parents=True, exist_ok=True)
        cookies = [
            {
                "name": cookie.name,
                "value": cookie.value,
                "domain": cookie.domain,
                "path": cookie.path,
            }
            for cookie in self._session.cookies
        ]
        tmp = self._cookie_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(cookies, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self._cookie_path)

    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        with self._lock:
            delay = self._min_interval - (time.monotonic() - self._last_request)
            if delay > 0:
                time.sleep(delay)
            timeout = kwargs.pop("timeout", 30)
            response = self._session.request(method, url, timeout=timeout, **kwargs)
            self._last_request = time.monotonic()
            self._save_cookies()
            return response

    def get(self, url: str, **kwargs: Any) -> requests.Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> requests.Response:
        return self.request("POST", url, **kwargs)

    def close(self) -> None:
        self._session.close()
