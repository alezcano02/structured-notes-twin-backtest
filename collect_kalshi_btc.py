"""Download the data shared by the Graduating Project notebooks:
- Kalshi KXBTCD daily (5pm New York) events: markets near the money and their hourly candlesticks for the last 30 hours
- Binance BTCUSDT hourly klines
- Deribit DVOL (30-day implied volatility index) hourly
Everything is cached, so the script can be stopped and started again.
"""
import json
import time
from pathlib import Path

import numpy as np
import pandas as pd
import requests

out = Path(__file__).resolve().parent / "data" / "kalshi_btc"
out.mkdir(parents=True, exist_ok=True)
K = "https://api.elections.kalshi.com/trade-api/v2"
s = requests.Session()


def get(url, params=None):
    for i in range(6):
        try:
            r = s.get(url, params=params, timeout=60)
            if r.status_code == 429:
                time.sleep(2 + 2 * i)
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException:
            time.sleep(3 + 3 * i)
    raise RuntimeError(url)


# 1. events: the 5pm New York ones (ticker ends in 17)
ev_path = out / "events.csv"
if not ev_path.exists():
    cur, evs = None, []
    while True:
        p = {"series_ticker": "KXBTCD", "status": "settled", "limit": 200}
        if cur:
            p["cursor"] = cur
        d = get(K + "/events", p)
        evs += d["events"]
        cur = d.get("cursor")
        if not cur:
            break
    ev = pd.DataFrame([{"event_ticker": e["event_ticker"], "title": e.get("title")} for e in evs])
    ev = ev[ev["event_ticker"].str.endswith("17")]
    ev.to_csv(ev_path, index=False)
ev = pd.read_csv(ev_path)
print("daily 5pm events:", len(ev))

# 2. markets of each event (strikes, result, settlement value)
mk_path = out / "markets.csv"
if not mk_path.exists():
    filas = []
    for i, t in enumerate(ev["event_ticker"]):
        d = get(K + "/markets", {"event_ticker": t, "limit": 500})
        if not d["markets"]:
            # old settled markets moved to the historical endpoints of Kalshi
            d = {"markets": []}
            cur = None
            while True:
                p = {"event_ticker": t, "limit": 500}
                if cur:
                    p["cursor"] = cur
                h = get(K + "/historical/markets", p)
                d["markets"] += h["markets"]
                cur = h.get("cursor")
                if not cur or not h["markets"]:
                    break
        for m in d["markets"]:
            filas.append({"event_ticker": t, "ticker": m["ticker"], "strike": m.get("floor_strike"), "strike_type": m.get("strike_type"),
                          "result": m.get("result"), "expiration_value": m.get("expiration_value"), "open_time": m.get("open_time"),
                          "close_time": m.get("close_time"), "volume": m.get("volume_fp", m.get("volume"))})
        if i % 50 == 0:
            print("markets", i, len(filas))
    pd.DataFrame(filas).to_csv(mk_path, index=False)
mk = pd.read_csv(mk_path)
mk["expiration_value"] = pd.to_numeric(mk["expiration_value"], errors="coerce")
mk = mk[(mk["strike_type"] == "greater") & mk["expiration_value"].notna()]
# strikes within +/- 7% of the final price
mk["lm"] = np.log(mk["strike"] / mk["expiration_value"])
cerca = mk[mk["lm"].abs() < 0.07]
print("markets near the money:", len(cerca))

# 3. hourly candlesticks of the 30 hours before the close
corte = pd.Timestamp(get(K + "/historical/cutoff")["market_settled_ts"])
cd_dir = out / "candles"
cd_dir.mkdir(exist_ok=True)
from concurrent.futures import ThreadPoolExecutor


def bajar(m):
    f = cd_dir / f"{m['ticker']}.json"
    if f.exists():
        return
    fin = int(pd.Timestamp(m["close_time"]).timestamp())
    params = {"start_ts": fin - 30 * 3600, "end_ts": fin, "period_interval": 60}
    try:
        if pd.Timestamp(m["close_time"]) < corte:
            d = get(f"{K}/historical/markets/{m['ticker']}/candlesticks", params)
        else:
            d = get(f"{K}/series/KXBTCD/markets/{m['ticker']}/candlesticks", params)
    except RuntimeError:
        # a network error after 6 tries: we leave it for the next run
        print("failed", m["ticker"], flush=True)
        return
    f.write_text(json.dumps(d.get("candlesticks", [])))


pendientes = [m for _, m in cerca.iterrows() if not (cd_dir / f"{m['ticker']}.json").exists()]
print("candles to download:", len(pendientes))
with ThreadPoolExecutor(max_workers=8) as ex:
    for i, _ in enumerate(ex.map(bajar, pendientes)):
        if i % 1000 == 0:
            print("candles", i, "/", len(pendientes))

filas = []
for f in cd_dir.glob("*.json"):
    for c in json.loads(f.read_text()):
        # the live and the historical endpoints name the fields a little different
        val = lambda dic, k: (dic or {}).get(k + "_dollars", (dic or {}).get(k))
        filas.append({"ticker": f.stem, "ts": c["end_period_ts"],
                      "bid": float(val(c.get("yes_bid"), "close") or "nan"),
                      "ask": float(val(c.get("yes_ask"), "close") or "nan"),
                      "last": float(val(c.get("price"), "close") or "nan"),
                      "volume": float(c.get("volume_fp", c.get("volume")) or 0),
                      "open_interest": float(c.get("open_interest_fp", c.get("open_interest")) or 0)})
pd.DataFrame(filas).to_parquet(out / "candles_hourly.parquet")
print("candles rows:", len(filas))

# 4. Binance BTCUSDT hourly
bn_path = out / "binance_btcusdt_1h.parquet"
if not bn_path.exists():
    ini = int(pd.Timestamp("2024-03-01", tz="UTC").timestamp() * 1000)
    filas = []
    while True:
        d = get("https://api.binance.com/api/v3/klines", {"symbol": "BTCUSDT", "interval": "1h", "startTime": ini, "limit": 1000})
        if not d:
            break
        filas += d
        ini = d[-1][0] + 3600 * 1000
        if len(d) < 1000:
            break
    b = pd.DataFrame(filas).iloc[:, :6]
    b.columns = ["open_time", "open", "high", "low", "close", "volume"]
    b["ts"] = pd.to_datetime(b["open_time"], unit="ms", utc=True) + pd.Timedelta(hours=1)    # end of the hour
    b[["open", "high", "low", "close", "volume"]] = b[["open", "high", "low", "close", "volume"]].astype(float)
    b.drop(columns="open_time").to_parquet(bn_path)

# 5. Deribit DVOL hourly
dv_path = out / "deribit_dvol_1h.parquet"
if not dv_path.exists():
    fin = int(time.time() * 1000)
    ini = int(pd.Timestamp("2024-01-01", tz="UTC").timestamp() * 1000)
    filas = []
    while fin > ini:
        d = get("https://www.deribit.com/api/v2/public/get_volatility_index_data",
                {"currency": "BTC", "start_timestamp": ini, "end_timestamp": fin, "resolution": "3600"})["result"]
        filas += d["data"]
        if not d.get("continuation"):
            break
        fin = d["continuation"]
    v = pd.DataFrame(filas, columns=["t", "open", "high", "low", "dvol"]).drop_duplicates("t")
    v["ts"] = pd.to_datetime(v["t"], unit="ms", utc=True)
    v[["ts", "dvol"]].sort_values("ts").to_parquet(dv_path)
print("done")
