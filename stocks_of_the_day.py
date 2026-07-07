#!/usr/bin/env python3
"""
Stocks of the Day

Daily financial email agent that:
1. Summarizes major market assets.
2. Screens US and Korean equities for a 30-day SMA crossover.
3. Runs basic financial-health verification.
4. Extracts financials, valuation data, projections, profiles, and news.
5. Sends a clean HTML email through Gmail SMTP.

Configuration is loaded from a .env file. See stocks_of_the_day.env.example.
"""

from __future__ import annotations

import html
import io
import logging
import logging.handlers
import os
import re
import smtplib
import socket
import sqlite3
import sys
import time
import traceback
from collections import deque
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd
import requests
import yfinance as yf
from dotenv import load_dotenv

try:
    import certifi as _certifi
    os.environ.setdefault("SSL_CERT_FILE", _certifi.where())
except ImportError:
    pass

try:
    import FinanceDataReader as fdr
except ImportError:  # pragma: no cover - handled at runtime with a clear log
    fdr = None

# GitHub-source installs expose the module as "OpenDartReader"; PyPI wheels
# (>=0.3) ship the same code under the lowercase name "opendartreader".
try:
    import OpenDartReader  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover - handled at runtime with a clear log
    try:
        import opendartreader as OpenDartReader  # type: ignore[import-untyped]
    except ImportError:
        OpenDartReader = None

try:
    import anthropic
except ImportError:  # pragma: no cover - handled at runtime with a clear log
    anthropic = None
    
from dart_data import DartClient
from sec_data import SecClient

# cached Dart client instance
_DART_CLIENT: Optional[DartClient] = None
_DART_SKIP_LOGGED = False


def get_dart_client(config: AppConfig) -> Optional[DartClient]:
    global _DART_CLIENT, _DART_SKIP_LOGGED
    if not config.dart_api_key or OpenDartReader is None:
        # Log the reason once per run; a silent None here would make every
        # DART consumer quietly fall back to yfinance with no trace.
        if not _DART_SKIP_LOGGED:
            reason = "DART_API_KEY not set" if not config.dart_api_key else "OpenDartReader import failed"
            LOGGER.warning("DART disabled (%s); Korean financials will fall back to yfinance.", reason)
            _DART_SKIP_LOGGED = True
        return None
    if _DART_CLIENT is None:
        _DART_CLIENT = DartClient(config.dart_api_key, call_delay=config.dart_call_delay_seconds)
    return _DART_CLIENT


# cached SEC EDGAR client instance
_SEC_CLIENT: Optional[SecClient] = None


def get_sec_client(config: AppConfig) -> Optional[SecClient]:
    global _SEC_CLIENT
    # SEC's fair-access policy requires a real identifying contact; without
    # one, skip EDGAR entirely and let callers fall back to yfinance.
    if not config.sec_contact_email:
        return None
    if _SEC_CLIENT is None:
        _SEC_CLIENT = SecClient(
            config.sec_contact_name or "StocksOfTheDay",
            config.sec_contact_email,
            timeout=config.request_timeout,
        )
    return _SEC_CLIENT


LOGGER = logging.getLogger("stocks_of_the_day")


DEFAULT_TEST_EMAIL_TO = "juhanchang0606@gmail.com"


MARKET_ASSETS = {
    "^GSPC": "S&P 500",
    "^IXIC": "NASDAQ Composite",
    "^DJI": "Dow Jones Industrial Average",
    "^KS11": "KOSPI",
    "GC=F": "Gold Futures",
    "BTC-USD": "Bitcoin",
    "CL=F": "WTI Crude Oil",
}


US_UNIVERSE = [
    "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "GOOG", "BRK-B", "LLY", "AVGO",
    "JPM", "TSLA", "XOM", "UNH", "V", "MA", "PG", "COST", "JNJ", "HD",
    "ABBV", "WMT", "NFLX", "BAC", "KO", "CRM", "ORCL", "MRK", "AMD", "CVX",
    "PEP", "ADBE", "TMO", "MCD", "LIN", "CSCO", "ACN", "ABT", "WFC", "IBM",
    "QCOM", "GE", "INTU", "DHR", "TXN", "PM", "AMGN", "VZ", "NOW", "ISRG",
]


US_NAME_MAP = {
    "AAPL": "Apple",
    "MSFT": "Microsoft",
    "NVDA": "NVIDIA",
    "AMZN": "Amazon",
    "META": "Meta Platforms",
    "GOOGL": "Alphabet Class A",
    "GOOG": "Alphabet Class C",
    "BRK-B": "Berkshire Hathaway",
    "LLY": "Eli Lilly",
    "AVGO": "Broadcom",
    "JPM": "JPMorgan Chase",
    "TSLA": "Tesla",
    "XOM": "Exxon Mobil",
    "UNH": "UnitedHealth Group",
    "V": "Visa",
    "MA": "Mastercard",
    "PG": "Procter & Gamble",
    "COST": "Costco Wholesale",
    "JNJ": "Johnson & Johnson",
    "HD": "Home Depot",
    "ABBV": "AbbVie",
    "WMT": "Walmart",
    "NFLX": "Netflix",
    "BAC": "Bank of America",
    "KO": "Coca-Cola",
    "CRM": "Salesforce",
    "ORCL": "Oracle",
    "MRK": "Merck",
    "AMD": "Advanced Micro Devices",
    "CVX": "Chevron",
    "PEP": "PepsiCo",
    "ADBE": "Adobe",
    "TMO": "Thermo Fisher Scientific",
    "MCD": "McDonald's",
    "LIN": "Linde",
    "CSCO": "Cisco Systems",
    "ACN": "Accenture",
    "ABT": "Abbott Laboratories",
    "WFC": "Wells Fargo",
    "IBM": "IBM",
    "QCOM": "Qualcomm",
    "GE": "GE Aerospace",
    "INTU": "Intuit",
    "DHR": "Danaher",
    "TXN": "Texas Instruments",
    "PM": "Philip Morris International",
    "AMGN": "Amgen",
    "VZ": "Verizon",
    "NOW": "ServiceNow",
    "ISRG": "Intuitive Surgical",
}


KR_UNIVERSE = [
    ("005930", "Samsung Electronics"),
    ("000660", "SK Hynix"),
    ("373220", "LG Energy Solution"),
    ("207940", "Samsung Biologics"),
    ("005380", "Hyundai Motor"),
    ("000270", "Kia"),
    ("068270", "Celltrion"),
    ("105560", "KB Financial Group"),
    ("055550", "Shinhan Financial Group"),
    ("005490", "POSCO Holdings"),
    ("035420", "NAVER"),
    ("012330", "Hyundai Mobis"),
    ("028260", "Samsung C&T"),
    ("035720", "Kakao"),
    ("051910", "LG Chem"),
    ("032830", "Samsung Life Insurance"),
    ("086790", "Hana Financial Group"),
    ("066570", "LG Electronics"),
    ("096770", "SK Innovation"),
    ("034730", "SK"),
    ("138040", "Meritz Financial Group"),
    ("015760", "Korea Electric Power"),
    ("003550", "LG"),
    ("017670", "SK Telecom"),
    ("329180", "HD Hyundai Heavy Industries"),
    ("010130", "Korea Zinc"),
    ("000810", "Samsung Fire & Marine Insurance"),
    ("018260", "Samsung SDS"),
    ("009150", "Samsung Electro-Mechanics"),
    ("033780", "KT&G"),
    ("011200", "HMM"),
    ("316140", "Woori Financial Group"),
    ("010950", "S-Oil"),
    ("259960", "Krafton"),
    ("011070", "LG Innotek"),
    ("024110", "Industrial Bank of Korea"),
    ("003670", "POSCO Future M"),
    ("267260", "HD Hyundai Electric"),
    ("042660", "Hanwha Ocean"),
    ("047050", "POSCO International"),
    ("034020", "Doosan Enerbility"),
    ("090430", "Amorepacific"),
    ("086280", "Hyundai Glovis"),
    ("030200", "KT"),
    ("010140", "Samsung Heavy Industries"),
    ("003490", "Korean Air"),
    ("272210", "Hanwha Systems"),
    ("377300", "KakaoPay"),
    ("402340", "SK Square"),
    ("241560", "Doosan Bobcat"),
]


@dataclass(frozen=True)
class AppConfig:
    dart_api_key: str
    newsapi_key: str
    naver_client_id: str
    naver_client_secret: str
    fmp_api_key: str
    anthropic_api_key: str
    anthropic_model: str
    smtp_user: str
    smtp_password: str
    email_from: str
    email_to: List[str]
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    sma_window: int = 30
    lookback_days: int = 140
    picks_per_market: int = 3
    health_candidate_limit: int = 20
    request_timeout: int = 20
    retry_count: int = 3
    retry_sleep_seconds: float = 2.0
    dart_call_delay_seconds: float = 0.7
    sec_contact_name: str = ""
    sec_contact_email: str = ""
    exclude_preferred_shares: bool = True
    dry_run: bool = False
    output_html_path: str = "stocks_of_the_day_email_preview.html"
    test_run: bool = False
    test_email_to: str = DEFAULT_TEST_EMAIL_TO
    test_interval_seconds: int = 60
    scheduler_enabled: bool = False
    schedule_interval_minutes: int = 1
    schedule_time_kst: str = "07:00"
    picks_db_path: str = "picks.db"

    @classmethod
    def from_env(cls) -> "AppConfig":
        load_dotenv()
        test_run = os.getenv("TEST_RUN", "false").strip().lower() in {"1", "true", "yes"}
        test_email_to = os.getenv("TEST_EMAIL_TO", DEFAULT_TEST_EMAIL_TO).strip()
        recipients = [
            item.strip()
            for item in os.getenv("EMAIL_TO", "").replace(";", ",").split(",")
            if item.strip()
        ]
        if test_run:
            recipients = [test_email_to]
        return cls(
            dart_api_key=os.getenv("DART_API_KEY", ""),
            newsapi_key=os.getenv("NEWSAPI_KEY", ""),
            naver_client_id=os.getenv("NAVER_CLIENT_ID", ""),
            naver_client_secret=os.getenv("NAVER_CLIENT_SECRET", ""),
            fmp_api_key=os.getenv("FMP_API_KEY", ""),
            anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
            anthropic_model=os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5"),
            smtp_user=os.getenv("GMAIL_USER", ""),
            smtp_password=os.getenv("GMAIL_APP_PASSWORD", ""),
            email_from=os.getenv("EMAIL_FROM", os.getenv("GMAIL_USER", "")),
            email_to=recipients,
            smtp_host=os.getenv("SMTP_HOST", "smtp.gmail.com"),
            smtp_port=int(os.getenv("SMTP_PORT", "587")),
            sma_window=int(os.getenv("SMA_WINDOW", "30")),
            lookback_days=int(os.getenv("LOOKBACK_DAYS", "140")),
            picks_per_market=int(os.getenv("PICKS_PER_MARKET", "3")),
            health_candidate_limit=int(os.getenv("HEALTH_CANDIDATE_LIMIT", "20")),
            request_timeout=int(os.getenv("REQUEST_TIMEOUT", "20")),
            retry_count=int(os.getenv("RETRY_COUNT", "3")),
            retry_sleep_seconds=float(os.getenv("RETRY_SLEEP_SECONDS", "2.0")),
            dart_call_delay_seconds=float(os.getenv("DART_CALL_DELAY_SECONDS", "0.7")),
            sec_contact_name=os.getenv("SEC_CONTACT_NAME", "").strip(),
            sec_contact_email=os.getenv("SEC_CONTACT_EMAIL", "").strip(),
            exclude_preferred_shares=os.getenv("EXCLUDE_PREFERRED_SHARES", "true").strip().lower() in {"1", "true", "yes"},
            dry_run=False if test_run else os.getenv("DRY_RUN", "false").strip().lower() in {"1", "true", "yes"},
            output_html_path=os.getenv(
                "OUTPUT_HTML_PATH",
                "stocks_of_the_day_email_preview.html",
            ),
            test_run=test_run,
            test_email_to=test_email_to,
            test_interval_seconds=int(os.getenv("TEST_INTERVAL_SECONDS", "60")),
            scheduler_enabled=os.getenv("SCHEDULER_ENABLED", "false").strip().lower() in {"1", "true", "yes"},
            schedule_interval_minutes=int(os.getenv("SCHEDULE_INTERVAL_MINUTES", "1")),
            schedule_time_kst=os.getenv("SCHEDULE_TIME_KST", "07:00"),
            picks_db_path=os.getenv("PICKS_DB_PATH", "picks.db"),
        )


@dataclass
class MarketSummaryRow:
    symbol: str
    name: str
    close: Optional[float]
    pct_change: Optional[float]
    close_date: Optional[pd.Timestamp]
    status: str
    changes: Dict[str, Optional[float]] = None
    data_source: str = "yfinance"


@dataclass
class ScreenCandidate:
    market: str
    symbol: str
    display_symbol: str
    name: str
    signal_date: pd.Timestamp
    previous_close: float
    previous_sma: float
    signal_close: float
    signal_sma: float
    signal_volume: Optional[float]


@dataclass
class HealthCheck:
    passed: bool
    free_cash_flow: Optional[float]
    net_income: Optional[float]
    source: str
    reason: str


@dataclass
class QuarterlyFinancialRow:
    fiscal_year: str
    period: str  # "FY", "Q1", "Q2", "Q3", "Q4"
    revenue: Optional[float]
    ebit: Optional[float]
    net_income: Optional[float]
    operating_cash_flow: Optional[float]
    free_cash_flow: Optional[float]


@dataclass
class StockReport:
    candidate: ScreenCandidate
    health: HealthCheck
    news_brief: List[str]
    quarterly_financials: List[QuarterlyFinancialRow]
    valuation: Dict[str, Optional[float | str]]
    projections: Dict[str, Optional[float | str]]
    news: List[Dict[str, str]]
    currency: str = "USD"
    next_earnings: Optional[str] = None
    earnings_days_away: Optional[int] = None
    financials_source: str = "yfinance"


