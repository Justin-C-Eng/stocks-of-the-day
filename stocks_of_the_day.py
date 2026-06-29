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
import logging
import os
import re
import smtplib
import time
from dataclasses import dataclass
from datetime import date, datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd
import requests
import yfinance as yf
from dotenv import load_dotenv

try:
    import FinanceDataReader as fdr
except ImportError:  # pragma: no cover - handled at runtime with a clear log
    fdr = None

try:
    import OpenDartReader
except ImportError:  # pragma: no cover - handled at runtime with a clear log
    OpenDartReader = None


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
    dry_run: bool = False
    output_html_path: str = "stocks_of_the_day_email_preview.html"
    test_run: bool = False
    test_email_to: str = DEFAULT_TEST_EMAIL_TO
    test_interval_seconds: int = 60

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
            dry_run=False if test_run else os.getenv("DRY_RUN", "false").strip().lower() in {"1", "true", "yes"},
            output_html_path=os.getenv(
                "OUTPUT_HTML_PATH",
                "stocks_of_the_day_email_preview.html",
            ),
            test_run=test_run,
            test_email_to=test_email_to,
            test_interval_seconds=int(os.getenv("TEST_INTERVAL_SECONDS", "60")),
        )


@dataclass
class MarketSummaryRow:
    symbol: str
    name: str
    close: Optional[float]
    pct_change: Optional[float]
    close_date: Optional[pd.Timestamp]
    status: str


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
class HistoricalFinancialRow:
    fiscal_year: str
    revenue: Optional[float]
    ebit: Optional[float]
    net_income: Optional[float]
    free_cash_flow: Optional[float]


@dataclass
class StockReport:
    candidate: ScreenCandidate
    health: HealthCheck
    profile: str
    historical_financials: List[HistoricalFinancialRow]
    valuation: Dict[str, Optional[float | str]]
    projections: Dict[str, Optional[float | str]]
    news: List[Dict[str, str]]


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


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


def safe_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except TypeError:
        pass
    try:
        result = float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None
    if not np.isfinite(result):
        return None
    return result


def format_number(value: Any, decimals: int = 2) -> str:
    number = safe_float(value)
    if number is None:
        return "N/A"
    abs_number = abs(number)
    if abs_number >= 1_000_000_000_000:
        return f"{number / 1_000_000_000_000:,.{decimals}f}T"
    if abs_number >= 1_000_000_000:
        return f"{number / 1_000_000_000:,.{decimals}f}B"
    if abs_number >= 1_000_000:
        return f"{number / 1_000_000:,.{decimals}f}M"
    return f"{number:,.{decimals}f}"


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
    for symbol, name in MARKET_ASSETS.items():
        try:
            raw = retry_call(
                f"Market summary download {symbol}",
                config,
                lambda symbol=symbol: yf.download(
                    symbol,
                    period="14d",
                    interval="1d",
                    end=end_date,
                    auto_adjust=False,
                    progress=False,
                    threads=False,
                ),
            )
            data = normalize_ohlcv(raw)
            if len(data) < 2:
                rows.append(MarketSummaryRow(symbol, name, None, None, None, "insufficient data"))
                LOGGER.warning("%s (%s): insufficient close history.", name, symbol)
                continue
            previous_close = float(data["Close"].iloc[-2])
            latest_close = float(data["Close"].iloc[-1])
            pct_change = (latest_close / previous_close - 1.0) * 100.0
            close_date = pd.Timestamp(data.index[-1])
            rows.append(MarketSummaryRow(symbol, name, latest_close, pct_change, close_date, "ok"))
            LOGGER.info(
                "%s (%s): close_date=%s close=%.4f previous_close=%.4f change=%+.2f%%",
                name,
                symbol,
                close_date.date(),
                latest_close,
                previous_close,
                pct_change,
            )
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception("%s (%s): market summary failed: %s", name, symbol, exc)
            rows.append(MarketSummaryRow(symbol, name, None, None, None, f"error: {exc}"))
    return rows


