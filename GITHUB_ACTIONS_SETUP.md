# Setting Up GitHub Actions for Daily Scheduled Runs

This repo includes `.github/workflows/stocks-daily.yml`, which runs the
Stocks of the Day pipeline automatically every day at **22:00 UTC (07:00
KST / 09:00 JST)** and can also be triggered manually for testing.

## 1. Prerequisites

- A GitHub account with write access to this repository (to add secrets and
  trigger workflow runs).
- The workflow file already committed on the default branch (secrets and
  `workflow_dispatch` only work once the workflow exists there).

## 2. Register secrets

Go to **Settings > Secrets and variables > Actions > New repository secret**
and add each of the following. Paste values exactly - a stray leading/trailing
space (easy to introduce when copying) will break API calls or SMTP login.

| Secret | Required? | Notes |
| --- | --- | --- |
| `GMAIL_USER` | **Required** | Sender Gmail address, e.g. `juhanchang0606@gmail.com`. Also used as the `From` address. |
| `GMAIL_APP_PASSWORD` | **Required** | 16-character Gmail **app password** - not your normal Google account password. Generate one at https://myaccount.google.com/apppasswords (requires 2-Step Verification enabled). |
| `EMAIL_TO` | **Required** | Recipient address(es), comma-separated for multiple, e.g. `you@example.com,other@example.com`. |
| `DART_API_KEY` | Optional | Korean DART financial data. If unset, DART enrichment is skipped and the pipeline continues. |
| `NEWSAPI_KEY` | Optional | US company news. If unset, the US news section is skipped. |
| `NAVER_CLIENT_ID` / `NAVER_CLIENT_SECRET` | Optional | Korean company news (Naver Search API). If either is unset, the KR news section is skipped. |
| `ANTHROPIC_API_KEY` | Optional | Powers the Claude-generated "Recent Developments" news brief. If unset, that section degrades gracefully. |
| `ANTHROPIC_MODEL` | Optional | Defaults to `claude-haiku-4-5` if not set (this is a plain config value, not a secret - a repository *variable* would also work here if you prefer). |
| `SEC_CONTACT_NAME` / `SEC_CONTACT_EMAIL` | Optional | SEC EDGAR requires a real contact in the User-Agent header for its fair-access policy. If either is unset, SEC EDGAR is skipped and yfinance is used instead. |

Only the three marked **Required** are enforced by the script; it raises an
error (and the pipeline's own failure-alert email fires) if any of those
three are missing.

## 3. Testing

1. Commit and push `.github/workflows/stocks-daily.yml` (already done if
   you're reading this after setup).
2. Go to the **Actions** tab in GitHub.
3. Click **Stocks of the Day - Daily Run** in the left sidebar, then
   **Run workflow > Run workflow** to trigger it manually.
4. Wait 2-5 minutes and check the run log. Expand any red (failed) step to
   see the error.
5. Check the inbox at the address in `GMAIL_USER` for either the generated
   report email or a failure-alert email.
6. If something went wrong before an email could be sent, download the
   `stocks-of-the-day-log` artifact from the run's summary page (bottom of
   the page, kept for 7 days) for full logs.

## 4. Troubleshooting

- **No email arrives at all**: open the log artifact and search for SMTP
  errors. Double-check `GMAIL_USER` and `GMAIL_APP_PASSWORD` - the most
  common mistake is pasting the app password with the spaces Google displays
  it with (`abcd efgh ijkl mnop`); it should be pasted as one continuous
  string or with the spaces exactly as shown, but never with extra
  leading/trailing whitespace added by the paste.
- **API calls fail (Anthropic, DART, NewsAPI, Naver)**: confirm the
  corresponding secret is set with no trailing spaces. These are all
  optional though - the pipeline should still complete and email a report
  even if every optional integration is missing.
- **A step times out**: usually a transient network/API issue. Re-run
  manually from the Actions UI (automatic retries are not configured).
- **The "Previous Picks Review" section is always empty**: this section
  reads from `picks.db`, which is not committed to git (see `.gitignore`)
  and is instead persisted across runs via `actions/cache`. A brand-new
  repository, or one where the GitHub Actions cache was recently evicted
  (caches expire after 7 days unused, and the repo has a 10 GB total cache
  cap), will start with an empty history that fills in over subsequent
  daily runs.

## 5. Monitoring & maintenance

- Periodically check the **Actions** tab for failed runs. Recurring
  failures on specific dates often trace back to market holidays or API
  maintenance windows rather than a code bug.
- Keep `requirements.txt` reasonably current, especially `yfinance` and
  `OpenDartReader`/`FinanceDataReader`, which change more often than the
  rest of the dependency set. Pin versions when you do bump them to avoid
  surprise breakage.
- Before pushing changes that touch the pipeline, run it locally first
  (`python stocks_of_the_day.py` with `DRY_RUN=true` in your local `.env`)
  so you catch bugs before they hit the scheduled run.

## 6. Rate limits (for reference)

- **DART**: 100 calls/day (free tier). This pipeline uses roughly 10-20 per
  run; if you expand the stock universe, keep total calls under ~80/run.
- **SEC EDGAR**: fair-access policy recommends staying at or under 10
  requests/sec; the SEC client already paces calls accordingly.
- **NewsAPI**: 100 requests/day (free plan); this pipeline uses about 6.
- **Naver News**: 25,000 calls/day; comfortably within limits.