# Rolling buffer of recent log lines, included in the failure alert email.
_LOG_BUFFER: deque = deque(maxlen=200)


class _LogBufferHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            _LOG_BUFFER.append(self.format(record))
        except Exception:  # noqa: BLE001 - logging must never raise
            pass


def tail_log_lines(count: int = 30) -> str:
    return "\n".join(list(_LOG_BUFFER)[-count:])


def configure_logging() -> None:
    log_format = "%(asctime)s | %(levelname)-8s | %(message)s"
    date_format = "%Y-%m-%d %H:%M:%S"
    logging.basicConfig(level=logging.INFO, format=log_format, datefmt=date_format)
    buffer_handler = _LogBufferHandler()
    buffer_handler.setFormatter(logging.Formatter(log_format, datefmt=date_format))
    logging.getLogger().addHandler(buffer_handler)


def today_for_data_cutoff() -> date:
    return date.today()


def retry_call(label: str, config: AppConfig, fn: Callable[[], Any]) -> Any:
    last_error: Optional[Exception] = None
    for attempt in range(1, config.retry_count + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - external APIs raise diverse exceptions
            last_error = exc
            if attempt == config.retry_count:
                break
            sleep_seconds = config.retry_sleep_seconds * attempt
            LOGGER.warning(
                "%s failed on attempt %s/%s: %s. Retrying in %.1fs.",
                label,
                attempt,
                config.retry_count,
                exc,
                sleep_seconds,
            )
            time.sleep(sleep_seconds)
    raise RuntimeError(f"{label} failed after {config.retry_count} attempts") from last_error


def raise_if_empty(frame: Any, label: str) -> Any:
    """Raise when a fetch returned None or an empty DataFrame.

    yfinance and OpenDartReader often signal transient failures by returning
    None/empty results instead of raising, which would otherwise slip past
    retry_call. Call this inside the lambda passed to retry_call so emptiness
    triggers retries.
    """
    if frame is None:
        raise RuntimeError(f"{label} returned no data")
    if isinstance(frame, pd.DataFrame) and frame.empty:
        raise RuntimeError(f"{label} returned an empty DataFrame")
    return frame


def safe_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except TypeError:
        pass
    try:
        text = str(value).replace(",", "").strip()
        match = re.search(r"[-+]?[0-9]*\.?[0-9]+(?:[eE][-+]?[0-9]+)?", text)
        if not match:
            return None
        result = float(match.group(0))
    except (TypeError, ValueError):
        return None
    if not np.isfinite(result):
        return None
    return result


def _parse_share_count_from_company_info(company_info: Dict[str, Any]) -> Optional[float]:
    if not company_info:
        return None
    patterns = [
        "stock",
        "share",
        "issued",
        "total",
        "cnt",
        "qty",
        "주식",
        "발행",
        "총주식",
        "주식수",
    ]
    for key, value in company_info.items():
        if not key or value is None:
            continue
        key_text = str(key).lower()
        if any(pattern in key_text for pattern in patterns):
            parsed = safe_float(value)
            if parsed is not None and parsed > 0:
                return parsed
    return None


def _parse_share_count_from_frame(frame: pd.DataFrame) -> Optional[float]:
    if frame is None or frame.empty:
        return None

    # Prefer explicit DART account ids for outstanding shares if available.
    candidate_account_ids = [
        "ifrs-full_IssuedShares",
        "ifrs-full_SharesOutstanding",
        "ifrs-full_ShareCapital",
        "ifrs-full_CommonStock",
        "ifrs-full_StockholdersEquity",
    ]
    for account_id in candidate_account_ids:
        shares, _, _ = DartClient.extract_three_year_amounts(frame, account_id=account_id, name_patterns=["Share", "Stock", "Issued", "Outstanding", "주식", "발행주식"])
        if shares is not None and shares > 0:
            return shares

    # Try name-based matching for share count rows.
    share_name_patterns = [
        "발행주식",
        "주식수",
        "총주식수",
        "issued shares",
        "shares outstanding",
        "common stock",
        "stock count",
        "보통주",
    ]
    shares, _, _ = DartClient.extract_three_year_amounts(frame, account_id=None, name_patterns=share_name_patterns)
    if shares is not None and shares > 0:
        return shares

    # Last resort: search matching rows with numeric amount-like columns.
    nm_col_candidates = ["account_nm", "account_nm_kor", "account_name"]
    nm_col = next((col for col in nm_col_candidates if col in frame.columns), None)
    if nm_col is None:
        return None

    for _, row in frame.iterrows():
        name_value = str(row.get(nm_col, ""))
        if any(pattern.lower() in name_value.lower() for pattern in share_name_patterns):
            for col in frame.columns:
                if not isinstance(col, str):
                    continue
                if any(token in col.lower() for token in ("amount", "amt", "qty", "cnt", "stock", "share", "number")):
                    candidate = safe_float(row.get(col))
                    if candidate is not None and candidate > 0:
                        return candidate
    return None


def extract_dart_share_count(
    dart_client: DartClient,
    stock_code: str,
    frame: Optional[pd.DataFrame],
    year: Optional[int] = None,
) -> Optional[float]:
    if dart_client is None:
        return None
    # Authoritative source first: DART's dedicated share-count report.
    if year is not None:
        try:
            shares = dart_client.shares_outstanding(stock_code, year)
            if shares is not None and shares > 0:
                return shares
        except Exception:  # noqa: BLE001
            pass
    if frame is not None and not frame.empty:
        share_count = _parse_share_count_from_frame(frame)
        if share_count is not None:
            return share_count
    try:
        company_info = dart_client.company(stock_code)
    except Exception:
        company_info = {}
    return _parse_share_count_from_company_info(company_info)


def format_number(value: Any, decimals: int = 2) -> str:
    number = safe_float(value)
    if number is None:
        return "N/A"
    abs_number = abs(number)
    # Use 5 significant figures and scale suffixes
    def _sf(v: float) -> str:
        return f"{v:.5g}"

    if abs_number >= 1_000_000_000_000:
        return f"{_sf(number / 1_000_000_000_000)}T"
    if abs_number >= 1_000_000_000:
        return f"{_sf(number / 1_000_000_000)}B"
    if abs_number >= 1_000_000:
        return f"{_sf(number / 1_000_000)}M"
    # 1,000 - 999,999: thousands separator (%.5g would switch to scientific
    # notation at 100k, which mangles e.g. KRW prices).
    if abs_number >= 1_000:
        return f"{number:,.0f}"
    return _sf(number)


def format_pct(value: Any) -> str:
    number = safe_float(value)
    return "N/A" if number is None else f"{number:+.2f}%"


def normalize_ohlcv(raw: pd.DataFrame) -> pd.DataFrame:
    if raw is None or raw.empty:
        return pd.DataFrame()
    data = raw.copy()
    if isinstance(data.columns, pd.MultiIndex):
        data.columns = data.columns.get_level_values(0)
    data.columns = [str(col).title().replace(" ", "") for col in data.columns]
    close_col = "Close" if "Close" in data.columns else "AdjClose"
    required = [close_col]
    if not all(col in data.columns for col in required):
        return pd.DataFrame()
    if close_col != "Close":
        data["Close"] = data[close_col]
    for col in ["Open", "High", "Low", "Close", "Volume"]:
        if col in data.columns:
            data[col] = pd.to_numeric(data[col], errors="coerce")
    data = data.sort_index()
    data = data[data["Close"].notna()]
    if "Volume" not in data.columns:
        data["Volume"] = np.nan
    return data


def fetch_market_summary(config: AppConfig) -> List[MarketSummaryRow]:
    LOGGER.info("Step 1/6 - Fetching market summary for %s assets.", len(MARKET_ASSETS))
    rows: List[MarketSummaryRow] = []
    end_date = today_for_data_cutoff().isoformat()
    # Ensure we have enough history for monthly/quarterly windows
    period_days = max(config.lookback_days, 130)

    def download_asset(sym: str) -> pd.DataFrame:
        label = f"Market summary download {sym}"
        return retry_call(
            label,
            config,
            lambda: raise_if_empty(
                yf.download(
                    sym,
                    period=f"{period_days}d",
                    interval="1d",
                    end=end_date,
                    auto_adjust=False,
                    progress=False,
                    threads=False,
                ),
                label,
            ),
        )

    for symbol, name in MARKET_ASSETS.items():
        display_name = name
        try:
            try:
                raw = download_asset(symbol)
            except Exception as exc:  # noqa: BLE001
                if symbol != "^GSPC":
                    raise
                # S&P 500 fallback chain: ^SPX quote, then SPY ETF as a proxy.
                LOGGER.warning("^GSPC failed after retries (%s); trying proxies.", exc)
                raw = None
                for proxy in ("^SPX", "SPY"):
                    try:
                        raw = download_asset(proxy)
                        display_name = f"{name} (proxy)"
                        LOGGER.info("S&P 500 proxy %s succeeded.", proxy)
                        break
                    except Exception as proxy_exc:  # noqa: BLE001
                        LOGGER.warning("S&P 500 proxy %s failed: %s", proxy, proxy_exc)
                if raw is None:
                    raise
            data = normalize_ohlcv(raw)
            if len(data) < 2:
                rows.append(MarketSummaryRow(symbol, display_name, None, None, None, "insufficient data"))
                LOGGER.warning("%s (%s): insufficient close history.", display_name, symbol)
                continue
            previous_close = float(data["Close"].iloc[-2])
            latest_close = float(data["Close"].iloc[-1])
            pct_change = (latest_close / previous_close - 1.0) * 100.0
            close_date = pd.Timestamp(data.index[-1])
            # compute multi-timeframe changes (trading day offsets)
            changes = compute_multi_changes(data, windows=(1, 5, 21, 63))
            rows.append(MarketSummaryRow(symbol, display_name, latest_close, pct_change, close_date, "ok", changes=changes, data_source="yfinance"))
            LOGGER.info(
                "%s (%s): close_date=%s close=%.4f previous_close=%.4f change=%+.2f%%",
                display_name,
                symbol,
                close_date.date(),
                latest_close,
                previous_close,
                pct_change,
            )
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception("%s (%s): market summary failed: %s", display_name, symbol, exc)
            rows.append(MarketSummaryRow(symbol, display_name, None, None, None, f"error: {exc}"))
    return rows


def fetch_us_ohlcv_batch(symbols: Sequence[str], config: AppConfig) -> Dict[str, pd.DataFrame]:
    LOGGER.info("Fetching US OHLCV batch: %s tickers.", len(symbols))
    end_date = today_for_data_cutoff().isoformat()
    period_days = max(config.lookback_days, 130)
    raw = retry_call(
        "US universe yfinance batch download",
        config,
        lambda: raise_if_empty(
            yf.download(
                list(symbols),
                period=f"{period_days}d",
                interval="1d",
                end=end_date,
                auto_adjust=False,
                group_by="ticker",
                progress=False,
                threads=True,
            ),
            "US universe yfinance batch download",
        ),
    )
    result: Dict[str, pd.DataFrame] = {}
    for symbol in symbols:
        try:
            if isinstance(raw.columns, pd.MultiIndex) and symbol in raw.columns.get_level_values(0):
                result[symbol] = normalize_ohlcv(raw[symbol])
            else:
                result[symbol] = normalize_ohlcv(raw)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("Unable to normalize US OHLCV for %s: %s", symbol, exc)
            result[symbol] = pd.DataFrame()
    return result


def fetch_kr_ohlcv(symbol: str, config: AppConfig) -> pd.DataFrame:
    if fdr is None:
        return pd.DataFrame()
    start = (pd.Timestamp(today_for_data_cutoff()) - pd.Timedelta(days=config.lookback_days)).strftime("%Y-%m-%d")
    end = today_for_data_cutoff().strftime("%Y-%m-%d")
    try:
        raw = fdr.DataReader(symbol, start, end)
        return normalize_ohlcv(raw)
    except Exception as exc:  # noqa: BLE001
        LOGGER.debug("Unable to fetch Korean OHLCV for %s: %s", symbol, exc)
        return pd.DataFrame()


def fetch_kr_ohlcv_batch(universe: List[Tuple[str, str]], config: AppConfig) -> Dict[str, pd.DataFrame]:
    from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
    LOGGER.info("Fetching Korean OHLCV for %s stocks in parallel.", len(universe))
    result: Dict[str, pd.DataFrame] = {}

    def _fetch(symbol: str) -> Tuple[str, pd.DataFrame]:
        return symbol, fetch_kr_ohlcv(symbol, config)

    # Allow each wait cycle up to 3x the per-request timeout before declaring tasks hung.
    per_cycle_timeout = float(config.request_timeout) * 3

    executor = ThreadPoolExecutor(max_workers=20)
    futures_map: Dict = {executor.submit(_fetch, symbol): symbol for symbol, _ in universe}
    remaining = list(futures_map)
    done_count = 0

    while remaining:
        done_set, remaining_set = wait(remaining, timeout=per_cycle_timeout, return_when=FIRST_COMPLETED)
        if not done_set:
            # No future completed within the timeout window — workers are hung (likely rate-limited).
            LOGGER.warning(
                "Korean OHLCV: %s tasks appear hung after %.0fs; skipping them.",
                len(remaining_set),
                per_cycle_timeout,
            )
            for f in remaining_set:
                result[futures_map[f]] = pd.DataFrame()
            break
        for f in done_set:
            sym = futures_map[f]
            try:
                _, data = f.result()
            except Exception as exc:  # noqa: BLE001
                LOGGER.debug("Korean OHLCV error for %s: %s", sym, exc)
                data = pd.DataFrame()
            result[sym] = data
            done_count += 1
            if done_count % 100 == 0:
                LOGGER.info("Korean OHLCV progress: %s/%s fetched.", done_count, len(universe))
        remaining = list(remaining_set)

    # Do not block on threads still running hung fdr.DataReader calls.
    executor.shutdown(wait=False)

    filled = sum(1 for d in result.values() if not d.empty)
    LOGGER.info("Korean OHLCV complete: %s/%s stocks have data.", filled, len(universe))
    return result


def detect_sma_crossovers(
    market: str,
    symbol_to_name: Dict[str, str],
    ohlcv_by_symbol: Dict[str, pd.DataFrame],
    config: AppConfig,
) -> List[ScreenCandidate]:
    LOGGER.info("Calculating %s-day SMA crossover signals for %s.", config.sma_window, market)
    candidates: List[ScreenCandidate] = []
    for symbol, data in ohlcv_by_symbol.items():
        if data.empty or len(data) < config.sma_window + 2:
            LOGGER.info("%s %s: skipped; rows=%s.", market, symbol, len(data))
            continue
        frame = data.copy()
        frame["SMA"] = frame["Close"].rolling(window=config.sma_window, min_periods=config.sma_window).mean()
        usable = frame.dropna(subset=["Close", "SMA"])
        if len(usable) < 2:
            LOGGER.info("%s %s: skipped; not enough SMA rows.", market, symbol)
            continue
        prev = usable.iloc[-2]
        signal = usable.iloc[-1]
        prev_close = float(prev["Close"])
        prev_sma = float(prev["SMA"])
        signal_close = float(signal["Close"])
        signal_sma = float(signal["SMA"])
        crossed = prev_close <= prev_sma and signal_close > signal_sma
        LOGGER.info(
            "%s %s: signal_date=%s prev_close=%.4f prev_sma=%.4f close=%.4f sma=%.4f crossed_above=%s",
            market,
            symbol,
            pd.Timestamp(usable.index[-1]).date(),
            prev_close,
            prev_sma,
            signal_close,
            signal_sma,
            crossed,
        )
        if crossed:
            candidates.append(
                ScreenCandidate(
                    market=market,
                    symbol=symbol,
                    display_symbol=symbol if market == "US" else f"{symbol}.KS",
                    name=symbol_to_name.get(symbol, symbol),
                    signal_date=pd.Timestamp(usable.index[-1]),
                    previous_close=prev_close,
                    previous_sma=prev_sma,
                    signal_close=signal_close,
                    signal_sma=signal_sma,
                    signal_volume=safe_float(signal.get("Volume")),
                )
            )
    candidates.sort(key=lambda item: (item.signal_volume or 0.0), reverse=True)
    LOGGER.info("%s: %s SMA crossover candidates found.", market, len(candidates))
    return candidates


def get_yfinance_ticker_symbol(candidate: ScreenCandidate) -> str:
    return candidate.symbol if candidate.market == "US" else f"{candidate.symbol}.KS"


def first_existing_row(frame: pd.DataFrame, row_names: Iterable[str]) -> Optional[pd.Series]:
    if frame is None or frame.empty:
        return None
    lookup = {str(idx).strip().lower(): idx for idx in frame.index}
    for name in row_names:
        idx = lookup.get(name.strip().lower())
        if idx is not None:
            return frame.loc[idx]
    return None


def latest_value_from_statement(frame: pd.DataFrame, row_names: Iterable[str]) -> Optional[float]:
    row = first_existing_row(frame, row_names)
    if row is None:
        return None
    row = pd.to_numeric(row, errors="coerce").dropna()
    if row.empty:
        return None
    return safe_float(row.iloc[0])


def fetch_yfinance_health(candidate: ScreenCandidate, config: AppConfig) -> HealthCheck:
    yf_symbol = get_yfinance_ticker_symbol(candidate)
    ticker = retry_call(f"yfinance ticker init {yf_symbol}", config, lambda: yf.Ticker(yf_symbol))
    financials = retry_call(f"yfinance financials {yf_symbol}", config, lambda: ticker.financials)
    cashflow = retry_call(f"yfinance cashflow {yf_symbol}", config, lambda: ticker.cashflow)
    net_income = latest_value_from_statement(financials, ["Net Income", "Net Income Common Stockholders"])
    fcf = latest_value_from_statement(cashflow, ["Free Cash Flow"])
    if fcf is None:
        operating_cf = latest_value_from_statement(cashflow, ["Operating Cash Flow", "Total Cash From Operating Activities"])
        capex = latest_value_from_statement(cashflow, ["Capital Expenditure", "Capital Expenditures"])
        if operating_cf is not None and capex is not None:
            fcf = operating_cf + capex if capex < 0 else operating_cf - capex
    passed = bool((fcf is not None and fcf > 0) and (net_income is not None and net_income > 0))
    reason = "passed" if passed else "FCF and/or net income is not positive or unavailable"
    return HealthCheck(passed, fcf, net_income, f"yfinance:{yf_symbol}", reason)


def fetch_dart_latest_financials(symbol: str, config: AppConfig) -> Tuple[Optional[float], Optional[float]]:
    dart_client = get_dart_client(config)
    if dart_client is None:
        return None, None
    current_year = today_for_data_cutoff().year
    # Try consolidated first, then separate
    for fs_div in ("CFS", "OFS"):
        try:
            frame = retry_call(
                f"DART finstate_all {symbol} {current_year} fs_div={fs_div}",
                config,
                lambda fs_div=fs_div: dart_client.finstate_all(symbol, current_year, reprt_code="11011", fs_div=fs_div),
            )
            if frame is None or frame.empty:
                LOGGER.debug("DART %s %s fs_div=%s: empty response", symbol, current_year, fs_div)
                continue

            # Log available account ids/names (first few) for diagnostics
            try:
                sample = list(frame.get("account_id", frame.get("account_nm", pd.Series(dtype=str))).astype(str).unique()[:12])
                LOGGER.info("DART accounts sample for %s (fs_div=%s): %s", symbol, fs_div, sample)
            except Exception:
                pass

            # Extract net income, operating CF, capex using robust id-first strategy
            net_income_cur, _, _ = dart_client.extract_three_year_amounts(
                frame,
                account_id="ifrs-full_ProfitLoss",
                name_patterns=["당기순이익", "Net income", "Profit loss"],
                sj_div_priority=("CIS", "IS"),
            )
            ocf_cur, _, _ = dart_client.extract_three_year_amounts(
                frame,
                account_id="ifrs-full_CashFlowsFromUsedInOperatingActivities",
                name_patterns=["영업활동.*현금흐름", "Operating cash"],
                sj_div_priority=("CF",),
            )
            capex_cur, _, _ = dart_client.extract_three_year_amounts(
                frame,
                account_id=None,
                name_patterns=_DART_CAPEX_PATTERNS,
                sj_div_priority=("CF",),
            )

            fcf = None
            if ocf_cur is not None and capex_cur is not None:
                fcf = ocf_cur - abs(capex_cur)

            LOGGER.info(
                "DART %s %s (fs_div=%s): net_income=%s ocf=%s capex=%s fcf=%s",
                symbol,
                current_year,
                fs_div,
                format_number(net_income_cur, 0),
                format_number(ocf_cur, 0),
                format_number(capex_cur, 0),
                format_number(fcf, 0),
            )

            # Basic validation: Assets ≈ Liabilities + Equity
            try:
                assets = dart_client.extract_three_year_amounts(frame, account_id="ifrs-full_Assets", name_patterns=["자산총계", "Assets"], sj_div_priority=("BS",))[0]
                liabilities = dart_client.extract_three_year_amounts(frame, account_id="ifrs-full_Liabilities", name_patterns=["부채총계", "Liabilities"], sj_div_priority=("BS",))[0]
                equity = dart_client.extract_three_year_amounts(frame, account_id="ifrs-full_Equity", name_patterns=["자본총계", "Equity"], sj_div_priority=("BS",))[0]
                if assets is not None and liabilities is not None and equity is not None:
                    if abs(assets - (liabilities + equity)) > max(1.0, abs(assets) * 0.01):
                        LOGGER.warning(
                            "DART validation failed %s %s: Assets %.0f != Liabilities+Equity %.0f",
                            symbol,
                            current_year,
                            assets,
                            (liabilities + equity),
                        )
            except Exception:
                LOGGER.debug("DART balance sheet validation skipped for %s", symbol)

            if net_income_cur is not None or fcf is not None:
                return fcf, net_income_cur
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("DART financial lookup failed for %s fs_div=%s: %s", symbol, fs_div, exc)
    return None, None


def verify_financial_health(candidate: ScreenCandidate, config: AppConfig) -> HealthCheck:
    LOGGER.info(
        "Step 3/6 - Cash flow verification for %s %s (%s).",
        candidate.market,
        candidate.display_symbol,
        candidate.name,
    )
    if candidate.market == "KR":
        dart_fcf, dart_net_income = fetch_dart_latest_financials(candidate.symbol, config)
        if dart_fcf is not None or dart_net_income is not None:
            passed = bool((dart_fcf is not None and dart_fcf > 0) and (dart_net_income is not None and dart_net_income > 0))
            health = HealthCheck(
                passed=passed,
                free_cash_flow=dart_fcf,
                net_income=dart_net_income,
                source="DART",
                reason="passed" if passed else "DART FCF and/or net income failed the positive threshold",
            )
        else:
            health = fetch_yfinance_health(candidate, config)
    else:
        health = fetch_yfinance_health(candidate, config)
    LOGGER.info(
        "%s %s cash flow verification result: passed=%s fcf=%s net_income=%s source=%s reason=%s",
        candidate.market,
        candidate.display_symbol,
        health.passed,
        format_number(health.free_cash_flow, 0),
        format_number(health.net_income, 0),
        health.source,
        health.reason,
    )
    return health


def select_quality_candidates(
    candidates: List[ScreenCandidate],
    config: AppConfig,
) -> List[Tuple[ScreenCandidate, HealthCheck]]:
    selected: List[Tuple[ScreenCandidate, HealthCheck]] = []
    for candidate in candidates[: config.health_candidate_limit]:
        try:
            health = verify_financial_health(candidate, config)
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception("Health check failed for %s: %s", candidate.display_symbol, exc)
            continue
        if health.passed:
            selected.append((candidate, health))
            LOGGER.info(
                "%s selected as quality candidate %s/%s.",
                candidate.display_symbol,
                len(selected),
                config.picks_per_market,
            )
        else:
            LOGGER.info("%s dropped after quality control.", candidate.display_symbol)
        if len(selected) >= config.picks_per_market:
            break
    while len(selected) < config.picks_per_market:
        LOGGER.info("No additional quality candidate available; padding output with N/A.")
        break
    return selected


def fmp_get(path: str, params: Dict[str, Any], config: AppConfig) -> Any:
    if not config.fmp_api_key:
        return None
    params = dict(params)
    params["apikey"] = config.fmp_api_key
    url = f"https://financialmodelingprep.com/api/{path.lstrip('/')}"
    response = retry_call(
        f"FMP GET {path}",
        config,
        lambda: requests.get(url, params=params, timeout=config.request_timeout),
    )
    if response.status_code in {401, 403, 429}:
        LOGGER.warning("FMP %s returned status %s: %s", path, response.status_code, response.text[:200])
        return None
    response.raise_for_status()
    return response.json()


def get_ticker_info(yf_symbol: str, config: AppConfig) -> Dict[str, Any]:
    try:
        ticker = yf.Ticker(yf_symbol)
        info = retry_call(f"yfinance info {yf_symbol}", config, lambda: ticker.get_info())
        return info or {}
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("yfinance info failed for %s: %s", yf_symbol, exc)
        return {}


def fetch_news_brief(candidate: ScreenCandidate, news: List[Dict[str, str]], config: AppConfig) -> List[str]:
    """Ask Claude (with web search) for 3 short bullets on the last 30 days of company news.

    Returns a list of bullet strings. Empty list means the brief is unavailable;
    the renderer decides how to degrade (missing key notice or raw headlines).
    """
    if not config.anthropic_api_key:
        LOGGER.warning("ANTHROPIC_API_KEY missing; news brief skipped for %s.", candidate.display_symbol)
        return []
    if anthropic is None:
        LOGGER.warning("anthropic package not installed; news brief skipped for %s.", candidate.display_symbol)
        return []

    exchange = "NYSE/NASDAQ (US)" if candidate.market == "US" else "KRX (Korea Exchange, KOSPI)"
    prompt = (
        f"The stock of {candidate.name}, listed on {exchange} with ticker {candidate.display_symbol}, "
        f"just closed above its 30-day moving average on {candidate.signal_date.date().isoformat()} "
        "after trading below it. Research news from the last 30 days about this exact listed company "
        "(not another company with a similar name) to explain what likely drove this price strength: "
        "earnings, guidance, deals, buybacks, regulatory actions, sector tailwinds, or other price-moving events.\n\n"
        "Return UP TO 3 bullet points in English, most price-relevant first. Each bullet must be "
        "15 words or fewer and state a concrete fact about the company or its sector. "
        "Base bullets ONLY on information retrieved from the web search; never invent events. "
        "If you find fewer than 3 relevant items, return fewer bullets - do not pad with weak or "
        "unrelated items. If nothing relevant is found, return the single bullet: "
        "\"- No significant news found in the past month\".\n\n"
        "Output ONLY the bullet lines, one per line, each starting with \"- \". "
        "No preamble, no description of your search process, no summary sentence."
    )

    def call_api() -> Any:
        client = anthropic.Anthropic(api_key=config.anthropic_api_key)
        messages: List[Dict[str, Any]] = [{"role": "user", "content": prompt}]
        tools = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}]
        response = client.messages.create(
            model=config.anthropic_model,
            max_tokens=1024,
            tools=tools,
            messages=messages,
        )
        # Server-side web search can pause the turn; resume until finished.
        continuations = 0
        while response.stop_reason == "pause_turn" and continuations < 3:
            messages = [
                {"role": "user", "content": prompt},
                {"role": "assistant", "content": response.content},
            ]
            response = client.messages.create(
                model=config.anthropic_model,
                max_tokens=1024,
                tools=tools,
                messages=messages,
            )
            continuations += 1
        return response

    try:
        response = retry_call(f"Anthropic news brief {candidate.display_symbol}", config, call_api)
    except Exception as exc:  # noqa: BLE001
        # Full repr so the root cause (invalid key / model not found / no
        # credits) is visible in the log, not just a generic message.
        LOGGER.warning(
            "News brief failed for %s; degrading to raw headlines: %r",
            candidate.display_symbol,
            exc,
        )
        return []

    # Citations split the reply into multiple contiguous text blocks, often
    # mid-line (the "- " marker in one block, the sentence in the next), so
    # they must be concatenated without a separator to reconstruct the lines.
    text = "".join(block.text for block in response.content if getattr(block, "type", None) == "text")
    # Keep only lines that are formatted as bullets; anything else (search
    # narration, preambles, summaries) is meta text that must not reach the email.
    bullets: List[str] = []
    for line in text.splitlines():
        line = line.strip()
        match = re.match(r"^(?:[-*•]|\d+[.)])\s+(.*)", line)
        if match and match.group(1).strip():
            bullets.append(match.group(1).strip())
    bullets = bullets[:3]
    # The no-news sentinel only makes sense alone; drop it if real bullets exist.
    if len(bullets) > 1:
        bullets = [b for b in bullets if not b.lower().startswith("no significant news")] or bullets[:1]
    LOGGER.info("News brief for %s: %s bullet(s).", candidate.display_symbol, len(bullets))
    return bullets


