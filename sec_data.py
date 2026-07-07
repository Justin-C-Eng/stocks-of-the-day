"""SEC EDGAR data layer for US quarterly financials.

Provides a `SecClient` that maps tickers to CIKs via the SEC's
company_tickers.json file and fetches XBRL company facts
(https://data.sec.gov/api/xbrl/companyfacts). All requests carry the
identifying User-Agent the SEC requires and are throttled to stay under
the 10 requests/second fair-access limit. Responses are cached in-memory
for the lifetime of the client (one run), so repeated lookups for the
same CIK never refetch.

Fetch methods raise on failure (non-200, malformed JSON) instead of
returning None so callers can wrap them with the existing `retry_call`
helper from `stocks_of_the_day.py`.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

import requests

LOGGER = logging.getLogger("stocks_of_the_day.sec")

_TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
_COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"


def normalize_ticker(ticker: str) -> str:
    """Normalize class-share delimiters so SEC and yfinance tickers compare equal.

    SEC's file and yfinance disagree on delimiters for some class shares
    (e.g. BRK-B vs BRK.B); fold both onto "-" and uppercase.
    """
    return ticker.strip().upper().replace(".", "-")


class SecClient:
    """Minimal SEC EDGAR client: ticker->CIK map and XBRL company facts."""

    def __init__(
        self,
        contact_name: str,
        contact_email: str,
        timeout: int = 20,
        min_interval: float = 0.11,
    ):
        # SEC fair-access policy requires a real identifying contact string.
        self._headers = {
            "User-Agent": f"{contact_name} {contact_email}".strip(),
            "Accept-Encoding": "gzip, deflate",
        }
        self._timeout = timeout
        self._min_interval = max(0.0, float(min_interval))
        self._last_call_at = 0.0
        self._ticker_map: Optional[Dict[str, str]] = None
        self._facts_cache: Dict[str, Dict[str, Any]] = {}

    def _throttle(self) -> None:
        wait = self._min_interval - (time.monotonic() - self._last_call_at)
        if wait > 0:
            time.sleep(wait)
        self._last_call_at = time.monotonic()

    def _get_json(self, url: str) -> Any:
        self._throttle()
        response = requests.get(url, headers=self._headers, timeout=self._timeout)
        if response.status_code != 200:
            raise RuntimeError(f"SEC GET {url} returned status {response.status_code}")
        try:
            return response.json()
        except ValueError as exc:
            raise RuntimeError(f"SEC GET {url} returned malformed JSON") from exc

    def ticker_to_cik(self, ticker: str) -> Optional[str]:
        """Return the 10-digit zero-padded CIK for a ticker, or None if unknown."""
        if self._ticker_map is None:
            payload = self._get_json(_TICKER_MAP_URL)
            if not isinstance(payload, dict):
                raise RuntimeError("SEC company_tickers.json: unexpected payload shape")
            mapping: Dict[str, str] = {}
            for item in payload.values():
                if not isinstance(item, dict):
                    continue
                symbol = item.get("ticker")
                cik = item.get("cik_str")
                if symbol and cik is not None:
                    mapping[normalize_ticker(str(symbol))] = f"{int(cik):010d}"
            self._ticker_map = mapping
            LOGGER.info("SEC ticker map loaded: %s tickers.", len(mapping))
        return self._ticker_map.get(normalize_ticker(ticker))

    def companyfacts(self, cik: str) -> Dict[str, Any]:
        """Fetch (and cache) the XBRL company facts payload for a CIK."""
        if cik in self._facts_cache:
            return self._facts_cache[cik]
        payload = self._get_json(_COMPANYFACTS_URL.format(cik=cik))
        if not isinstance(payload, dict) or "facts" not in payload:
            raise RuntimeError(f"SEC companyfacts CIK{cik}: unexpected payload shape")
        self._facts_cache[cik] = payload
        return payload

    @staticmethod
    def extract_quarterly(facts: Dict[str, Any], concept_names: List[str]) -> List[Dict[str, Any]]:
        """Return USD fact entries for the first concept present in the payload.

        Each entry is {"start", "end", "val", "form", "fp", "fy", "accn",
        "filed"}. Only 10-Q/10-K entries are kept; amendment forms (10-Q/A,
        10-K/A) can carry values that conflict with the originals, so they are
        included only for (start, end) periods that have no non-amendment
        entry at all.
        """
        gaap = (facts.get("facts") or {}).get("us-gaap") or {}
        for concept in concept_names:
            data = gaap.get(concept)
            if not isinstance(data, dict):
                continue
            usd = (data.get("units") or {}).get("USD") or []
            if not usd:
                continue
            primary: List[Dict[str, Any]] = []
            amendments: List[Dict[str, Any]] = []
            for entry in usd:
                if not isinstance(entry, dict):
                    continue
                row = {
                    "start": entry.get("start"),
                    "end": entry.get("end"),
                    "val": entry.get("val"),
                    "form": str(entry.get("form") or ""),
                    "fp": entry.get("fp"),
                    "fy": entry.get("fy"),
                    "accn": entry.get("accn"),
                    "filed": entry.get("filed"),
                }
                if row["start"] is None or row["end"] is None or row["val"] is None:
                    continue
                if row["form"] in ("10-Q", "10-K"):
                    primary.append(row)
                elif row["form"] in ("10-Q/A", "10-K/A"):
                    amendments.append(row)
            covered = {(row["start"], row["end"]) for row in primary}
            primary.extend(row for row in amendments if (row["start"], row["end"]) not in covered)
            if primary:
                return primary
        return []
