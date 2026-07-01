# FinDataHub

FinDataHub is the embedded market data layer used by FinClaw.

In this repository it is mounted by the main backend at `/datahub` by default, so
you usually do not need to start it as a separate service.

## Scope

- A-share instrument index
- Watchlist and positions
- Stock snapshots, daily bars, technical indicators, valuation, money flow, and fundamentals
- Market context and refresh logs
- Provider routing for free providers and optional Tushare

Runtime data is stored under `services/findatahub/data/` and is ignored by git.

## Standalone Mode

If you need to run it separately:

```powershell
python -m uvicorn services.findatahub.backend.app:app --host 127.0.0.1 --port 8700
```

Then configure FinClaw:

```env
FINCLAW_DATAHUB_MODE=http
DATAHUB_BASE_URL=http://127.0.0.1:8700
```
