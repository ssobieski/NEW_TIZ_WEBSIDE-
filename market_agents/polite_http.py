from __future__ import annotations

import json
import random
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx

DEFAULT_USER_AGENT = (
    "MarketAgentsLocal/0.3 (+local research; polite adaptive crawler; contact: local-only)"
)


@dataclass
class CrawlPolicy:
    """Polityka anty-ban: bez bulk, z adaptacyjnym opóźnieniem per host + SSRF."""

    enabled: bool = True
    min_delay_seconds: float = 1.5
    max_delay_seconds: float = 45.0
    jitter_seconds: float = 0.5
    per_host_concurrency: int = 1
    global_concurrency: int = 2
    max_retries: int = 3
    backoff_factor: float = 2.0
    respect_robots_txt: bool = True
    adaptive: bool = True
    cooldown_on_block_seconds: float = 900.0
    timeout_seconds: float = 35.0
    user_agent: str = DEFAULT_USER_AGENT
    deny_path_prefixes: list[str] = field(
        default_factory=lambda: ["/cdn-cgi/", "/wp-admin/", "/cart", "/checkout"]
    )
    block_private_networks: bool = True
    resolve_dns_for_ssrf: bool = True
    allowed_hosts: list[str] = field(default_factory=list)
    max_redirects: int = 3
    max_response_bytes: int = 15_000_000


@dataclass
class HostHealth:
    host: str
    delay_seconds: float = 1.5
    success_count: int = 0
    fail_count: int = 0
    block_count: int = 0
    cooldown_until: float = 0.0
    last_request_at: float = 0.0
    avg_latency_seconds: float = 0.0
    last_status: int | None = None
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "HostHealth":
        return cls(
            host=str(raw.get("host") or ""),
            delay_seconds=float(raw.get("delay_seconds") or 1.5),
            success_count=int(raw.get("success_count") or 0),
            fail_count=int(raw.get("fail_count") or 0),
            block_count=int(raw.get("block_count") or 0),
            cooldown_until=float(raw.get("cooldown_until") or 0.0),
            last_request_at=float(raw.get("last_request_at") or 0.0),
            avg_latency_seconds=float(raw.get("avg_latency_seconds") or 0.0),
            last_status=(
                int(raw["last_status"]) if raw.get("last_status") is not None else None
            ),
            notes=str(raw.get("notes") or "")[:300],
        )


class CrawlBlockedError(RuntimeError):
    """Host w cooldown / robots disallow / soft-ban."""


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