def fetch_next_earnings(candidate: ScreenCandidate, config: AppConfig) -> Tuple[Optional[str], Optional[int]]:
    """Return (ISO date string, days away) of the next earnings date, or (None, None)."""
    yf_symbol = get_yfinance_ticker_symbol(candidate)
    today = today_for_data_cutoff()
    dates: List[Any] = []
    try:
        ticker = yf.Ticker(yf_symbol)
        try:
            cal = ticker.calendar
            if isinstance(cal, dict):
                dates.extend(cal.get("Earnings Date") or [])
            elif isinstance(cal, pd.DataFrame) and not cal.empty and "Earnings Date" in cal.index:
                dates.extend(list(cal.loc["Earnings Date"].dropna()))
        except Exception:  # noqa: BLE001
            pass
        if not dates:
            try:
                earnings_df = ticker.get_earnings_dates(limit=8)
                if earnings_df is not None and not earnings_df.empty:
                    dates.extend(list(earnings_df.index))
            except Exception:  # noqa: BLE001
                pass
    except Exception as exc:  # noqa: BLE001
        LOGGER.debug("Earnings date lookup failed for %s: %s", candidate.display_symbol, exc)
        return None, None
    future = sorted(
        d for d in (pd.Timestamp(item).date() for item in dates if item is not None)
        if d >= today
    )
    if not future:
        LOGGER.info("No upcoming earnings date found for %s.", candidate.display_symbol)
        return None, None
    next_date = future[0]
    days_away = (next_date - today).days
    LOGGER.info("%s next earnings: %s (%s days away).", candidate.display_symbol, next_date, days_away)
    return next_date.isoformat(), days_away