def fetch_us_ohlcv_batch(symbols: Sequence[str], config: AppConfig) -> Dict[str, pd.DataFrame]:
    LOGGER.info("Fetching US OHLCV batch: %s tickers.", len(symbols))
    end_date = today_for_data_cutoff().isoformat()
    raw = retry_call(
        "US universe yfinance batch download",
        config,
        lambda: yf.download(
            list(symbols),
            period=f"{config.lookback_days}d",
            interval="1d",
            end=end_date,
            auto_adjust=False,
            group_by="ticker",
            progress=False,
            threads=True,
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
        LOGGER.error("FinanceDataReader is not installed; Korean screening cannot run.")
        return pd.DataFrame()
    start = (pd.Timestamp(today_for_data_cutoff()) - pd.Timedelta(days=config.lookback_days)).strftime("%Y-%m-%d")
    end = today_for_data_cutoff().strftime("%Y-%m-%d")
    try:
        raw = retry_call(
            f"Korean OHLCV FinanceDataReader {symbol}",
            config,
            lambda: fdr.DataReader(symbol, start, end),
        )
        return normalize_ohlcv(raw)
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("Unable to fetch Korean OHLCV for %s: %s", symbol, exc)
        return pd.DataFrame()


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
    if not config.dart_api_key or OpenDartReader is None:
        return None, None
    dart = OpenDartReader(config.dart_api_key)
    current_year = today_for_data_cutoff().year
    for fiscal_year in range(current_year - 1, current_year - 5, -1):
        try:
            frame = retry_call(
                f"DART financial statements {symbol} {fiscal_year}",
                config,
                lambda fiscal_year=fiscal_year: dart.finstate(symbol, fiscal_year, reprt_code="11011"),
            )
            if frame is None or frame.empty:
                continue
            accounts = frame.copy()
            accounts["clean_amount"] = pd.to_numeric(
                accounts.get("thstrm_amount", pd.Series(dtype=str)).astype(str).str.replace(",", "", regex=False),
                errors="coerce",
            )
            ni_rows = accounts[accounts["account_nm"].astype(str).str.contains("당기순이익|Net income", case=False, regex=True)]
            ocf_rows = accounts[accounts["account_nm"].astype(str).str.contains("영업활동.*현금흐름|Operating cash", case=False, regex=True)]
            capex_rows = accounts[accounts["account_nm"].astype(str).str.contains("유형자산.*취득|capital expenditure", case=False, regex=True)]
            net_income = safe_float(ni_rows["clean_amount"].dropna().iloc[0]) if not ni_rows.empty else None
            operating_cf = safe_float(ocf_rows["clean_amount"].dropna().iloc[0]) if not ocf_rows.empty else None
            capex = safe_float(capex_rows["clean_amount"].dropna().iloc[0]) if not capex_rows.empty else None
            fcf = None
            if operating_cf is not None and capex is not None:
                fcf = operating_cf - abs(capex)
            LOGGER.info(
                "DART %s %s: net_income=%s operating_cf=%s capex=%s fcf=%s",
                symbol,
                fiscal_year,
                format_number(net_income, 0),
                format_number(operating_cf, 0),
                format_number(capex, 0),
                format_number(fcf, 0),
            )
            if net_income is not None or fcf is not None:
                return fcf, net_income
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("DART financial lookup failed for %s year %s: %s", symbol, fiscal_year, exc)
    return None, None


def verify_financial_health(candidate: ScreenCandidate, config: AppConfig) -> HealthCheck:
    LOGGER.info(
        "Step 3/6 - Financial health check for %s %s (%s).",
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
                source="OpenDartReader",
                reason="passed" if passed else "DART FCF and/or net income failed the positive threshold",
            )
        else:
            health = fetch_yfinance_health(candidate, config)
    else:
        health = fetch_yfinance_health(candidate, config)
    LOGGER.info(
        "%s %s health result: passed=%s fcf=%s net_income=%s source=%s reason=%s",
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


def fetch_profile(candidate: ScreenCandidate, config: AppConfig) -> str:
    yf_symbol = get_yfinance_ticker_symbol(candidate)
    info = get_ticker_info(yf_symbol, config)
    summary = info.get("longBusinessSummary") or info.get("businessSummary")
    if candidate.market == "KR" and config.dart_api_key and OpenDartReader is not None:
        try:
            dart = OpenDartReader(config.dart_api_key)
            company = retry_call(
                f"DART company profile {candidate.symbol}",
                config,
                lambda: dart.company(candidate.symbol),
            )
            if company:
                corp_name = company.get("corp_name") or candidate.name
                ceo = company.get("ceo_nm") or "N/A"
                address = company.get("adres") or "N/A"
                stock_name = company.get("stock_name") or candidate.name
                intro = f"{corp_name} ({stock_name}); CEO: {ceo}; registered address: {address}."
                if summary:
                    return f"{intro} {summary}"
                return intro
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("DART profile failed for %s: %s", candidate.display_symbol, exc)
    return str(summary or f"No corporate profile summary was available for {candidate.name}.")


def statement_row_value(frame: pd.DataFrame, names: Iterable[str], col: Any) -> Optional[float]:
    row = first_existing_row(frame, names)
    if row is None or col not in row.index:
        return None
    return safe_float(row[col])


def fetch_historical_financials(candidate: ScreenCandidate, config: AppConfig) -> List[HistoricalFinancialRow]:
    yf_symbol = get_yfinance_ticker_symbol(candidate)
    LOGGER.info("Step 4/6 - Fetching historical financials for %s.", candidate.display_symbol)
    try:
        ticker = yf.Ticker(yf_symbol)
        financials = retry_call(f"yfinance annual financials {yf_symbol}", config, lambda: ticker.financials)
        cashflow = retry_call(f"yfinance annual cashflow {yf_symbol}", config, lambda: ticker.cashflow)
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("Historical financials unavailable for %s: %s", candidate.display_symbol, exc)
        return []
    if financials is None or financials.empty:
        return []
    columns = list(financials.columns[:3])
    rows: List[HistoricalFinancialRow] = []
    for col in columns:
        fiscal_year = str(pd.Timestamp(col).year) if not isinstance(col, str) else col
        revenue = statement_row_value(financials, ["Total Revenue", "Revenue"], col)
        ebit = statement_row_value(financials, ["EBIT", "Operating Income"], col)
        net_income = statement_row_value(financials, ["Net Income", "Net Income Common Stockholders"], col)
        fcf = statement_row_value(cashflow, ["Free Cash Flow"], col) if cashflow is not None and not cashflow.empty else None
        if fcf is None and cashflow is not None and not cashflow.empty:
            operating_cf = statement_row_value(cashflow, ["Operating Cash Flow", "Total Cash From Operating Activities"], col)
            capex = statement_row_value(cashflow, ["Capital Expenditure", "Capital Expenditures"], col)
            if operating_cf is not None and capex is not None:
                fcf = operating_cf + capex if capex < 0 else operating_cf - capex
        LOGGER.info(
            "%s FY%s: revenue=%s ebit=%s net_income=%s fcf=%s",
            candidate.display_symbol,
            fiscal_year,
            format_number(revenue, 0),
            format_number(ebit, 0),
            format_number(net_income, 0),
            format_number(fcf, 0),
        )
        rows.append(HistoricalFinancialRow(fiscal_year, revenue, ebit, net_income, fcf))
    return rows


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
    params = {"query": candidate.name, "display": 3, "sort": "date"}
    response = retry_call(
        f"Naver News {candidate.symbol}",
        config,
        lambda: requests.get(url, headers=headers, params=params, timeout=config.request_timeout),
    )
    if response.status_code in {401, 403, 429}:
        LOGGER.warning("Naver News status %s for %s: %s", response.status_code, candidate.display_symbol, response.text[:200])
        return []
    response.raise_for_status()
    items = response.json().get("items", [])[:3]
    return [
        {
            "title": strip_html_tags(item.get("title") or "Untitled"),
            "url": item.get("originallink") or item.get("link") or "#",
            "source": "Naver News",
            "published_at": item.get("pubDate") or "",
        }
        for item in items
    ]


def fetch_news(candidate: ScreenCandidate, config: AppConfig) -> List[Dict[str, str]]:
    LOGGER.info("Step 5/6 - Fetching news for %s.", candidate.display_symbol)
    try:
        return fetch_us_news(candidate, config) if candidate.market == "US" else fetch_kr_news(candidate, config)
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("News fetch failed for %s: %s", candidate.display_symbol, exc)
        return []


def build_stock_report(candidate: ScreenCandidate, health: HealthCheck, config: AppConfig) -> StockReport:
    profile = fetch_profile(candidate, config)
    historical = fetch_historical_financials(candidate, config)
    valuation, projections = fetch_valuation_and_projections(candidate, config)
    news = fetch_news(candidate, config)
    return StockReport(candidate, health, profile, historical, valuation, projections, news)


def html_table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    head = "".join(f"<th>{html.escape(header)}</th>" for header in headers)
    body = "\n".join(
        "<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>"
        for row in rows
    )
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def render_market_summary(rows: List[MarketSummaryRow]) -> str:
    table_rows = []
    for row in rows:
        table_rows.append(
            [
                html.escape(row.name),
                html.escape(row.symbol),
                format_number(row.close),
                format_pct(row.pct_change),
                html.escape(str(row.close_date.date()) if row.close_date is not None else "N/A"),
            ]
        )
    return html_table(["Asset", "Ticker", "Close", "Day Change", "Close Date"], table_rows)


def render_financial_rows(rows: List[HistoricalFinancialRow]) -> str:
    if not rows:
        return "<p class='muted'>Historical financials unavailable.</p>"
    return html_table(
        ["Fiscal Year", "Revenue", "EBIT", "Net Income", "Free Cash Flow"],
        [
            [
                html.escape(row.fiscal_year),
                format_number(row.revenue, 0),
                format_number(row.ebit, 0),
                format_number(row.net_income, 0),
                format_number(row.free_cash_flow, 0),
            ]
            for row in rows
        ],
    )


def render_key_values(values: Dict[str, Optional[float | str]]) -> str:
    return html_table(
        ["Metric", "Value"],
        [
            [
                html.escape(str(key)),
                html.escape(str(value)) if isinstance(value, str) else format_number(value),
            ]
            for key, value in values.items()
        ],
    )


def render_news(news: List[Dict[str, str]]) -> str:
    if not news:
        return "<p class='muted'>No recent relevant headlines returned by the configured news API.</p>"
    items = []
    for article in news[:3]:
        title = html.escape(article.get("title", "Untitled"))
        url = html.escape(article.get("url", "#"))
        source = html.escape(article.get("source", "News"))
        published = html.escape(article.get("published_at", ""))
        items.append(f"<li><a href='{url}'>{title}</a><br><span>{source} | {published}</span></li>")
    return "<ol>" + "".join(items) + "</ol>"


def render_stock_report(report: StockReport) -> str:
    c = report.candidate
    signal_table = html_table(
        ["Signal Date", "Previous Close", "Previous SMA", "Signal Close", "Signal SMA", "Signal Volume"],
        [[
            html.escape(str(c.signal_date.date())),
            format_number(c.previous_close),
            format_number(c.previous_sma),
            format_number(c.signal_close),
            format_number(c.signal_sma),
            format_number(c.signal_volume, 0),
        ]],
    )
    health_table = html_table(
        ["FCF", "Net Income", "Source", "Result"],
        [[
            format_number(report.health.free_cash_flow, 0),
            format_number(report.health.net_income, 0),
            html.escape(report.health.source),
            html.escape(report.health.reason),
        ]],
    )
    return f"""
    <section class="stock">
      <h3>{html.escape(c.name)} ({html.escape(c.display_symbol)})</h3>
      <h4>30-Day SMA Signal Verification</h4>
      {signal_table}
      <h4>Financial Health Verification</h4>
      {health_table}
      <h4>Corporate Profile</h4>
      <p>{html.escape(report.profile[:1800])}</p>
      <h4>Historical Financials</h4>
      {render_financial_rows(report.historical_financials)}
      <h4>Valuation</h4>
      {render_key_values(report.valuation)}
      <h4>Forward Projections</h4>
      {render_key_values(report.projections)}
      <h4>Relevant News</h4>
      {render_news(report.news)}
    </section>
    """


def render_na_slot(market: str, slot: int) -> str:
    return f"""
    <section class="stock muted-card">
      <h3>{html.escape(market)} Slot {slot}: N/A</h3>
      <p>No additional stock passed both the 30-day SMA crossover screen and the positive FCF / positive net income quality-control check.</p>
    </section>
    """


def build_email_html(
    market_summary: List[MarketSummaryRow],
    us_reports: List[StockReport],
    kr_reports: List[StockReport],
    config: AppConfig,
) -> str:
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    us_sections = "".join(render_stock_report(report) for report in us_reports)
    kr_sections = "".join(render_stock_report(report) for report in kr_reports)
    for idx in range(len(us_reports) + 1, config.picks_per_market + 1):
        us_sections += render_na_slot("US", idx)
    for idx in range(len(kr_reports) + 1, config.picks_per_market + 1):
        kr_sections += render_na_slot("Korea", idx)
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <style>
    body {{ font-family: Arial, Helvetica, sans-serif; color: #182026; line-height: 1.45; margin: 0; padding: 0; background: #f5f7f9; }}
    .container {{ max-width: 1120px; margin: 0 auto; padding: 24px; background: #ffffff; }}
    h1 {{ margin: 0 0 4px; font-size: 28px; }}
    h2 {{ margin-top: 28px; padding-bottom: 8px; border-bottom: 2px solid #d8dee6; font-size: 20px; }}
    h3 {{ margin-bottom: 8px; font-size: 18px; }}
    h4 {{ margin: 18px 0 8px; font-size: 14px; text-transform: uppercase; color: #46515c; letter-spacing: .03em; }}
    table {{ width: 100%; border-collapse: collapse; margin: 8px 0 16px; font-size: 13px; }}
    th, td {{ border: 1px solid #d8dee6; padding: 8px; text-align: left; vertical-align: top; }}
    th {{ background: #edf1f5; color: #182026; }}
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
  <div class="container">
    <h1>Stocks of the Day</h1>
    <p class="subtitle">Generated at {html.escape(generated_at)}. Signal date is the latest completed trading day available before the current data cutoff.</p>
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
    us_ohlcv = fetch_us_ohlcv_batch(US_UNIVERSE, config)
    us_candidates = detect_sma_crossovers("US", US_NAME_MAP, us_ohlcv, config)
    us_quality = select_quality_candidates(us_candidates, config)

    LOGGER.info("Step 2/6 - Running technical screening for Korean stocks.")
    kr_name_map = dict(KR_UNIVERSE)
    kr_ohlcv = {
        symbol: fetch_kr_ohlcv(symbol, config)
        for symbol, _name in KR_UNIVERSE
    }
    kr_candidates = detect_sma_crossovers("KR", kr_name_map, kr_ohlcv, config)
    kr_quality = select_quality_candidates(kr_candidates, config)

    LOGGER.info("Step 4/6 - Building detailed reports for selected US stocks.")
    us_reports = [build_stock_report(candidate, health, config) for candidate, health in us_quality]
    LOGGER.info("Step 4/6 - Building detailed reports for selected Korean stocks.")
    kr_reports = [build_stock_report(candidate, health, config) for candidate, health in kr_quality]

    subject_date = today_for_data_cutoff().strftime("%Y-%m-%d")
    email_html = build_email_html(market_summary, us_reports, kr_reports, config)
    send_email(f"Stocks of the Day - {subject_date}", email_html, config)
    LOGGER.info("Stocks of the Day pipeline completed.")


def run_test_loop(config: AppConfig) -> None:
    LOGGER.info(
        "TEST_RUN=true; sending to %s every %s seconds. Press Ctrl+C to stop.",
        ", ".join(config.email_to),
        config.test_interval_seconds,
    )
    iteration = 1
    while True:
        LOGGER.info("Starting test email iteration %s.", iteration)
        run_pipeline(config)
        LOGGER.info(
            "Test email iteration %s completed; sleeping for %s seconds.",
            iteration,
            config.test_interval_seconds,
        )
        iteration += 1
        time.sleep(config.test_interval_seconds)


def main() -> None:
    configure_logging()
    config = AppConfig.from_env()
    try:
        if config.test_run:
            run_test_loop(config)
        else:
            run_pipeline(config)
    except Exception as exc:  # noqa: BLE001
        LOGGER.exception("Pipeline failed: %s", exc)
        raise


if __name__ == "__main__":
    main()
