from __future__ import annotations

import json
from typing import Any
from urllib.parse import unquote

import httpx

from market_agents.config import CloudflareR2Config
from market_agents.models import MarketItem


class CloudflareR2Collector:
    """
    Czyta duże archiwum z Cloudflare R2 (S3 API).
    Używa podpisanych requestów SigV4 przez boto3, jeśli dostępne;
    w przeciwnym razie wymaga publicznych URL / custom endpoint z tokenem.
    """

    name = "cloudflare_r2"

    def __init__(
        self,
        config: CloudflareR2Config,
        account_id: str,
        access_key: str,
        secret_key: str,
        endpoint: str,
    ) -> None:
        self.config = config
        self.account_id = account_id
        self.access_key = access_key
        self.secret_key = secret_key
        self.endpoint = endpoint.rstrip("/")
        self._client = None

    def _s3(self):
        if self._client is not None:
            return self._client
        try:
            import boto3
            from botocore.config import Config as BotoConfig
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "Do R2 potrzeba boto3: pip install boto3"
            ) from exc
        self._client = boto3.client(
            "s3",
            endpoint_url=self.endpoint,
            aws_access_key_id=self.access_key,
            aws_secret_access_key=self.secret_key,
            region_name="auto",
            config=BotoConfig(signature_version="s3v4"),
        )
        return self._client

    def collect(self, max_items: int | None = None) -> list[MarketItem]:
        limit = max_items or self.config.max_objects
        keys = self.list_keys(limit=limit)
        items: list[MarketItem] = []
        for key in keys:
            if not self._is_text_key(key):
                # nadal indeksuj jako sygnał (metadane)
                items.append(
                    MarketItem(
                        title=key.rsplit("/", 1)[-1],
                        url=f"r2://{self.config.bucket}/{key}",
                        source="cloudflare_r2",
                        summary=f"Obiekt R2: {key}",
                        tags=["r2", "archive"],
                        relevance_score=0.4,
                        analysis={"r2_key": key, "binary": True},
                    )
                )
                continue
            try:
                body = self.get_text(key)
            except Exception as exc:  # noqa: BLE001
                print(f"[r2] read {key}: {exc}")
                continue
            excerpt = body[:2000]
            items.append(
                MarketItem(
                    title=key.rsplit("/", 1)[-1],
                    url=f"r2://{self.config.bucket}/{key}",
                    source="cloudflare_r2",
                    summary=excerpt[:400],
                    content=body[:50000],
                    tags=["r2", "archive", "text"],
                    relevance_score=0.5,
                    analysis={"r2_key": key, "chars": len(body)},
                )
            )
        return items

    def list_keys(self, limit: int = 100, prefix: str | None = None) -> list[str]:
        client = self._s3()
        pref = prefix if prefix is not None else self.config.prefix
        keys: list[str] = []
        token = None
        while len(keys) < limit:
            kwargs: dict[str, Any] = {
                "Bucket": self.config.bucket,
                "Prefix": pref,
                "MaxKeys": min(1000, limit - len(keys)),
            }
            if token:
                kwargs["ContinuationToken"] = token
            resp = client.list_objects_v2(**kwargs)
            for obj in resp.get("Contents") or []:
                key = obj.get("Key")
                if key and not key.endswith("/"):
                    keys.append(key)
            if not resp.get("IsTruncated"):
                break
            token = resp.get("NextContinuationToken")
        return keys[:limit]

    def get_text(self, key: str) -> str:
        client = self._s3()
        obj = client.get_object(Bucket=self.config.bucket, Key=key)
        raw = obj["Body"].read()
        # try utf-8 then latin-1
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return raw.decode("latin-1", errors="replace")

    def get_json(self, key: str) -> Any:
        return json.loads(self.get_text(key))

    def search(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        q = query.lower().strip()
        hits: list[dict[str, Any]] = []
        for key in self.list_keys(limit=self.config.max_objects):
            score = 0
            name = key.lower()
            if q and q in name:
                score += 3
            for token in q.split():
                if len(token) > 2 and token in name:
                    score += 1
            if score == 0 and self._is_text_key(key):
                try:
                    text = self.get_text(key)[:8000].lower()
                    score += sum(1 for token in q.split() if len(token) > 2 and token in text)
                except Exception:  # noqa: BLE001
                    continue
            if score:
                hits.append({"key": key, "url": f"r2://{self.config.bucket}/{key}", "score": score})
        hits.sort(key=lambda h: h["score"], reverse=True)
        return hits[:limit]

    def put_json(self, key: str, payload: Any) -> None:
        client = self._s3()
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        client.put_object(
            Bucket=self.config.bucket,
            Key=key,
            Body=body,
            ContentType="application/json; charset=utf-8",
        )

    def healthcheck(self) -> dict[str, Any]:
        try:
            keys = self.list_keys(limit=5)
            return {"ok": True, "bucket": self.config.bucket, "sample_keys": keys}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}

    def _is_text_key(self, key: str) -> bool:
        lower = unquote(key).lower()
        return any(lower.endswith(ext) for ext in self.config.text_extensions)


def build_r2_collector(config: CloudflareR2Config, creds: dict[str, str | None]) -> CloudflareR2Collector:
    missing = [k for k in ("account_id", "access_key", "secret_key", "endpoint") if not creds.get(k)]
    if missing:
        raise RuntimeError(
            "Brak credentiali R2. Ustaw CF_ACCOUNT_ID, CF_R2_ACCESS_KEY_ID, "
            "CF_R2_SECRET_ACCESS_KEY (i opcjonalnie endpoint)."
        )
    return CloudflareR2Collector(
        config=config,
        account_id=str(creds["account_id"]),
        access_key=str(creds["access_key"]),
        secret_key=str(creds["secret_key"]),
        endpoint=str(creds["endpoint"]),
    )