def statement_row_value(frame: pd.DataFrame, names: Iterable[str], col: Any) -> Optional[float]:
    row = first_existing_row(frame, names)
    if row is None or col not in row.index:
        return None
    return safe_float(row[col])


def _sub_or_none(a: Optional[float], b: Optional[float]) -> Optional[float]:
    if a is None or b is None:
        return None
    return a - b


def _fcf_from(ocf: Optional[float], capex: Optional[float]) -> Optional[float]:
    if ocf is None or capex is None:
        return None
    return ocf - abs(capex)


# Metric names as they appear in yfinance income/cashflow statements.
_YF_INCOME_ROWS = {
    "revenue": ["Total Revenue", "Revenue"],
    "ebit": ["EBIT", "Operating Income"],
    "net_income": ["Net Income", "Net Income Common Stockholders"],
}
_YF_CASHFLOW_ROWS = {
    "ocf": ["Operating Cash Flow", "Total Cash From Operating Activities"],
    "capex": ["Capital Expenditure", "Capital Expenditures"],
}


def _yf_metrics_for_column(financials: Optional[pd.DataFrame], cashflow: Optional[pd.DataFrame], col: Any) -> Dict[str, Optional[float]]:
    metrics: Dict[str, Optional[float]] = {"revenue": None, "ebit": None, "net_income": None, "ocf": None, "capex": None}
    if financials is not None and not financials.empty and col in financials.columns:
        for key, names in _YF_INCOME_ROWS.items():
            metrics[key] = statement_row_value(financials, names, col)
    if cashflow is not None and not cashflow.empty and col in cashflow.columns:
        for key, names in _YF_CASHFLOW_ROWS.items():
            metrics[key] = statement_row_value(cashflow, names, col)
    return metrics


def _fetch_us_quarterly_financials(candidate: ScreenCandidate, config: AppConfig) -> List[QuarterlyFinancialRow]:
    yf_symbol = get_yfinance_ticker_symbol(candidate)
    try:
        ticker = yf.Ticker(yf_symbol)
        financials = retry_call(f"yfinance annual financials {yf_symbol}", config, lambda: ticker.financials)
        cashflow = retry_call(f"yfinance annual cashflow {yf_symbol}", config, lambda: ticker.cashflow)
        q_financials = retry_call(f"yfinance quarterly financials {yf_symbol}", config, lambda: ticker.quarterly_financials)
        q_cashflow = retry_call(f"yfinance quarterly cashflow {yf_symbol}", config, lambda: ticker.quarterly_cashflow)
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("Quarterly financials unavailable for %s: %s", candidate.display_symbol, exc)
        return []
    if financials is None or financials.empty:
        return []

    quarter_cols = []
    if q_financials is not None and not q_financials.empty:
        quarter_cols = [pd.Timestamp(c) for c in q_financials.columns]
    elif q_cashflow is not None and not q_cashflow.empty:
        quarter_cols = [pd.Timestamp(c) for c in q_cashflow.columns]

    def find_quarter_col(target_end: pd.Timestamp) -> Optional[pd.Timestamp]:
        for c in quarter_cols:
            if abs((c - target_end).days) <= 45:
                return c
        return None

    rows: List[QuarterlyFinancialRow] = []
    for col in list(financials.columns[:3]):
        fy_end = pd.Timestamp(col)
        fiscal_year = str(fy_end.year)
        fy = _yf_metrics_for_column(financials, cashflow, col)
        rows.append(QuarterlyFinancialRow(
            fiscal_year, "FY",
            fy["revenue"], fy["ebit"], fy["net_income"], fy["ocf"], _fcf_from(fy["ocf"], fy["capex"]),
        ))

        quarters: Dict[int, Dict[str, Optional[float]]] = {}
        for q in (1, 2, 3, 4):
            # yfinance only exposes ~5 recent quarters; older ones stay None -> N/A.
            target_end = fy_end - pd.DateOffset(months=3 * (4 - q))
            q_col = find_quarter_col(pd.Timestamp(target_end))
            if q_col is not None:
                quarters[q] = _yf_metrics_for_column(q_financials, q_cashflow, q_col)
            else:
                quarters[q] = {"revenue": None, "ebit": None, "net_income": None, "ocf": None, "capex": None}

        # Derive Q4 = FY - Q1 - Q2 - Q3 when Q4 is not directly available.
        for key in ("revenue", "ebit", "net_income", "ocf", "capex"):
            if quarters[4][key] is None and fy[key] is not None and all(quarters[q][key] is not None for q in (1, 2, 3)):
                quarters[4][key] = fy[key] - quarters[1][key] - quarters[2][key] - quarters[3][key]

        for q in (1, 2, 3, 4):
            m = quarters[q]
            rows.append(QuarterlyFinancialRow(
                fiscal_year, f"Q{q}",
                m["revenue"], m["ebit"], m["net_income"], m["ocf"], _fcf_from(m["ocf"], m["capex"]),
            ))
    return rows


# SEC XBRL us-gaap concept fallbacks per metric, tried in order.
_SEC_CONCEPTS: Dict[str, List[str]] = {
    "revenue": ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet"],
    "ebit": ["OperatingIncomeLoss"],
    "net_income": ["NetIncomeLoss", "ProfitLoss"],
    "ocf": ["NetCashProvidedByUsedInOperatingActivities"],
    "capex": ["PaymentsToAcquirePropertyPlantAndEquipment"],
}


def _sec_metric_periods(
    entries: List[Dict[str, Any]],
) -> Tuple[Dict[Tuple[int, str], float], Dict[int, float], Dict[Tuple[int, int], float]]:
    """Classify SEC fact entries into (quarters, annuals, ytd) keyed by fiscal year.

    Returns:
      quarters: {(fy, "Q1".."Q4"): value} from standalone ~3-month entries.
      annual:   {fy: value} from ~12-month 10-K entries.
      ytd:      {(fy, 2|3): value} from 6/9-month year-to-date entries
                (cash-flow facts after Q1 only exist as year-to-date spans).

    Multiple entries can cover the same (start, end) period: the original
    filing plus restated comparatives in later reports. The value is taken
    from the most recently filed entry (restatements win), but the fy/fp
    labels from the ORIGINAL filing — later filings stamp their own fy/fp
    onto comparative periods, which would mis-assign the fiscal year.
    """
    groups: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for entry in entries:
        groups.setdefault((str(entry["start"]), str(entry["end"])), []).append(entry)

    quarters: Dict[Tuple[int, str], float] = {}
    annual: Dict[int, float] = {}
    ytd: Dict[Tuple[int, int], float] = {}
    for (start, end), group in groups.items():
        ordered = sorted(group, key=lambda e: (str(e.get("filed") or ""), str(e.get("accn") or "")))
        value = safe_float(ordered[-1]["val"])
        label = ordered[0]
        fp = str(label.get("fp") or "")
        try:
            fiscal_year = int(label.get("fy"))
            days = (pd.Timestamp(end) - pd.Timestamp(start)).days
        except (TypeError, ValueError):
            continue
        if value is None:
            continue
        # Duration windows are wide enough for 52/53-week fiscal calendars
        # (13-14 week quarters, 364/371-day years).
        if 330 <= days <= 400:
            if fp == "FY":
                annual[fiscal_year] = value
        elif 65 <= days <= 115:
            if fp in ("Q1", "Q2", "Q3"):
                quarters[(fiscal_year, fp)] = value
            elif fp in ("Q4", "FY"):
                # A standalone 3-month entry inside a 10-K is the Q4 figure.
                quarters[(fiscal_year, "Q4")] = value
        elif 155 <= days <= 205:
            if fp == "Q2":
                ytd[(fiscal_year, 2)] = value
        elif 245 <= days <= 295:
            if fp == "Q3":
                ytd[(fiscal_year, 3)] = value
    return quarters, annual, ytd


def _fetch_us_quarterly_financials_sec(candidate: ScreenCandidate, config: AppConfig) -> List[QuarterlyFinancialRow]:
    sec_client = get_sec_client(config)
    if sec_client is None:
        LOGGER.info("SEC EDGAR contact not configured (SEC_CONTACT_EMAIL); skipping for %s.", candidate.display_symbol)
        return []
    try:
        cik = retry_call(
            f"SEC CIK lookup {candidate.symbol}",
            config,
            lambda: sec_client.ticker_to_cik(candidate.symbol),
        )
        if not cik:
            LOGGER.info("SEC EDGAR: no CIK found for ticker %s.", candidate.symbol)
            return []
        facts = retry_call(
            f"SEC companyfacts {candidate.symbol} (CIK{cik})",
            config,
            lambda: sec_client.companyfacts(cik),
        )
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("SEC EDGAR fetch failed for %s: %s", candidate.display_symbol, exc)
        return []

    metric_keys = ("revenue", "ebit", "net_income", "ocf", "capex")
    quarters_by_metric: Dict[str, Dict[Tuple[int, str], float]] = {}
    annual_by_metric: Dict[str, Dict[int, float]] = {}
    ytd_by_metric: Dict[str, Dict[Tuple[int, int], float]] = {}
    for key in metric_keys:
        # Merge per period across the concept fallbacks instead of taking the
        # first concept that exists: companies switch concepts over time and
        # can keep a stale one around (e.g. AAPL's "Revenues" stops at FY2018
        # while recent years live in RevenueFromContractWithCustomer...).
        # Lower-priority concepts are applied first so earlier-listed ones
        # overwrite on conflicting periods.
        quarters_by_metric[key], annual_by_metric[key], ytd_by_metric[key] = {}, {}, {}
        for concept in reversed(_SEC_CONCEPTS[key]):
            entries = sec_client.extract_quarterly(facts, [concept])
            quarters, annual, ytd = _sec_metric_periods(entries)
            quarters_by_metric[key].update(quarters)
            annual_by_metric[key].update(annual)
            ytd_by_metric[key].update(ytd)

    # 3 most recent fiscal years with a filed annual (10-K) value.
    fiscal_years = sorted({fy for annuals in annual_by_metric.values() for fy in annuals}, reverse=True)[:3]
    if not fiscal_years:
        LOGGER.info("SEC EDGAR: no annual facts found for %s.", candidate.display_symbol)
        return []

    rows: List[QuarterlyFinancialRow] = []
    for year in fiscal_years:
        fy = {key: annual_by_metric[key].get(year) for key in metric_keys}
        quarters: Dict[int, Dict[str, Optional[float]]] = {
            q: {key: quarters_by_metric[key].get((year, f"Q{q}")) for key in metric_keys}
            for q in (1, 2, 3, 4)
        }

        # 10-Q income-statement facts are standalone 3-month values, but
        # cash-flow facts after Q1 only exist as year-to-date spans; recover
        # standalone quarters from consecutive year-to-date entries.
        for key in ("ocf", "capex"):
            ytd = ytd_by_metric[key]
            if quarters[2][key] is None:
                quarters[2][key] = _sub_or_none(ytd.get((year, 2)), quarters[1][key])
            if quarters[3][key] is None:
                quarters[3][key] = _sub_or_none(ytd.get((year, 3)), ytd.get((year, 2)))
            if quarters[4][key] is None:
                quarters[4][key] = _sub_or_none(fy[key], ytd.get((year, 3)))

        # Derive Q4 = FY - Q1 - Q2 - Q3 when no direct Q4 entry exists.
        for key in metric_keys:
            if quarters[4][key] is None and fy[key] is not None and all(quarters[q][key] is not None for q in (1, 2, 3)):
                quarters[4][key] = fy[key] - quarters[1][key] - quarters[2][key] - quarters[3][key]

        periods: List[Tuple[str, Dict[str, Optional[float]]]] = [("FY", fy)] + [
            (f"Q{q}", quarters[q]) for q in (1, 2, 3, 4)
        ]
        for period, metrics in periods:
            revenue = metrics.get("revenue")
            net_income = metrics.get("net_income")
            # NI far above revenue usually means a wrong concept was matched
            # (cumulative span or unit mismatch), not real profitability.
            if revenue is not None and net_income is not None and revenue > 0 and net_income > revenue * 2:
                LOGGER.warning(
                    "SEC EDGAR implausible values for %s %s %s: net_income=%s > 2x revenue=%s",
                    candidate.display_symbol,
                    year,
                    period,
                    net_income,
                    revenue,
                )
            rows.append(QuarterlyFinancialRow(
                str(year), period,
                revenue, metrics.get("ebit"), net_income, metrics.get("ocf"),
                _fcf_from(metrics.get("ocf"), metrics.get("capex")),
            ))
    return rows


