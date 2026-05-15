"""Close CRM API client.

Wraps requests.Session with the throttling and retry patterns from the
internal Close API reference (close-api-reference.md):

* 0.5s sleep before every call (Close limit ~100 req/min)
* Retry on 429 using Retry-After header
* Retry on 5xx with exponential backoff
* Always pass `_fields` to keep payloads small
"""
import logging
import time
from typing import Iterator

import requests

log = logging.getLogger(__name__)


class CloseClient:
    BASE = "https://api.close.com/api/v1"
    THROTTLE_SEC = 0.5
    DEFAULT_RETRIES = 5

    def __init__(self, api_key: str):
        if not api_key:
            raise ValueError("Close API key is required")
        self.session = requests.Session()
        self.session.auth = (api_key, "")

    # -------------------------------------------------------------------------
    # Internal request wrapper
    # -------------------------------------------------------------------------
    def _request(self, method, path, params=None, json=None, retry=DEFAULT_RETRIES):
        url = f"{self.BASE}{path}"
        last_exc = None
        for attempt in range(retry):
            time.sleep(self.THROTTLE_SEC)
            try:
                resp = self.session.request(
                    method, url, params=params, json=json, timeout=30
                )
            except requests.RequestException as e:
                last_exc = e
                backoff = 2 ** attempt
                log.warning(
                    "Network error on %s %s (attempt %d/%d): %s — retrying in %ds",
                    method, path, attempt + 1, retry, e, backoff,
                )
                time.sleep(backoff)
                continue

            if resp.status_code == 429:
                wait = int(resp.headers.get("Retry-After", 10))
                log.warning("Rate limited on %s %s, sleeping %ds", method, path, wait)
                time.sleep(wait)
                continue

            if 500 <= resp.status_code < 600:
                backoff = 2 ** attempt
                log.warning(
                    "Close %d on %s %s (attempt %d/%d) — retrying in %ds",
                    resp.status_code, method, path, attempt + 1, retry, backoff,
                )
                time.sleep(backoff)
                continue

            resp.raise_for_status()
            return resp.json()

        if last_exc:
            raise last_exc
        raise RuntimeError(f"Max retries exceeded for {method} {path}")

    # -------------------------------------------------------------------------
    # Contact search (paginated, generator)
    # -------------------------------------------------------------------------
    def search_contacts(self, query: str, fields: list[str]) -> Iterator[dict]:
        """Yield every contact matching `query`, paging in batches of 100.

        Results are ordered by date_updated DESCENDING (most recent first).
        This lets consumers early-terminate when records become older than a
        cutoff — important because Close caps skip-based pagination at ~10k
        records, and the /contact/ endpoint silently ignores date filters
        in the text query string.
        """
        skip = 0
        while True:
            data = self._request("GET", "/contact/", params={
                "query":     query,
                "_skip":     skip,
                "_limit":    100,
                "_fields":   ",".join(fields),
                "_order_by": "-date_updated",
            })
            batch = data.get("data", [])
            if not batch:
                return
            for c in batch:
                yield c
            if not data.get("has_more"):
                return
            skip += 100

    # -------------------------------------------------------------------------
    # Lead read / update
    # -------------------------------------------------------------------------
    def get_lead(self, lead_id: str, fields: list[str]) -> dict:
        return self._request("GET", f"/lead/{lead_id}/", params={
            "_fields": ",".join(fields),
        })

    def update_lead(self, lead_id: str, payload: dict) -> dict:
        return self._request("PUT", f"/lead/{lead_id}/", json=payload)