class AdaptivePoliteFetcher:
    """
    Inteligentny fetcher anty-ban:
    - 1 request naraz na host (domyślnie)
    - niski global concurrency (domyślnie 2)
    - adaptacyjny delay (rośnie po 429/503/wolnych odpowiedziach)
    - Retry-After + exponential backoff
    - opcjonalnie robots.txt
    - stan zdrowia hostów na dysku
    """

    _registry_lock = threading.RLock()
    _instances: dict[str, "AdaptivePoliteFetcher"] = {}

    def __init__(
        self,
        policy: CrawlPolicy | None = None,
        data_dir: Path | str | None = None,
    ) -> None:
        self.policy = policy or CrawlPolicy()
        self.data_dir = Path(data_dir or "data")
        self._hosts: dict[str, HostHealth] = {}
        self._host_locks: dict[str, threading.Semaphore] = {}
        self._global_sem = threading.Semaphore(max(1, int(self.policy.global_concurrency)))
        self._robots: dict[str, RobotFileParser | None] = {}
        self._robots_checked_at: dict[str, float] = {}
        self._load_state()

    @classmethod
    def shared(
        cls,
        policy: CrawlPolicy | None = None,
        data_dir: Path | str | None = None,
    ) -> "AdaptivePoliteFetcher":
        key = str(Path(data_dir or "data").resolve())
        with cls._registry_lock:
            inst = cls._instances.get(key)
            if inst is None:
                inst = cls(policy=policy, data_dir=data_dir)
                cls._instances[key] = inst
            elif policy is not None:
                inst.policy = policy
                inst._global_sem = threading.Semaphore(
                    max(1, int(policy.global_concurrency))
                )
            return inst

    @classmethod
    def reset_shared(cls) -> None:
        with cls._registry_lock:
            cls._instances.clear()

    def _state_file(self) -> Path:
        return self.data_dir / "knowledge" / "crawl_health.json"

    def _load_state(self) -> None:
        path = self._state_file()
        if not path.exists():
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return
        rows = payload.get("hosts", [])
        if isinstance(rows, list):
            for row in rows:
                if isinstance(row, dict) and row.get("host"):
                    health = HostHealth.from_dict(row)
                    self._hosts[health.host] = health

    def save_state(self) -> Path:
        path = self._state_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "updated_at": time.time(),
            "policy": {
                "min_delay_seconds": self.policy.min_delay_seconds,
                "max_delay_seconds": self.policy.max_delay_seconds,
                "global_concurrency": self.policy.global_concurrency,
                "per_host_concurrency": self.policy.per_host_concurrency,
                "respect_robots_txt": self.policy.respect_robots_txt,
                "adaptive": self.policy.adaptive,
            },
            "hosts": [
                h.to_dict()
                for h in sorted(self._hosts.values(), key=lambda x: x.host)
            ],
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    @staticmethod
    def host_of(url: str) -> str:
        host = (urlparse(url).netloc or "").lower().strip()
        if host.startswith("www."):
            host = host[4:]
        return host

    def status(self, host_or_url: str | None = None) -> dict[str, Any]:
        if host_or_url:
            host = (
                self.host_of(host_or_url)
                if "://" in host_or_url
                else host_or_url.lower().strip()
            )
            health = self._hosts.get(host)
            return {
                "ok": True,
                "host": host,
                "health": health.to_dict() if health else None,
                "in_cooldown": bool(health and health.cooldown_until > time.time()),
            }
        return {
            "ok": True,
            "count": len(self._hosts),
            "hosts": [
                h.to_dict() for h in sorted(self._hosts.values(), key=lambda x: x.host)
            ],
            "policy": asdict(self.policy),
        }

    def _host_state(self, host: str) -> HostHealth:
        if host not in self._hosts:
            self._hosts[host] = HostHealth(
                host=host,
                delay_seconds=float(self.policy.min_delay_seconds),
            )
        return self._hosts[host]

    def _host_lock(self, host: str) -> threading.Semaphore:
        if host not in self._host_locks:
            self._host_locks[host] = threading.Semaphore(
                max(1, int(self.policy.per_host_concurrency))
            )
        return self._host_locks[host]

    def _wait_slot(self, host: str) -> None:
        state = self._host_state(host)
        now = time.time()
        if state.cooldown_until > now:
            wait = state.cooldown_until - now
            raise CrawlBlockedError(
                f"host {host} w cooldown jeszcze {wait:.0f}s "
                f"(block_count={state.block_count})"
            )
        delay = max(self.policy.min_delay_seconds, float(state.delay_seconds))
        delay = min(delay, self.policy.max_delay_seconds)
        delay += random.uniform(0.0, max(0.0, float(self.policy.jitter_seconds)))
        elapsed = now - (state.last_request_at or 0.0)
        if elapsed < delay:
            time.sleep(delay - elapsed)

    def _allowed_by_robots(self, url: str) -> bool:
        if not self.policy.respect_robots_txt:
            return True
        parsed = urlparse(url)
        host = self.host_of(url)
        path = parsed.path or "/"
        for prefix in self.policy.deny_path_prefixes:
            if path.startswith(prefix):
                return False
        now = time.time()
        robots = self._robots.get(host)
        checked = self._robots_checked_at.get(host, 0.0)
        if host not in self._robots or (now - checked) > 3600:
            robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
            parser = RobotFileParser()
            try:
                with httpx.Client(
                    timeout=10,
                    follow_redirects=True,
                    headers={"User-Agent": self.policy.user_agent},
                ) as client:
                    resp = client.get(robots_url)
                    if resp.status_code >= 400:
                        self._robots[host] = None
                        self._robots_checked_at[host] = now
                        return True
                    parser.parse(resp.text.splitlines())
                    self._robots[host] = parser
                    self._robots_checked_at[host] = now
                    robots = parser
            except Exception:  # noqa: BLE001
                self._robots[host] = None
                self._robots_checked_at[host] = now
                return True
        if robots is None:
            return True
        try:
            return bool(robots.can_fetch(self.policy.user_agent, url))
        except Exception:  # noqa: BLE001
            return True

    def _on_success(self, host: str, latency: float, status: int) -> None:
        state = self._host_state(host)
        state.success_count += 1
        state.last_status = status
        state.last_request_at = time.time()
        if state.avg_latency_seconds <= 0:
            state.avg_latency_seconds = latency
        else:
            state.avg_latency_seconds = (state.avg_latency_seconds * 0.7) + (latency * 0.3)
        if self.policy.adaptive:
            if latency > 3.0:
                state.delay_seconds = min(
                    self.policy.max_delay_seconds,
                    state.delay_seconds * 1.25 + 0.3,
                )
            elif latency < 0.8 and state.fail_count == 0:
                state.delay_seconds = max(
                    self.policy.min_delay_seconds,
                    state.delay_seconds * 0.9,
                )
        self.save_state()

    def _on_block(
        self, host: str, status: int, retry_after: float | None = None
    ) -> None:
        state = self._host_state(host)
        state.block_count += 1
        state.fail_count += 1
        state.last_status = status
        state.last_request_at = time.time()
        bump = max(
            state.delay_seconds * self.policy.backoff_factor,
            self.policy.min_delay_seconds * 2,
        )
        state.delay_seconds = min(self.policy.max_delay_seconds, bump)
        cool = (
            float(retry_after)
            if retry_after and retry_after > 0
            else self.policy.cooldown_on_block_seconds
        )
        if status == 403:
            cool = max(cool, self.policy.cooldown_on_block_seconds)
        state.cooldown_until = time.time() + cool
        state.notes = f"blocked status={status} cooldown={cool:.0f}s"
        self.save_state()

    def _on_fail(self, host: str, status: int | None = None) -> None:
        state = self._host_state(host)
        state.fail_count += 1
        state.last_status = status
        state.last_request_at = time.time()
        if self.policy.adaptive:
            state.delay_seconds = min(
                self.policy.max_delay_seconds,
                max(self.policy.min_delay_seconds, state.delay_seconds * 1.35),
            )
        self.save_state()

    def get(
        self,
        url: str,
        *,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
        max_retries: int | None = None,
    ) -> httpx.Response:
        from market_agents.security import SecurityError, validate_fetch_url

        try:
            url = validate_fetch_url(
                url,
                allowed_hosts=self.policy.allowed_hosts or None,
                allow_private=not bool(self.policy.block_private_networks),
                resolve_dns=bool(self.policy.resolve_dns_for_ssrf),
            )
        except SecurityError as exc:
            raise CrawlBlockedError(str(exc)) from exc

        if not self.policy.enabled:
            with httpx.Client(
                timeout=timeout or self.policy.timeout_seconds,
                follow_redirects=True,
                max_redirects=max(0, int(self.policy.max_redirects)),
                headers={"User-Agent": self.policy.user_agent, **(headers or {})},
            ) as client:
                response = client.request(method, url, headers=headers)
                self._assert_public_response(response)
                return response

        host = self.host_of(url)
        if not host:
            raise ValueError(f"invalid url: {url}")
        if not self._allowed_by_robots(url):
            raise CrawlBlockedError(f"robots.txt disallow: {url}")

        retries = int(self.policy.max_retries if max_retries is None else max_retries)
        host_lock = self._host_lock(host)
        last_exc: Exception | None = None

        for attempt in range(retries + 1):
            if not self._global_sem.acquire(timeout=120):
                raise TimeoutError("global crawl concurrency timeout")
            if not host_lock.acquire(timeout=120):
                self._global_sem.release()
                raise TimeoutError(f"host crawl lock timeout: {host}")
            try:
                self._wait_slot(host)
                req_headers = {
                    "User-Agent": self.policy.user_agent,
                    "Accept": "text/html,application/xhtml+xml,application/pdf,*/*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.8,pl;q=0.7",
                    **(headers or {}),
                }
                started = time.time()
                with httpx.Client(
                    timeout=timeout or self.policy.timeout_seconds,
                    follow_redirects=True,
                    max_redirects=max(0, int(self.policy.max_redirects)),
                    headers=req_headers,
                ) as client:
                    response = client.request(method, url)
                self._assert_public_response(response)
                latency = time.time() - started
                self._host_state(host).last_request_at = time.time()

                if response.status_code in {429, 503}:
                    retry_after = _parse_retry_after(response.headers.get("Retry-After"))
                    self._on_block(host, response.status_code, retry_after)
                    last_exc = httpx.HTTPStatusError(
                        f"rate limited {response.status_code}",
                        request=response.request,
                        response=response,
                    )
                    sleep_for = retry_after or min(
                        self.policy.max_delay_seconds,
                        self.policy.min_delay_seconds
                        * (self.policy.backoff_factor ** attempt),
                    )
                    time.sleep(min(float(sleep_for), self.policy.max_delay_seconds))
                    continue

                if response.status_code == 403:
                    self._on_block(host, 403)
                    raise CrawlBlockedError(f"403 Forbidden — cooldown host {host}")

                if response.status_code >= 400:
                    self._on_fail(host, response.status_code)
                    response.raise_for_status()

                clen = response.headers.get("content-length")
                max_bytes = int(self.policy.max_response_bytes)
                if clen and int(clen) > max_bytes:
                    raise CrawlBlockedError(
                        f"response too large ({clen} > {max_bytes})"
                    )
                if len(response.content) > max_bytes:
                    raise CrawlBlockedError(
                        f"response too large ({len(response.content)} bytes)"
                    )

                self._on_success(host, latency, response.status_code)
                return response
            except CrawlBlockedError:
                raise
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if not isinstance(exc, httpx.HTTPStatusError):
                    self._on_fail(host)
                if attempt >= retries:
                    break
                time.sleep(
                    min(
                        self.policy.max_delay_seconds,
                        self.policy.min_delay_seconds
                        * (self.policy.backoff_factor ** attempt)
                        + random.uniform(0, self.policy.jitter_seconds),
                    )
                )
            finally:
                host_lock.release()
                self._global_sem.release()

        assert last_exc is not None
        raise last_exc

    def _assert_public_response(self, response: httpx.Response) -> None:
        """Po redirectach — sprawdź finalny host (SSRF via redirect)."""
        if not self.policy.block_private_networks:
            return
        from market_agents.security import SecurityError, validate_fetch_url

        try:
            validate_fetch_url(
                str(response.url),
                allowed_hosts=self.policy.allowed_hosts or None,
                allow_private=False,
                resolve_dns=bool(self.policy.resolve_dns_for_ssrf),
            )
        except SecurityError as exc:
            raise CrawlBlockedError(f"SSRF blocked after redirect: {exc}") from exc

    def get_text(self, url: str, **kwargs: Any) -> str:
        return self.get(url, **kwargs).text

    def get_bytes(self, url: str, **kwargs: Any) -> bytes:
        return self.get(url, **kwargs).content