# DART reprt_code mapping: 11013=Q1, 11012=half-year, 11014=Q3 cumulative, 11011=annual.
_DART_REPORT_CODES = {"Q1": "11013", "H1": "11012", "Q3C": "11014", "FY": "11011"}

# Capex must match PP&E acquisitions only; broader patterns (bare "취득")
# match unrelated cash-flow rows such as 단기금융상품의 취득 or 무형자산의 취득.
_DART_CAPEX_PATTERNS = ["유형자산의 취득", "유형자산의취득", "유형자산 취득", "Purchase of property"]


def _dart_extract_metrics(
    dart_client: DartClient,
    frame: Optional[pd.DataFrame],
    prefer_cumulative: bool = False,
) -> Dict[str, Optional[float]]:
    metrics: Dict[str, Optional[float]] = {"revenue": None, "ebit": None, "net_income": None, "ocf": None, "capex": None}
    if frame is None or frame.empty:
        return metrics
    # sj_div priority keeps income metrics out of the equity-changes statement
    # and cash metrics inside the cash-flow statement. Capex patterns are
    # PP&E-only on purpose: a bare "취득" would also match e.g.
    # "단기금융상품의 취득" and silently corrupt FCF - no generic fallback.
    specs = {
        "revenue": ("ifrs-full_Revenue", ["매출액", r"수익\(매출액\)", "영업수익", "Revenue"], ("CIS", "IS")),
        "ebit": ("ifrs-full_ProfitLossFromOperatingActivities", ["영업이익", "Operating income"], ("CIS", "IS")),
        "net_income": ("ifrs-full_ProfitLoss", ["당기순이익", "Net income"], ("CIS", "IS")),
        "ocf": ("ifrs-full_CashFlowsFromUsedInOperatingActivities", ["영업활동.*현금흐름", "Operating cash"], ("CF",)),
        "capex": (None, _DART_CAPEX_PATTERNS, ("CF",)),
    }
    # Interim income-statement rows put the standalone 3-month figure in
    # thstrm_amount and the cumulative one in thstrm_add_amount; the quarterly
    # subtraction logic needs cumulative values everywhere.
    amount_cols = ("thstrm_add_amount", "thstrm_amount") if prefer_cumulative else ("thstrm_amount",)
    for key, (account_id, patterns, sj_priority) in specs.items():
        value, _, _ = dart_client.extract_three_year_amounts(
            frame, account_id=account_id, name_patterns=patterns, amount_cols=amount_cols,
            sj_div_priority=sj_priority,
        )
        metrics[key] = value
    return metrics


def _fetch_kr_quarterly_financials(candidate: ScreenCandidate, config: AppConfig) -> List[QuarterlyFinancialRow]:
    dart_client = get_dart_client(config)
    if dart_client is None:
        return []

    def fetch_report(year: int, reprt_code: str, fs_div: str) -> Optional[pd.DataFrame]:
        label = f"DART finstate_all {candidate.symbol} {year} reprt={reprt_code} fs_div={fs_div}"
        try:
            return retry_call(
                label,
                config,
                lambda: raise_if_empty(
                    dart_client.finstate_all(candidate.symbol, year, reprt_code=reprt_code, fs_div=fs_div),
                    label,
                ),
            )
        except Exception as exc:  # noqa: BLE001
            LOGGER.debug("DART report %s %s/%s fs_div=%s failed after retries: %s", candidate.symbol, year, reprt_code, fs_div, exc)
            return None

    # Determine the 3 most recent fiscal years that have an annual report filed.
    current_year = today_for_data_cutoff().year
    rows: List[QuarterlyFinancialRow] = []
    years_done = 0
    for year in range(current_year, current_year - 6, -1):
        if years_done >= 3:
            break
        cumulative: Dict[str, Dict[str, Optional[float]]] = {}
        used_fs_div = None
        for fs_div in ("CFS", "OFS"):
            annual_frame = fetch_report(year, _DART_REPORT_CODES["FY"], fs_div)
            if annual_frame is None or annual_frame.empty:
                continue
            used_fs_div = fs_div
            cumulative["FY"] = _dart_extract_metrics(dart_client, annual_frame)
            for label in ("Q1", "H1", "Q3C"):
                frame = fetch_report(year, _DART_REPORT_CODES[label], fs_div)
                cumulative[label] = _dart_extract_metrics(dart_client, frame, prefer_cumulative=True)
            break
        if used_fs_div is None:
            continue

        fy = cumulative["FY"]
        q1 = cumulative.get("Q1", {})
        h1 = cumulative.get("H1", {})
        q3c = cumulative.get("Q3C", {})

        # DART interim reports are cumulative; derive standalone quarters by
        # subtraction (Q2 = half - Q1, Q3 = Q3-cumulative - half, Q4 = annual - Q3-cumulative).
        periods: Dict[str, Dict[str, Optional[float]]] = {"FY": fy, "Q1": q1, "Q2": {}, "Q3": {}, "Q4": {}}
        for key in ("revenue", "ebit", "net_income", "ocf", "capex"):
            periods["Q2"][key] = _sub_or_none(h1.get(key), q1.get(key))
            periods["Q3"][key] = _sub_or_none(q3c.get(key), h1.get(key))
            periods["Q4"][key] = _sub_or_none(fy.get(key), q3c.get(key))

        for period in ("FY", "Q1", "Q2", "Q3", "Q4"):
            m = periods[period]
            rows.append(QuarterlyFinancialRow(
                str(year), period,
                m.get("revenue"), m.get("ebit"), m.get("net_income"), m.get("ocf"),
                _fcf_from(m.get("ocf"), m.get("capex")),
            ))
        years_done += 1
    return rows


def fetch_quarterly_financials(candidate: ScreenCandidate, config: AppConfig) -> Tuple[List[QuarterlyFinancialRow], str]:
    """Return (rows, source) where source is the provider that supplied the rows."""
    LOGGER.info("Step 4/6 - Fetching quarterly financials for %s.", candidate.display_symbol)
    if candidate.market == "KR":
        rows = _fetch_kr_quarterly_financials(candidate, config)
        if rows:
            LOGGER.info("Quarterly financials for %s sourced from DART (%s rows).", candidate.display_symbol, len(rows))
            return rows, "DART"
        LOGGER.info("DART returned no quarterly data for %s; falling back to yfinance.", candidate.display_symbol)
    else:
        try:
            rows = _fetch_us_quarterly_financials_sec(candidate, config)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("SEC EDGAR quarterly financials failed for %s: %s", candidate.display_symbol, exc)
            rows = []
        if rows:
            LOGGER.info("Quarterly financials for %s sourced from SEC EDGAR (%s rows).", candidate.display_symbol, len(rows))
            return rows, "SEC EDGAR"
        LOGGER.info("SEC EDGAR returned no quarterly data for %s; falling back to yfinance.", candidate.display_symbol)
    rows = _fetch_us_quarterly_financials(candidate, config)
    LOGGER.info("Quarterly financials for %s sourced from yfinance (%s rows).", candidate.display_symbol, len(rows))
    return rows, "yfinance"


def fetch_valuation_and_projections(candidate: ScreenCandidate, config: AppConfig) -> Tuple[Dict[str, Optional[float | str]], Dict[str, Optional[float | str]]]:
    yf_symbol = get_yfinance_ticker_symbol(candidate)
    info = get_ticker_info(yf_symbol, config)
    valuation: Dict[str, Optional[float | str]] = {
        "Current P/E": safe_float(info.get("trailingPE")),
        "Forward P/E": safe_float(info.get("forwardPE")),
        "P/B": safe_float(info.get("priceToBook")),
        "Consensus Price Target": safe_float(info.get("targetMeanPrice")),
        "Currency": info.get("currency"),
    }
    projections: Dict[str, Optional[float | str]] = {
        "Forward EPS": safe_float(info.get("forwardEps")),
        "Revenue Growth": safe_float(info.get("revenueGrowth")),
        "Earnings Growth": safe_float(info.get("earningsGrowth")),
        "Analyst Count": safe_float(info.get("numberOfAnalystOpinions")),
    }
    # Attempt to compute DART-derived EPS for KR stocks when possible.
    # The current year's annual report is not filed until the following March,
    # so look for the latest FILED fiscal year instead. The DartClient cache
    # means the frame fetched by the quarterly section is reused, not refetched.
    if candidate.market == "KR":
        dart_client = get_dart_client(config)
        if dart_client is not None:
            try:
                current_year = today_for_data_cutoff().year
                frame = None
                filed_year: Optional[int] = None
                for year in (current_year - 1, current_year - 2):
                    for fs_div in ("CFS", "OFS"):
                        try:
                            candidate_frame = dart_client.finstate_all(
                                candidate.symbol, year, reprt_code="11011", fs_div=fs_div
                            )
                        except Exception as exc:  # noqa: BLE001
                            LOGGER.debug("DART EPS lookup %s %s %s failed: %s", candidate.symbol, year, fs_div, exc)
                            continue
                        if candidate_frame is not None and not candidate_frame.empty:
                            frame = candidate_frame
                            filed_year = year
                            break
                    if frame is not None:
                        break
                if frame is not None:
                    # Prefer net income attributable to owners of the parent.
                    net_income_owners, _, _ = dart_client.extract_three_year_amounts(
                        frame,
                        account_id="ifrs-full_ProfitLossAttributableToOwnersOfParent",
                        name_patterns=["지배기업 소유주지분", "지배기업의 소유주"],
                        sj_div_priority=("CIS", "IS"),
                    )
                    if net_income_owners is None:
                        net_income_owners, _, _ = dart_client.extract_three_year_amounts(
                            frame,
                            account_id="ifrs-full_ProfitLoss",
                            name_patterns=["당기순이익", "Net income"],
                            sj_div_priority=("CIS", "IS"),
                        )
                    shares = extract_dart_share_count(dart_client, candidate.symbol, frame, year=filed_year)
                    if net_income_owners is not None and shares is not None and shares > 0:
                        eps = net_income_owners / shares
                        valuation["DART EPS (derived)"] = eps
                        if valuation.get("Current P/E") is None and eps > 0:
                            # candidate.signal_close is the latest close from
                            # the already-fetched KR OHLCV.
                            valuation["Current P/E"] = candidate.signal_close / eps
                            valuation["_pe_source"] = f"Current P/E DART-derived (FY{filed_year})"
                            LOGGER.info(
                                "%s Current P/E derived from DART FY%s: close=%s eps=%s pe=%.2f",
                                candidate.display_symbol,
                                filed_year,
                                format_number(candidate.signal_close),
                                format_number(eps),
                                valuation["Current P/E"],
                            )
            except Exception:
                LOGGER.debug("DART-derived EPS unavailable for %s", candidate.symbol)
    if config.fmp_api_key and candidate.market == "US":
        try:
            estimates = fmp_get(f"v3/analyst-estimates/{candidate.symbol}", {"limit": 1}, config)
            if isinstance(estimates, list) and estimates:
                estimate = estimates[0]
                projections["FMP Estimated EPS Avg"] = safe_float(estimate.get("estimatedEpsAvg"))
                projections["FMP Estimated Revenue Avg"] = safe_float(estimate.get("estimatedRevenueAvg"))
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("FMP projections failed for %s: %s", candidate.symbol, exc)
    LOGGER.info(
        "%s valuation: PE=%s PB=%s target=%s projections=%s",
        candidate.display_symbol,
        format_number(valuation["Current P/E"]),
        format_number(valuation["P/B"]),
        format_number(valuation["Consensus Price Target"]),
        projections,
    )
    return valuation, projections


def strip_html_tags(value: str) -> str:
    return re.sub(r"<[^>]+>", "", value or "")


