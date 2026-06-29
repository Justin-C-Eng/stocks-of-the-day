# Stocks of the Day

Daily financial email agent for US and Korean equities.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp stocks_of_the_day.env.example .env
```

Fill in `.env`. Keep `DRY_RUN=true` for the first execution so the script writes an HTML preview without sending email.

## Run

```bash
python stocks_of_the_day.py
```

The script logs every pipeline stage: market closes, 30-day SMA calculations, crossover decisions, FCF and net income checks, valuation/projection pulls, news retrieval, and email delivery.

## Notes

- `yfinance` is used for US market data, US fundamentals, and fallback valuation/projection fields.
- `FinanceDataReader` is used for Korean daily OHLCV.
- `OpenDartReader` is used for Korean company profiles and Korean financial-health checks when DART provides the required fields.
- `NewsAPI` powers US news, and Naver Search News powers Korean news.
- Gmail requires an app password for SMTP; normal account passwords are rejected by Google.