def build_fetcher_from_config(config: Any) -> AdaptivePoliteFetcher:
    crawl = getattr(getattr(config, "agents", None), "crawl", None)
    security = getattr(getattr(config, "agents", None), "security", None)
    if crawl is None:
        policy = CrawlPolicy()
        if security is not None:
            policy.block_private_networks = bool(
                getattr(security, "block_private_networks", True)
            )
            policy.allowed_hosts = list(getattr(security, "allowed_hosts", None) or [])
    else:
        allowed = list(getattr(crawl, "allowed_hosts", None) or [])
        if security is not None:
            allowed = list(
                dict.fromkeys(
                    [*allowed, *(getattr(security, "allowed_hosts", None) or [])]
                )
            )
        block_private = bool(getattr(crawl, "block_private_networks", True))
        if security is not None:
            block_private = block_private and bool(
                getattr(security, "block_private_networks", True)
            )
        policy = CrawlPolicy(
            enabled=bool(getattr(crawl, "enabled", True)),
            min_delay_seconds=float(getattr(crawl, "min_delay_seconds", 1.5)),
            max_delay_seconds=float(getattr(crawl, "max_delay_seconds", 45.0)),
            jitter_seconds=float(getattr(crawl, "jitter_seconds", 0.5)),
            per_host_concurrency=int(getattr(crawl, "per_host_concurrency", 1)),
            global_concurrency=int(getattr(crawl, "global_concurrency", 2)),
            max_retries=int(getattr(crawl, "max_retries", 3)),
            backoff_factor=float(getattr(crawl, "backoff_factor", 2.0)),
            respect_robots_txt=bool(getattr(crawl, "respect_robots_txt", True)),
            adaptive=bool(getattr(crawl, "adaptive", True)),
            cooldown_on_block_seconds=float(
                getattr(crawl, "cooldown_on_block_seconds", 900.0)
            ),
            timeout_seconds=float(getattr(crawl, "timeout_seconds", 35.0)),
            user_agent=str(getattr(crawl, "user_agent", DEFAULT_USER_AGENT)),
            block_private_networks=block_private,
            resolve_dns_for_ssrf=bool(getattr(crawl, "resolve_dns_for_ssrf", True)),
            allowed_hosts=allowed,
            max_redirects=int(getattr(crawl, "max_redirects", 3)),
            max_response_bytes=int(getattr(crawl, "max_response_bytes", 15_000_000)),
        )
    data_dir = getattr(config, "data_path", None) or "data"
    return AdaptivePoliteFetcher.shared(policy=policy, data_dir=data_dir)