# KRX preferred-share name suffixes: 우, 우B, 2우B, 3우B, 우선주.
_KR_PREFERRED_NAME_RE = re.compile(r"(?:\d?우B?|우선주)$")


def strip_preferred_suffix(name: str) -> str:
    """Return the common-share company name for a preferred-share listing."""
    stripped = _KR_PREFERRED_NAME_RE.sub("", name).strip()
    return stripped if len(stripped) >= 2 else name


def is_kr_preferred_share(code: str, name: str) -> bool:
    """KRX preferred shares: 6-character codes not ending in 0 (e.g. 005935,
    00104K - common shares always end in 0), and/or names carrying a preferred
    suffix (우/우B/2우B/우선주)."""
    code = str(code).strip()
    if len(code) == 6 and not code.endswith("0"):
        return True
    return bool(_KR_PREFERRED_NAME_RE.search(str(name).strip()))


def fetch_us_news(candidate: ScreenCandidate, config: AppConfig) -> List[Dict[str, str]]:
    if not config.newsapi_key:
        LOGGER.warning("NEWSAPI_KEY missing; US news skipped for %s.", candidate.display_symbol)
        return []
    url = "https://newsapi.org/v2/everything"
    params = {
        "q": f'"{candidate.name}" OR {candidate.symbol}',
        "language": "en",
        "sortBy": "publishedAt",
        "pageSize": 3,
        "apiKey": config.newsapi_key,
    }
    response = retry_call(
        f"NewsAPI {candidate.symbol}",
        config,
        lambda: requests.get(url, params=params, timeout=config.request_timeout),
    )
    if response.status_code in {401, 403, 429}:
        LOGGER.warning("NewsAPI status %s for %s: %s", response.status_code, candidate.display_symbol, response.text[:200])
        return []
    response.raise_for_status()
    articles = response.json().get("articles", [])[:3]
    return [
        {
            "title": article.get("title") or "Untitled",
            "url": article.get("url") or "#",
            "source": (article.get("source") or {}).get("name") or "NewsAPI",
            "published_at": article.get("publishedAt") or "",
        }
        for article in articles
    ]


def fetch_kr_news(candidate: ScreenCandidate, config: AppConfig) -> List[Dict[str, str]]:
    if not config.naver_client_id or not config.naver_client_secret:
        LOGGER.warning("Naver credentials missing; Korean news skipped for %s.", candidate.display_symbol)
        return []
    url = "https://openapi.naver.com/v1/search/news.json"
    headers = {
        "X-Naver-Client-Id": config.naver_client_id,
        "X-Naver-Client-Secret": config.naver_client_secret,
    }
    # Exact company name (common-share name for preferreds) plus a finance
    # keyword; a bare name query returns generic market-wrap articles.
    base_name = strip_preferred_suffix(candidate.name)
    params = {"query": f'"{base_name}" 실적 OR 주가', "display": 10, "sort": "date"}
    response = retry_call(
        f"Naver News {candidate.symbol}",
        config,
        lambda: requests.get(url, headers=headers, params=params, timeout=config.request_timeout),
    )
    if response.status_code in {401, 403, 429}:
        LOGGER.warning("Naver News status %s for %s: %s", response.status_code, candidate.display_symbol, response.text[:200])
        return []
    response.raise_for_status()
    items = response.json().get("items", [])
    name_matched: List[Dict[str, str]] = []
    others: List[Dict[str, str]] = []
    for item in items:
        title = strip_html_tags(item.get("title") or "Untitled")
        description = strip_html_tags(item.get("description") or "")
        article = {
            "title": title,
            "url": item.get("originallink") or item.get("link") or "#",
            "source": "Naver News",
            "published_at": item.get("pubDate") or "",
        }
        if base_name in title or base_name in description:
            name_matched.append(article)
        else:
            others.append(article)
    # Name-matched articles first; top up with unfiltered results if fewer than 3.
    ordered = name_matched + others
    LOGGER.info(
        "Naver news for %s: %s/%s articles mention %r.",
        candidate.display_symbol,
        len(name_matched),
        len(items),
        base_name,
    )
    return ordered[:3]


def fetch_news(candidate: ScreenCandidate, config: AppConfig) -> List[Dict[str, str]]:
    LOGGER.info("Step 5/6 - Fetching news for %s.", candidate.display_symbol)
    try:
        return fetch_us_news(candidate, config) if candidate.market == "US" else fetch_kr_news(candidate, config)
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("News fetch failed for %s: %s", candidate.display_symbol, exc)
        return []


def build_stock_report(candidate: ScreenCandidate, health: HealthCheck, config: AppConfig) -> StockReport:
    quarterly, quarterly_source = fetch_quarterly_financials(candidate, config)
    valuation, projections = fetch_valuation_and_projections(candidate, config)
    news = fetch_news(candidate, config)
    news_brief = fetch_news_brief(candidate, news, config)
    next_earnings, earnings_days_away = fetch_next_earnings(candidate, config)
    currency = None
    if candidate.market == "KR":
        currency = "KRW"
    else:
        currency = valuation.get("Currency") or "USD"
    return StockReport(
        candidate,
        health,
        news_brief,
        quarterly,
        valuation,
        projections,
        news,
        currency,
        next_earnings=next_earnings,
        earnings_days_away=earnings_days_away,
        financials_source=quarterly_source,
    )


def html_table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    return html_table_with_options(headers, rows)


def html_table_with_options(
    headers: Sequence[str],
    rows: Sequence[Sequence[str]],
    source: Optional[str] = None,
    row_styles: Optional[Sequence[Optional[str]]] = None,
) -> str:
    # All cells are left-aligned and every column gets an equal share of the
    # table width. Inline only what email clients must see per-cell
    # (text-align, padding, border); font-size, vertical-align, overflow-wrap,
    # and header background live once in the <style> block - Gmail clips
    # messages over ~102KB, so per-cell style repetition matters.
    cell_base = "text-align:left;padding:8px;border:1px solid #c9d9ea"
    colgroup = ""
    if len(headers) > 0:
        pct = 100.0 / len(headers)
        colgroup = "".join(f"<col style='width:{pct:.2f}%'>" for _ in headers)

    head = "".join(f"<th style='{cell_base}'>{html.escape(header)}</th>" for header in headers)
    body_rows = []
    for row_idx, row in enumerate(rows):
        row_extra = ""
        if row_styles is not None and row_idx < len(row_styles) and row_styles[row_idx]:
            row_extra = ";" + row_styles[row_idx]
        if len(row) == 1 and len(headers) > 1:
            # Single-cell row spans the full table width (e.g. collapsed
            # "quarterly breakdown unavailable" rows).
            cells = [f"<td colspan='{len(headers)}' style=\"{cell_base}{row_extra}\">{row[0]}</td>"]
        else:
            cells = [f"<td style=\"{cell_base}{row_extra}\">{cell}</td>" for cell in row]
        body_rows.append("<tr>" + "".join(cells) + "</tr>")
    body = "".join(body_rows)
    table_html = (
        "<table>"
        f"<colgroup>{colgroup}</colgroup>"
        f"<thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"
    )
    if source:
        table_html += f"<div style='color:#697581;font-size:12px;margin-top:4px'>Source: {html.escape(source)}</div>"
    return table_html


def render_market_summary(rows: List[MarketSummaryRow]) -> str:
    rows_html = []
    has_multi_change = any(getattr(row, "changes", None) for row in rows)
    for row in rows:
        close = format_number(row.close)
        daily_change = change_cell_html(row.pct_change)
        date_str = html.escape(str(row.close_date.date()) if row.close_date is not None else "N/A")
        if getattr(row, "changes", None):
            ch = row.changes
            weekly = change_cell_html(ch.get("weekly"))
            monthly = change_cell_html(ch.get("monthly"))
            quarterly = change_cell_html(ch.get("quarterly"))
        else:
            weekly = "N/A"
            monthly = "N/A"
            quarterly = "N/A"
        rows_html.append([
            html.escape(row.name),
            html.escape(row.symbol),
            close,
            daily_change,
            weekly,
            monthly,
            quarterly,
            date_str,
        ])
    headers = ["Asset", "Ticker", "Close", "Daily", "Weekly", "Monthly", "Quarterly", "Close Date"]
    return html_table_with_options(headers, rows_html, source="yfinance")


def _quarter_row_is_all_na(row: QuarterlyFinancialRow) -> bool:
    return row.period != "FY" and all(
        value is None
        for value in (row.revenue, row.ebit, row.net_income, row.operating_cash_flow, row.free_cash_flow)
    )


def render_quarterly_financials(rows: List[QuarterlyFinancialRow], source: Optional[str] = None) -> str:
    if not rows:
        return "<p class='muted'>Quarterly financials unavailable.</p>"
    rows_out = []
    row_styles = []
    index = 0
    while index < len(rows):
        row = rows[index]
        # Collapse consecutive all-N/A quarter rows within a fiscal year into a
        # single muted row; FY rows always render.
        if _quarter_row_is_all_na(row):
            run_end = index
            while (
                run_end + 1 < len(rows)
                and rows[run_end + 1].fiscal_year == row.fiscal_year
                and _quarter_row_is_all_na(rows[run_end + 1])
            ):
                run_end += 1
            if run_end > index:
                label = f"{row.period}–{rows[run_end].period} {row.fiscal_year}: quarterly breakdown unavailable"
                rows_out.append([html.escape(label)])
                row_styles.append("color:#697581")
                index = run_end + 1
                continue
        period_label = f"FY {row.fiscal_year}" if row.period == "FY" else f"{row.fiscal_year} {row.period}"
        rows_out.append([
            html.escape(period_label),
            format_number(row.revenue),
            format_number(row.ebit),
            format_number(row.net_income),
            format_number(row.operating_cash_flow),
            format_number(row.free_cash_flow),
        ])
        row_styles.append("font-weight:bold;background:#dbe8f6" if row.period == "FY" else None)
        index += 1
    return html_table_with_options(
        ["Period", "Revenue", "EBIT", "Net Income", "Operating CF", "Free Cash Flow"],
        rows_out,
        source=source,
        row_styles=row_styles,
    )


def render_key_values(values: Dict[str, Optional[float | str]], source: Optional[str] = None) -> str:
    rows = []
    for key, value in values.items():
        display = html.escape(str(value)) if isinstance(value, str) else format_number(value)
        rows.append([html.escape(str(key)), display])
    return html_table_with_options(["Metric", "Value"], rows, source=source)


def render_developments_and_news(report: StockReport, config: AppConfig) -> str:
    """Render the developments/news block with the header chosen by outcome.

    With Claude bullets: "Recent Developments (AI-generated)" + disclaimer,
    followed by the separate "Relevant News" headline section. Without bullets
    (no key or API failure): the raw headlines render ONCE under "Recent
    Headlines", with no AI header/disclaimer and no duplicate section.
    """
    if report.news_brief:
        items = "".join(f"<li>{html.escape(bullet)}</li>" for bullet in report.news_brief)
        disclaimer = (
            "<p style='color:#697581;font-size:11px;margin:4px 0 0'>"
            "Bullets are AI-generated from web sources and may be incomplete.</p>"
        )
        return (
            "<h4>Recent Developments (AI-generated)</h4>"
            f"<ul style='margin:8px 0 0;padding-left:20px'>{items}</ul>{disclaimer}"
            "<h4>Relevant News</h4>"
            f"{render_news(report.news)}"
        )
    return f"<h4>Recent Headlines</h4>{render_news(report.news)}"


def render_news(news: List[Dict[str, str]]) -> str:
    if not news:
        return "<p class='muted'>No recent relevant headlines returned by the configured news API.</p>"
    items = []
    for article in news[:3]:
        title = html.escape(article.get("title", "Untitled"))
        url = html.escape(article.get("url", "#"))
        source = html.escape(article.get("source", "News"))
        published = html.escape(article.get("published_at", ""))
        items.append(f"<li><a href='{url}' style='color:#0958a5'>{title}</a><br><span style='color:#697581;font-size:12px'>{source} | {published}</span></li>")
    # Add source attribution if all articles seem to come from same API
    api_source = None
    if news and isinstance(news, list):
        # use the first article's source name as the caption
        api_source = news[0].get("source")
    caption = f"<div style='color:#697581;font-size:12px;margin-top:6px'>Source: {html.escape(api_source) if api_source else 'News API'}</div>"
    return "<ol>" + "".join(items) + "</ol>" + caption


def compute_multi_changes(data: pd.DataFrame, windows: Tuple[int, int, int, int] = (1, 5, 21, 63)) -> Dict[str, Optional[float]]:
    """Compute percentage changes for multiple trading-day windows.

    Windows are trading-day counts; data is expected to be a normalized OHLCV frame
    with a sorted index and a numeric 'Close' column.
    """
    result: Dict[str, Optional[float]] = {}
    mapping = {0: "daily", 1: "weekly", 2: "monthly", 3: "quarterly"}
    for idx, w in enumerate(windows):
        key = mapping.get(idx, str(w))
        try:
            if data is None or data.empty or len(data) <= w:
                result[key] = None
                continue
            latest = float(data["Close"].iloc[-1])
            past = float(data["Close"].iloc[-1 - w])
            result[key] = (latest / past - 1.0) * 100.0
        except Exception:
            result[key] = None
    return result


def emoji_for_change(pct: Optional[float]) -> str:
    if pct is None:
        return "N/A"
    if pct > 10:
        return "🚀"
    if pct < -10:
        return "💥"
    return "🔺" if pct >= 0 else "🔻"


def change_cell_html(pct: Optional[float]) -> str:
    if pct is None:
        return "N/A"
    emoji = emoji_for_change(pct)
    color = "#0a8a0a" if pct >= 0 else "#c00000"
    pct_text = format_pct(pct)
    return f"<span style='color:{color};white-space:nowrap'>{emoji} {html.escape(pct_text)}</span>"


