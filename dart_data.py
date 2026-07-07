"""DART data layer wrapping OpenDartReader.

Provides a `DartClient` that caches the OpenDartReader instance and exposes
helpers to fetch full financial statements and extract standardized account
values for Korean securities.

The implementation intentionally keeps a thin wrapper around the underlying
library and defers retry/wrapping to callers so callers can use the existing
`retry_call` helper from `stocks_of_the_day.py`.
"""
from __future__ import annotations

import contextlib
import io
import logging
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pandas as pd

try:
    import OpenDartReader  # type: ignore[import-untyped]
except Exception:  # pragma: no cover - handled by caller
    OpenDartReader = None

LOGGER = logging.getLogger("stocks_of_the_day.dart")


class DartClient:
    """Simple DART client wrapper.

    Usage:
      client = DartClient(api_key)
      df = client.finstate_all(stock_code, year, reprt_code="11011", fs_div="CFS")

    Methods intentionally raise exceptions if OpenDartReader is unavailable
    so higher-level code can decide to retry or fall back.
    """

    def __init__(self, api_key: str, call_delay: float = 0.7):
        if OpenDartReader is None:
            raise RuntimeError("OpenDartReader is not installed")
        self._api_key = api_key
        # The library replaces its module in sys.modules with the class itself
        # (sys.modules['OpenDartReader'] = dart.OpenDartReader), so `import
        # OpenDartReader` may yield either the module or the class directly.
        ctor = getattr(OpenDartReader, "OpenDartReader", OpenDartReader)
        self._dart = ctor(api_key)
        self._company_cache: Dict[str, Dict[str, Any]] = {}
        # Cache non-empty finstate frames so repeated lookups (health check,
        # quarterly section, EPS derivation) do not refetch or re-throttle.
        self._finstate_cache: Dict[Tuple[str, int, str, str], pd.DataFrame] = {}
        self._call_delay = max(0.0, float(call_delay))
        self._last_call_at = 0.0

    def _throttle(self) -> None:
        """Sleep so consecutive DART API calls are at least call_delay apart."""
        if self._call_delay <= 0:
            return
        wait = self._call_delay - (time.monotonic() - self._last_call_at)
        if wait > 0:
            time.sleep(wait)
        self._last_call_at = time.monotonic()

    def company(self, stock_code: str) -> Dict[str, Any]:
        """Return company metadata for a Korean stock code.

        The underlying OpenDartReader supports `company` lookup by stock code.
        Cache results to avoid repeated network calls.
        """
        if stock_code in self._company_cache:
            return self._company_cache[stock_code]
        self._throttle()
        info = self._dart.company(stock_code)
        self._company_cache[stock_code] = info or {}
        return self._company_cache[stock_code]

    def finstate_all(self, stock_code: str, fiscal_year: int, reprt_code: str = "11011", fs_div: str = "CFS") -> Optional[pd.DataFrame]:
        """Fetch the full finstate (all accounts) for a given stock code and year.

        Tries to call `finstate_all` on the underlying reader if available,
        otherwise falls back to `finstate`. Throttled, cached when non-empty,
        and logged one line per call so failing report lookups are visible.
        """
        cache_key = (stock_code, int(fiscal_year), reprt_code, fs_div)
        cached = self._finstate_cache.get(cache_key)
        if cached is not None:
            return cached

        self._throttle()
        # OpenDartReader prints DART status/error messages to stdout instead of
        # raising; capture them so they can be surfaced in the log line.
        captured = io.StringIO()
        outcome = ""
        frame: Optional[pd.DataFrame] = None
        error: Optional[BaseException] = None
        try:
            with contextlib.redirect_stdout(captured):
                if hasattr(self._dart, "finstate_all"):
                    frame = self._dart.finstate_all(stock_code, fiscal_year, reprt_code=reprt_code, fs_div=fs_div)
                elif hasattr(self._dart, "finstate"):
                    frame = self._dart.finstate(stock_code, fiscal_year, reprt_code=reprt_code)
                else:
                    raise RuntimeError("OpenDartReader missing finstate/finstate_all API")
        except Exception as exc:  # noqa: BLE001 - re-raised after logging
            error = exc
            outcome = f"ERROR {exc!r}"
        else:
            if frame is None or (isinstance(frame, pd.DataFrame) and frame.empty):
                outcome = "EMPTY"
            else:
                outcome = f"rows={len(frame)}"
                self._finstate_cache[cache_key] = frame

        message = " ".join(captured.getvalue().split())
        if message and outcome.startswith("rows="):
            # Successful calls still print the report label; keep the log short.
            message = ""
        LOGGER.info(
            "DART finstate_all code=%s year=%s reprt=%s fs_div=%s -> %s%s",
            stock_code,
            fiscal_year,
            reprt_code,
            fs_div,
            outcome,
            f" | dart said: {message[:300]}" if message else "",
        )
        if error is not None:
            raise error
        return frame

    def shares_outstanding(self, stock_code: str, year: int) -> Optional[float]:
        """Return issued common shares from DART's 주식총수 report, or None.

        This is the authoritative share count; statement-based heuristics
        routinely match monetary rows instead and corrupt derived EPS.
        """
        if not hasattr(self._dart, "report"):
            return None
        self._throttle()
        captured = io.StringIO()
        try:
            with contextlib.redirect_stdout(captured):
                df = self._dart.report(stock_code, "주식총수", year)
        except Exception as exc:  # noqa: BLE001
            LOGGER.info("DART 주식총수 code=%s year=%s -> ERROR %r", stock_code, year, exc)
            return None
        if df is None or df.empty or "istc_totqy" not in df.columns:
            LOGGER.info("DART 주식총수 code=%s year=%s -> EMPTY", stock_code, year)
            return None
        se_col = df["se"].astype(str) if "se" in df.columns else None
        rows = df[se_col.str.contains("보통주", na=False)] if se_col is not None else df
        if rows.empty:
            rows = df[se_col.str.contains("합계", na=False)] if se_col is not None else df
        for value in rows["istc_totqy"]:
            text = str(value).replace(",", "").strip()
            if text.isdigit() and int(text) > 0:
                shares = float(text)
                LOGGER.info("DART 주식총수 code=%s year=%s -> shares=%s", stock_code, year, int(shares))
                return shares
        LOGGER.info("DART 주식총수 code=%s year=%s -> no parsable share count", stock_code, year)
        return None

    @staticmethod
    def extract_three_year_amounts(
        frame: pd.DataFrame,
        account_id: Optional[str],
        name_patterns: List[str],
        amount_cols: Sequence[str] = ("thstrm_amount",),
        sj_div_priority: Optional[Sequence[str]] = None,
    ) -> Tuple[Optional[float], Optional[float], Optional[float]]:
        """Extract thstrm/frmtrm/bfefrmtrm amounts for a target account.

        Prefer matching by `account_id` if provided (exact match). If that
        produces no rows, fall back to fuzzy `account_nm` regex matching using
        the provided patterns.
        Returns a 3-tuple (current, prior, two_prior) where each may be None.

        `amount_cols` sets the priority for the current-term value. In interim
        reports, income-statement rows put the standalone 3-month figure in
        `thstrm_amount` and the cumulative figure in `thstrm_add_amount`
        (cash-flow rows are already cumulative in `thstrm_amount`), so pass
        ("thstrm_add_amount", "thstrm_amount") to consistently get cumulative
        values.

        `sj_div_priority` restricts matching to those statement types (sj_div:
        BS/IS/CIS/CF/SCE), in priority order. finstate_all frames contain every
        statement, and the same account_id can appear in several (e.g.
        ifrs-full_ProfitLoss appears in the income statement AND the statement
        of changes in equity), so an unrestricted first-match can pick the
        wrong statement's row. When the frame has no sj_div column the filter
        is skipped.
        """
        if frame is None or frame.empty:
            return None, None, None
        df = frame

        # Build sub-frames in statement-priority order; matching walks them in
        # order so a hit in a higher-priority statement type wins.
        subframes: List[pd.DataFrame] = [df]
        if sj_div_priority and "sj_div" in df.columns:
            sj_col = df["sj_div"].astype(str).str.strip()
            subframes = [df[sj_col == sj] for sj in sj_div_priority]
            subframes = [sub for sub in subframes if not sub.empty]
            if not subframes:
                return None, None, None

        def amounts_from(match: pd.DataFrame) -> Tuple[Optional[float], Optional[float], Optional[float]]:
            return (
                DartClient._first_amount(match, amount_cols),
                DartClient._safe_amount(match, "frmtrm_amount"),
                DartClient._safe_amount(match, "bfefrmtrm_amount"),
            )

        # try account_id match first
        if account_id is not None and "account_id" in df.columns:
            for sub in subframes:
                match = sub[sub["account_id"].astype(str).str.strip() == str(account_id).strip()]
                if not match.empty:
                    return amounts_from(match)

        # Fallback to fuzzy name matching
        nm_col = next((c for c in ("account_nm", "account_nm_kor", "account_name") if c in df.columns), None)
        if nm_col is None:
            return None, None, None
        for sub in subframes:
            for pattern in name_patterns:
                match = sub[sub[nm_col].astype(str).str.contains(pattern, case=False, na=False)]
                if not match.empty:
                    return amounts_from(match)
        return None, None, None

    @staticmethod
    def _first_amount(df: pd.DataFrame, colnames: Sequence[str]) -> Optional[float]:
        for colname in colnames:
            value = DartClient._safe_amount(df, colname)
            if value is not None:
                return value
        return None

    @staticmethod
    def _safe_amount(df: pd.DataFrame, colname: str) -> Optional[float]:
        if colname not in df.columns:
            return None
        try:
            s = df[colname].dropna().astype(str).str.replace(",", "", regex=False)
            if s.empty:
                return None
            return float(s.iloc[0])
        except Exception:
            return None