def color_pct_html(pct: Optional[float]) -> str:
    if pct is None:
        return "N/A"
    color = "#0a8a0a" if pct >= 0 else "#c00000"
    return f"<span style='color:{color};white-space:nowrap'>{html.escape(format_pct(pct))}</span>"


def render_stock_report(report: StockReport, config: AppConfig, ohlcv_map: Optional[Dict[str, pd.DataFrame]] = None) -> str:
    c = report.candidate
    # determine OHLCV for this symbol
    yf_symbol = get_yfinance_ticker_symbol(c)
    data: Optional[pd.DataFrame] = None
    if ohlcv_map and c.symbol in ohlcv_map:
        data = ohlcv_map.get(c.symbol)
    else:
        # attempt a lightweight fetch (useful when called standalone)
        try:
            period_days = max(config.lookback_days, 130)
            raw = retry_call(f"yfinance quick fetch {yf_symbol}", config, lambda: yf.download(yf_symbol, period=f"{period_days}d", interval="1d", progress=False, threads=False))
            data = normalize_ohlcv(raw)
        except Exception:
            data = pd.DataFrame()

    changes = compute_multi_changes(data) if data is not None else {}

    # Signal table
    signal_source = "yfinance" if c.market == "US" else ("FinanceDataReader" if fdr is not None else "yfinance")
    signal_table = html_table_with_options(
        ["Signal Date", "Previous Close", f"Previous {config.sma_window}-Day SMA", "Signal Close", f"Signal {config.sma_window}-Day SMA", "Signal Volume"],
        [[
            html.escape(str(c.signal_date.date())),
            format_number(c.previous_close),
            format_number(c.previous_sma),
            format_number(c.signal_close),
            format_number(c.signal_sma),
            format_number(c.signal_volume),
        ]],
        source=signal_source,
    )

    # Health table
    health_table = html_table_with_options(
        ["FCF", "Net Income", "Source", "Result"],
        [[
            format_number(report.health.free_cash_flow),
            format_number(report.health.net_income),
            html.escape(report.health.source),
            html.escape(report.health.reason),
        ]],
        source=report.health.source,
    )

    # Multi-timeframe change table for this stock
    change_headers = ["Daily", "Weekly", "Monthly", "Quarterly"]
    change_row = [change_cell_html(changes.get("daily")), change_cell_html(changes.get("weekly")), change_cell_html(changes.get("monthly")), change_cell_html(changes.get("quarterly"))]
    change_table = html_table_with_options(change_headers, [change_row], source=signal_source)

    # Financials source attribution
    fin_source = report.financials_source
    valuation = dict(report.valuation)
    pe_note = valuation.pop("_pe_source", None)
    valuation_source = "DART-first (fallback: yfinance)" if report.currency == "KRW" else "yfinance"
    if pe_note:
        valuation_source = f"{valuation_source}; {pe_note}"
    projections_source = "DART-first (fallback: yfinance)" if report.currency == "KRW" else "yfinance"

    # Next-earnings line (display only; imminent earnings flagged with a warning emoji).
    if report.next_earnings is not None and report.earnings_days_away is not None:
        warn = "⚠️ " if report.earnings_days_away <= 7 else ""
        earnings_line = (
            f"<p style='font-size:13px;color:#46515c;margin:2px 0 10px'>{warn}Next earnings: "
            f"{html.escape(report.next_earnings)} ({report.earnings_days_away} days away)</p>"
        )
    else:
        earnings_line = "<p style='font-size:13px;color:#46515c;margin:2px 0 10px'>Next earnings: N/A</p>"

    return f"""
    <section class="stock">
      <h3>{html.escape(c.name)} ({html.escape(c.display_symbol)}) <small class="muted">({html.escape(report.currency)})</small></h3>
      {earnings_line}
      {change_table}
      <h4>{config.sma_window}-Day SMA Signal Verification</h4>
      {signal_table}
      <p style="font-size:12px;color:#697581;margin-top:6px">Note: SMA window used: {config.sma_window}-Day. Signal = close crossing above SMA.</p>
      <h4>Cash Flow Verification</h4>
      {health_table}
      {render_developments_and_news(report, config)}
      <h4>Quarterly Financials</h4>
      {render_quarterly_financials(report.quarterly_financials, source=fin_source)}
      <h4>Valuation</h4>
      {render_key_values(valuation, source=valuation_source)}
      <h4>Forward Projections</h4>
      {render_key_values(report.projections, source=projections_source)}
    </section>
    """


def render_na_slot(market: str, slot: int, config: AppConfig) -> str:
        return f"""
        <section class="stock muted-card">
            <h3>{html.escape(market)} Slot {slot}: N/A</h3>
            <p>No additional stock passed both the {config.sma_window}-Day SMA crossover screen and the positive FCF / positive net income quality-control check.</p>
        </section>
        """


def _picks_db_connect(config: AppConfig) -> sqlite3.Connection:
    conn = sqlite3.connect(config.picks_db_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS picks (
            date TEXT NOT NULL,
            market TEXT NOT NULL,
            symbol TEXT NOT NULL,
            name TEXT NOT NULL,
            signal_close REAL,
            signal_date TEXT,
            PRIMARY KEY (date, market, symbol)
        )
        """
    )
    conn.commit()
    return conn


def save_picks_to_history(reports: List[StockReport], config: AppConfig) -> None:
    """Persist today's picks to picks.db. DB errors must never break the pipeline."""
    run_date = today_for_data_cutoff().isoformat()
    try:
        conn = _picks_db_connect(config)
        try:
            for report in reports:
                c = report.candidate
                conn.execute(
                    "INSERT OR REPLACE INTO picks (date, market, symbol, name, signal_close, signal_date) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (run_date, c.market, c.symbol, c.name, float(c.signal_close), str(c.signal_date.date())),
                )
            conn.commit()
        finally:
            conn.close()
        LOGGER.info("Pick history: saved %s pick(s) for %s to %s.", len(reports), run_date, config.picks_db_path)
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("Pick history: failed to save picks: %s", exc)


def _latest_close_for_pick(
    market: str,
    symbol: str,
    config: AppConfig,
    us_ohlcv: Optional[Dict[str, pd.DataFrame]],
    kr_ohlcv: Optional[Dict[str, pd.DataFrame]],
) -> Optional[float]:
    ohlcv_map = us_ohlcv if market == "US" else kr_ohlcv
    data = (ohlcv_map or {}).get(symbol)
    if data is None or data.empty:
        try:
            if market == "KR":
                data = fetch_kr_ohlcv(symbol, config)
            else:
                raw = yf.download(symbol, period="10d", interval="1d", progress=False, threads=False)
                data = normalize_ohlcv(raw)
        except Exception as exc:  # noqa: BLE001
            LOGGER.debug("Pick history: latest close fetch failed for %s %s: %s", market, symbol, exc)
            return None
    if data is None or data.empty:
        return None
    return safe_float(data["Close"].iloc[-1])


def build_picks_review_html(
    config: AppConfig,
    us_ohlcv: Optional[Dict[str, pd.DataFrame]] = None,
    kr_ohlcv: Optional[Dict[str, pd.DataFrame]] = None,
) -> str:
    """Render the "Previous Picks Review" section from picks.db.

    Shows picks from the last 5 recorded run days (deduplicated by symbol, most
    recent first) with price at pick vs latest close. Any DB problem degrades to
    a muted notice; it never breaks the pipeline.
    """
    header = "<h2>Previous Picks Review</h2>"
    today_iso = today_for_data_cutoff().isoformat()
    try:
        conn = _picks_db_connect(config)
        try:
            dates = [
                row[0]
                for row in conn.execute(
                    "SELECT DISTINCT date FROM picks WHERE date < ? ORDER BY date DESC LIMIT 5",
                    (today_iso,),
                )
            ]
            total_tracked = conn.execute(
                "SELECT COUNT(*) FROM picks WHERE date < ?", (today_iso,)
            ).fetchone()[0]
            records: List[Tuple[str, str, str, str, Optional[float]]] = []
            if dates:
                placeholders = ",".join("?" for _ in dates)
                records = conn.execute(
                    f"SELECT date, market, symbol, name, signal_close FROM picks "
                    f"WHERE date IN ({placeholders}) ORDER BY date DESC, market, symbol",
                    dates,
                ).fetchall()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("Pick history: review unavailable due to DB error: %s", exc)
        return header + "<p class='muted'>Pick history unavailable.</p>"

    if not records:
        return header + "<p class='muted'>No pick history yet.</p>"

    seen: set = set()
    table_rows: List[List[str]] = []
    positive = 0
    with_price = 0
    for pick_date, market, symbol, name, signal_close in records:
        key = (market, symbol)
        if key in seen:
            continue
        seen.add(key)
        display_symbol = symbol if market == "US" else f"{symbol}.KS"
        latest_close = _latest_close_for_pick(market, symbol, config, us_ohlcv, kr_ohlcv)
        pick_price = safe_float(signal_close)
        pct: Optional[float] = None
        if latest_close is not None and pick_price is not None and pick_price > 0:
            pct = (latest_close / pick_price - 1.0) * 100.0
            with_price += 1
            if pct >= 0:
                positive += 1
        table_rows.append([
            html.escape(str(pick_date)),
            html.escape(display_symbol),
            html.escape(str(name)),
            format_number(pick_price),
            format_number(latest_close),
            color_pct_html(pct),
        ])

    table = html_table_with_options(
        ["Pick Date", "Symbol", "Name", "Price at Pick", "Latest Close", "Change"],
        table_rows,
    )
    if with_price:
        pct_positive = positive / with_price * 100.0
        stat_line = (
            f"<p style='font-size:12px;color:#697581;margin:0 0 8px'>Picks tracked: {total_tracked} total &middot; "
            f"{positive}/{with_price} recent picks currently positive ({pct_positive:.0f}%).</p>"
        )
    else:
        stat_line = (
            f"<p style='font-size:12px;color:#697581;margin:0 0 8px'>Picks tracked: {total_tracked} total.</p>"
        )
    return header + stat_line + table


def build_email_html(
    market_summary: List[MarketSummaryRow],
    us_reports: List[StockReport],
    kr_reports: List[StockReport],
    config: AppConfig,
    us_ohlcv: Optional[Dict[str, pd.DataFrame]] = None,
    kr_ohlcv: Optional[Dict[str, pd.DataFrame]] = None,
    picks_review_html: str = "",
) -> str:
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    us_sections = "".join(render_stock_report(report, config, us_ohlcv) for report in us_reports)
    kr_sections = "".join(render_stock_report(report, config, kr_ohlcv) for report in kr_reports)
    for idx in range(len(us_reports) + 1, config.picks_per_market + 1):
        us_sections += render_na_slot("US", idx, config)
    for idx in range(len(kr_reports) + 1, config.picks_per_market + 1):
        kr_sections += render_na_slot("Korea", idx, config)
    email_html = f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <style>
    body {{ font-family: Arial, Helvetica, sans-serif; color: #182026; line-height: 1.45; margin: 0; padding: 0; background: #f5f7f9; }}
        .container {{ max-width: 640px; width:100%; margin: 0 auto; padding: 16px; background: #ffffff; }}
        @media (max-width:600px) {{
            body {{ font-size:13px; }}
            .container {{ padding:12px; }}
            table {{ font-size:13px; }}
        }}
    h1 {{ margin: 0 0 4px; font-size: 28px; }}
    h2 {{ margin-top: 28px; padding-bottom: 8px; border-bottom: 2px solid #d8dee6; font-size: 20px; }}
    h3 {{ margin-bottom: 8px; font-size: 18px; }}
    h4 {{ margin: 18px 0 8px; font-size: 14px; text-transform: uppercase; color: #46515c; letter-spacing: .03em; }}
    table {{ width: 100%; table-layout: fixed; border-collapse: collapse; margin: 8px 0 16px; font-size: 13px; }}
    th, td {{ border: 1px solid #c9d9ea; padding: 8px; text-align: left; vertical-align: top; overflow-wrap: break-word; font-size: 13px; }}
    th {{ background: #1f4e79; color: #ffffff; }}
    a {{ color: #0958a5; text-decoration: none; }}
    .subtitle {{ color: #5d6975; margin: 0 0 18px; }}
    .stock {{ border: 1px solid #d8dee6; border-radius: 8px; padding: 16px; margin: 14px 0; background: #ffffff; }}
    .muted {{ color: #697581; }}
    .muted-card {{ background: #f8fafc; color: #596672; }}
    ol {{ margin-top: 8px; }}
    li {{ margin-bottom: 10px; }}
    li span {{ color: #697581; font-size: 12px; }}
    .footer {{ margin-top: 28px; font-size: 12px; color: #697581; }}
  </style>
</head>
<body>
    <div class="container" style="max-width:640px;width:100%;margin:0 auto;padding:16px;background:#ffffff">
    <h1>Stocks of the Day</h1>
    <p class="subtitle">Generated at {html.escape(generated_at)}. Signal date is the latest completed trading day available before the current data cutoff.</p>
    {picks_review_html}
    <h2>Market Summary</h2>
    {render_market_summary(market_summary)}
    <h2>US Technical Picks</h2>
    {us_sections}
    <h2>Korean Technical Picks</h2>
    {kr_sections}
    <p class="footer">This email is generated for screening and research workflow support only. It is not investment advice.</p>
  </div>
</body>
</html>"""
    # Compact the markup: whitespace between tags is dead weight against
    # Gmail's ~102KB clipping threshold.
    email_html = re.sub(r">\s+<", "><", email_html)
    size_bytes = len(email_html.encode("utf-8"))
    LOGGER.info("Email HTML built: %s bytes (%.1f KB).", size_bytes, size_bytes / 1024)
    if size_bytes > 95 * 1024:
        LOGGER.warning(
            "Email HTML is %.1f KB, above the 95KB budget; Gmail clips at ~102KB.",
            size_bytes / 1024,
        )
    return email_html


def send_email(subject: str, html_body: str, config: AppConfig) -> None:
    LOGGER.info("Step 6/6 - Preparing email delivery.")
    output_dir = os.path.dirname(os.path.abspath(config.output_html_path))
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)
    with open(config.output_html_path, "w", encoding="utf-8") as file:
        file.write(html_body)
    LOGGER.info("HTML preview saved to %s.", config.output_html_path)
    if config.dry_run:
        LOGGER.info("DRY_RUN=true; email delivery skipped.")
        return
    missing = []
    if not config.smtp_user:
        missing.append("GMAIL_USER")
    if not config.smtp_password:
        missing.append("GMAIL_APP_PASSWORD")
    if not config.email_from:
        missing.append("EMAIL_FROM or GMAIL_USER")
    if not config.email_to:
        missing.append("EMAIL_TO")
    if missing:
        raise RuntimeError(f"Email delivery cannot run; missing environment variables: {', '.join(missing)}")

    message = MIMEMultipart("alternative")
    message["Subject"] = subject
    message["From"] = config.email_from
    message["To"] = ", ".join(config.email_to)
    message.attach(MIMEText(html_body, "html", "utf-8"))

    LOGGER.info("Connecting to SMTP host %s:%s.", config.smtp_host, config.smtp_port)
    with smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=config.request_timeout) as server:
        server.ehlo()
        server.starttls()
        server.login(config.smtp_user, config.smtp_password)
        server.sendmail(config.email_from, config.email_to, message.as_string())
    LOGGER.info("Email sent to %s.", ", ".join(config.email_to))


def fetch_sp500_universe(config: AppConfig) -> Tuple[List[str], Dict[str, str]]:
    LOGGER.info("Fetching S&P 500 constituent list from Wikipedia.")
    try:
        resp = retry_call(
            "S&P 500 Wikipedia fetch",
            config,
            lambda: requests.get(
                "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
                timeout=config.request_timeout,
                headers={"User-Agent": "Mozilla/5.0"},
            ),
        )
        resp.raise_for_status()
        tables = pd.read_html(io.StringIO(resp.text))
        df = tables[0]
        symbols = df["Symbol"].str.replace(".", "-", regex=False).tolist()
        name_map = dict(zip(
            df["Symbol"].str.replace(".", "-", regex=False),
            df["Security"],
        ))
        LOGGER.info("S&P 500 universe: %s stocks loaded.", len(symbols))
        return symbols, name_map
    except Exception as exc:
        LOGGER.warning("S&P 500 fetch failed (%s); falling back to built-in universe.", exc)
        return list(US_UNIVERSE), dict(US_NAME_MAP)


def load_us_universe(config: AppConfig) -> Tuple[List[str], Dict[str, str]]:
    """Compatibility wrapper to load the US universe at runtime.

    This calls `fetch_sp500_universe` but is provided so callers can
    explicitly request a dynamic load and it can be swapped easily.
    """
    return fetch_sp500_universe(config)


def filter_preferred_shares(pairs: List[Tuple[str, str]], config: AppConfig) -> List[Tuple[str, str]]:
    if not config.exclude_preferred_shares:
        return pairs
    kept = [(code, name) for code, name in pairs if not is_kr_preferred_share(code, name)]
    excluded = len(pairs) - len(kept)
    if excluded:
        LOGGER.info("KR universe: excluded %s preferred share(s); %s common shares remain.", excluded, len(kept))
    return kept


def fetch_kospi_universe(config: AppConfig) -> List[Tuple[str, str]]:
    if fdr is None:
        LOGGER.warning("FinanceDataReader not available; using built-in KR universe.")
        return filter_preferred_shares(list(KR_UNIVERSE), config)
    LOGGER.info("Fetching KOSPI constituent list via FinanceDataReader.")
    try:
        df = retry_call(
            "KOSPI StockListing",
            config,
            lambda: fdr.StockListing("KOSPI"),
        )
        code_col = next((c for c in ("Code", "Symbol") if c in df.columns), None)
        name_col = next((c for c in ("Name", "ISU_ABBRV") if c in df.columns), None)
        if code_col is None or name_col is None:
            raise ValueError(f"Unexpected columns: {list(df.columns)}")
        pairs = [
            (str(row[code_col]).zfill(6), str(row[name_col]))
            for _, row in df[[code_col, name_col]].iterrows()
            if pd.notna(row[code_col]) and pd.notna(row[name_col])
        ]
        LOGGER.info("KOSPI universe: %s stocks loaded.", len(pairs))
        return filter_preferred_shares(pairs, config)
    except Exception as exc:
        LOGGER.warning("KOSPI fetch failed (%s); falling back to built-in universe.", exc)
        return filter_preferred_shares(list(KR_UNIVERSE), config)


def load_kr_universe(config: AppConfig) -> List[Tuple[str, str]]:
    """Compatibility wrapper to load the KR universe at runtime.

    Calls `fetch_kospi_universe` and returns the listing as `(code, name)` pairs.
    """
    return fetch_kospi_universe(config)


def run_pipeline(config: AppConfig) -> None:
    LOGGER.info("Starting Stocks of the Day pipeline.")
    LOGGER.info(
        "Configuration: sma_window=%s lookback_days=%s picks_per_market=%s dry_run=%s test_run=%s.",
        config.sma_window,
        config.lookback_days,
        config.picks_per_market,
        config.dry_run,
        config.test_run,
    )
    market_summary = fetch_market_summary(config)

    LOGGER.info("Step 2/6 - Running technical screening for US stocks.")
    us_symbols, us_name_map = fetch_sp500_universe(config)
    us_ohlcv = fetch_us_ohlcv_batch(us_symbols, config)
    us_candidates = detect_sma_crossovers("US", us_name_map, us_ohlcv, config)
    us_quality = select_quality_candidates(us_candidates, config)

    LOGGER.info("Step 2/6 - Running technical screening for Korean stocks.")
    kr_universe = fetch_kospi_universe(config)
    kr_name_map = dict(kr_universe)
    kr_ohlcv = fetch_kr_ohlcv_batch(kr_universe, config)
    kr_candidates = detect_sma_crossovers("KR", kr_name_map, kr_ohlcv, config)
    kr_quality = select_quality_candidates(kr_candidates, config)

    LOGGER.info("Step 4/6 - Building detailed reports for selected US stocks.")
    us_reports = [build_stock_report(candidate, health, config) for candidate, health in us_quality]
    LOGGER.info("Step 4/6 - Building detailed reports for selected Korean stocks.")
    kr_reports = [build_stock_report(candidate, health, config) for candidate, health in kr_quality]

    # Review yesterday-and-earlier picks before persisting today's, so today's
    # picks do not show up in their own review with a ~0% change.
    picks_review_html = build_picks_review_html(config, us_ohlcv, kr_ohlcv)
    save_picks_to_history(us_reports + kr_reports, config)

    subject_date = today_for_data_cutoff().strftime("%Y-%m-%d")
    email_html = build_email_html(
        market_summary, us_reports, kr_reports, config, us_ohlcv, kr_ohlcv, picks_review_html
    )
    send_email(f"Stocks of the Day - {subject_date}", email_html, config)
    LOGGER.info("Stocks of the Day pipeline completed.")


def send_failure_alert(exc: BaseException, config: AppConfig) -> None:
    """Send a minimal plain-text failure alert.

    Deliberately avoids the HTML rendering path so a rendering bug cannot break
    the alert itself.
    """
    subject_date = today_for_data_cutoff().strftime("%Y-%m-%d")
    body = (
        "The Stocks of the Day pipeline failed.\n\n"
        f"Exception type: {type(exc).__name__}\n"
        f"Message: {exc}\n\n"
        "Last log lines:\n"
        f"{tail_log_lines(30)}\n"
    )
    recipients = config.email_to or ([config.test_email_to] if config.test_email_to else [])
    if config.dry_run:
        LOGGER.warning("DRY_RUN=true; failure alert email skipped. Alert body:\n%s", body)
        return
    if not (config.smtp_user and config.smtp_password and recipients):
        raise RuntimeError("SMTP credentials or recipients missing; cannot send failure alert")
    message = MIMEText(body, "plain", "utf-8")
    message["Subject"] = f"Stocks of the Day FAILED - {subject_date}"
    message["From"] = config.email_from or config.smtp_user
    message["To"] = ", ".join(recipients)
    with smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=config.request_timeout) as server:
        server.ehlo()
        server.starttls()
        server.login(config.smtp_user, config.smtp_password)
        server.sendmail(message["From"], recipients, message.as_string())
    LOGGER.info("Failure alert email sent to %s.", ", ".join(recipients))


def run_pipeline_with_alerts(config: AppConfig) -> None:
    """Run the pipeline; on failure send the alert email, then re-raise.

    If even the alert email fails, write the details to last_failure.log and
    exit non-zero.
    """
    try:
        run_pipeline(config)
    except Exception as exc:  # noqa: BLE001
        LOGGER.exception("Pipeline failed: %s", exc)
        original_traceback = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        try:
            send_failure_alert(exc, config)
        except Exception as alert_exc:  # noqa: BLE001
            LOGGER.error("Failure alert email could not be sent: %s", alert_exc)
            try:
                with open("last_failure.log", "w", encoding="utf-8") as file:
                    file.write(
                        f"Failure alert delivery failed: {alert_exc}\n\n"
                        f"Original pipeline failure:\n{original_traceback}\n"
                        f"Last log lines:\n{tail_log_lines(30)}\n"
                    )
            except Exception:  # noqa: BLE001
                pass
            sys.exit(1)
        raise


def run_test_loop(config: AppConfig) -> None:
    LOGGER.info(
        "TEST_RUN=true; sending to %s every %s seconds. Press Ctrl+C to stop.",
        ", ".join(config.email_to),
        config.test_interval_seconds,
    )
    iteration = 1
    while True:
        LOGGER.info("Starting test email iteration %s.", iteration)
        run_pipeline_with_alerts(config)
        LOGGER.info(
            "Test email iteration %s completed; sleeping for %s seconds.",
            iteration,
            config.test_interval_seconds,
        )
        iteration += 1
        time.sleep(config.test_interval_seconds)


def run_scheduler(config: AppConfig) -> None:
    # Daily 07:00 KST delivery: set SCHEDULER_ENABLED=true, SCHEDULE_TIME_KST=07:00,
    # SCHEDULE_INTERVAL_MINUTES=0 in .env, then keep this process running.
    #
    # Running persistently on macOS — preferred: launchd. Save as
    # ~/Library/LaunchAgents/com.stocksoftheday.agent.plist, then run
    # `launchctl load ~/Library/LaunchAgents/com.stocksoftheday.agent.plist`:
    #
    #   <?xml version="1.0" encoding="UTF-8"?>
    #   <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
    #     "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
    #   <plist version="1.0">
    #   <dict>
    #     <key>Label</key><string>com.stocksoftheday.agent</string>
    #     <key>ProgramArguments</key>
    #     <array>
    #       <string>/PATH/TO/PROJECT/.venv/bin/python</string>
    #       <string>/PATH/TO/PROJECT/stocks_of_the_day.py</string>
    #     </array>
    #     <key>WorkingDirectory</key><string>/PATH/TO/PROJECT</string>
    #     <key>RunAtLoad</key><true/>
    #     <key>KeepAlive</key><true/>
    #     <key>StandardOutPath</key><string>/PATH/TO/PROJECT/scheduler.log</string>
    #     <key>StandardErrorPath</key><string>/PATH/TO/PROJECT/scheduler.log</string>
    #   </dict>
    #   </plist>
    #
    # Quick alternative (dies on reboot/logout, fine for testing):
    #   nohup .venv/bin/python stocks_of_the_day.py >> scheduler.log 2>&1 &
    from zoneinfo import ZoneInfo
    KST = ZoneInfo("Asia/Seoul")
    if config.schedule_interval_minutes > 0:
        LOGGER.info(
            "Scheduler: running every %s minute(s). "
            "Set SCHEDULE_INTERVAL_MINUTES=0 in .env to switch to daily KST mode.",
            config.schedule_interval_minutes,
        )
        while True:
            try:
                run_pipeline_with_alerts(config)
            except Exception:  # noqa: BLE001 - alert already sent; keep the daemon alive
                LOGGER.error("Run failed; scheduler continues to the next scheduled run.")
            LOGGER.info("Sleeping for %s minute(s) until next run.", config.schedule_interval_minutes)
            time.sleep(config.schedule_interval_minutes * 60)
    else:
        hour, minute = map(int, config.schedule_time_kst.split(":"))
        LOGGER.info(
            "Scheduler: daily at %s KST. Change SCHEDULE_TIME_KST in .env to adjust.",
            config.schedule_time_kst,
        )
        while True:
            now = datetime.now(KST)
            target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if now >= target:
                target += timedelta(days=1)
            wait = (target - now).total_seconds()
            LOGGER.info(
                "Next run at %s KST (in %.0f s / %.1f h).",
                target.strftime("%H:%M"),
                wait,
                wait / 3600,
            )
            time.sleep(wait)
            try:
                run_pipeline_with_alerts(config)
            except Exception:  # noqa: BLE001 - alert already sent; keep the daemon alive
                LOGGER.error("Run failed; scheduler continues to the next scheduled run.")


def main() -> None:
    configure_logging()
    config = AppConfig.from_env()
    # OpenDartReader (and some other libraries) issue HTTP requests without a
    # timeout, so one stalled connection hangs the pipeline forever with no
    # output. This default only applies to sockets that don't set their own.
    socket.setdefaulttimeout(max(config.request_timeout, 20))
    if config.test_run:
        run_test_loop(config)
    elif config.scheduler_enabled:
        run_scheduler(config)
    else:
        run_pipeline_with_alerts(config)


if __name__ == "__main__":
    main()
