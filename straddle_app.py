"""
NIFTY SHORT STRADDLE — STREAMLIT APP
=====================================
Three variants, one shared data feed, results persisted to Google Sheets.

  FIXED_1_3X   both legs SL at 1.3x entry. On SL, partner goes naked and a
               replacement straddle deploys immediately.
  DYNAMIC_SL   same, but when a trend confirms, the hurt leg tightens to
               1.25x. Only straddle #1 is eligible (TIGHTEN_MODE).
In both variants, when one leg stops out its partner is carried on as a
naked leg until its own SL or a 21-EMA reversal closes it.

SHEETS
------
  Live_Status      constant. The current day's full leg ledger, rewritten
                   every cycle. Also what the engine reads to resume.
  Log_<date>       one row per strategy every 3 minutes, open to close.
  Trades_<date>    one row per leg, appended when the leg closes.

WHY THERE IS NO LIVE POLLING LOOP
---------------------------------
Streamlit Cloud sleeps on inactivity and a browser session cannot hold a
six-hour loop. It does not need to. Paper trading places no orders and acts
only on COMPLETED candles, so replaying a finished session in one pass
produces exactly the trades a 3-minute poller would have produced. Press
"Run day" once after 15:30 and the result is identical.

SETUP
-----
repo: straddle_app.py, requirements.txt
      (streamlit, smartapi-python, pyotp, gspread, google-auth, pandas, numpy, requests)

Streamlit -> Settings -> Secrets:

    [angel]
    api_key = "..."
    client_code = "..."
    password = "..."
    totp_secret = "..."

    [sheets]
    sheet_id = "..."

    [gcp_service_account]
    ... contents of service_account.json as TOML ...

Never put credentials in the repo.
"""

import datetime as dt
import threading
import time
from zoneinfo import ZoneInfo
from dataclasses import dataclass, field

import altair as alt
import numpy as np
import pandas as pd
import requests
import streamlit as st

st.set_page_config(page_title="Straddle Desk", page_icon="◆", layout="wide",
                   initial_sidebar_state="expanded")

# ============================== CONSTANTS ===================================
# Streamlit Cloud runs in UTC. Every time decision here — session state, the
# candle-fetch cap, "today" — must be IST or the app silently asks the broker
# for the wrong window and finds no data.
IST = ZoneInfo("Asia/Kolkata")


def now_ist():
    return dt.datetime.now(IST).replace(tzinfo=None)


def today_ist():
    return dt.datetime.now(IST).date()


NIFTY_INDEX_TOKEN = "99926000"
NIFTY_FUT_TOKEN = "68407"          # front month — update after each roll
INDIA_VIX_TOKEN = "99926017"
NSE = "NSE"
NFO = "NFO"
INTERVAL = "THREE_MINUTE"
API_SLEEP = 0.35

LOT_SIZE = 75
STRIKE_STEP = 50
SL_MULTIPLIER = 1.3
TIGHT_SL_MULTIPLIER = 1.25

EMA_PERIOD = 21
ADX_PERIOD = 14
TREND_ADX_PERIOD = 11
ADX_MIN = 20.0
FALLING_LOOKBACK = 3

VIX_LOW, VIX_HIGH = 10.0, 14.0
KC_PERIOD, KC_ATR_MULT = 20, 1.0
CHOP_PERIOD = 14
LOXX_PERIOD, LOXX_DEV, LOXX_WIDTH_MULT = 40, 1.0, 0.9

MIN_TREND_BARS = (LOXX_PERIOD - 1) + int(LOXX_PERIOD ** 0.5)            # 45
FULL_WARMUP_BARS = 2*(LOXX_PERIOD - 1) + int(LOXX_PERIOD ** 0.5) - 1    # 83
WARMUP_CALENDAR_DAYS = 5

ENTRY_START = dt.time(9, 45)
ENTRY_END = dt.time(14, 30)
HARD_EXIT = dt.time(15, 10)

MAX_STRADDLES_PER_DAY = 2
TIGHTEN_MODE = "FIRST_STRADDLE_ONLY"   # FIRST_STRADDLE_ONLY | FRESH_TREND | ALWAYS
REDEPLOY_IMMEDIATE = "IMMEDIATE"

# A replacement straddle after an SL hit will not deploy past this time.
REDEPLOY_CUTOFF = dt.time(14, 45)

# Strikes either side of the rounded ATM to test. The winner is the strike
# whose CE and PE premiums are closest — the market's true ATM, which the
# forward basis can push one strike away from a simple spot rounding.
STRIKE_SCAN = 1

# name,          dynamic_sl, redeploy_mode,      max_open_legs
FIXED   = ("FIXED_1_3X", False, REDEPLOY_IMMEDIATE, 3)
DYNAMIC = ("DYNAMIC_SL", True,  REDEPLOY_IMMEDIATE, 3)

VARIANTS_LIVE = [FIXED, DYNAMIC]
VARIANTS_BACKTEST = [FIXED, DYNAMIC]

# ---- SHEET LAYOUT -------------------------------------------------------
# Sheet 1  constant name. The full leg ledger for the current day, rewritten
#          every cycle: open legs with live LTP, closed legs with realised
#          P&L. This is also what the engine reads to resume after a restart.
# Sheet 2  Log_<date>. One row per strategy every 3 minutes, open to close.
# Sheet 3  Trades_<date>. One row per leg, appended when the leg closes.
TAB_STATUS = "Live_Status"


def tab_log(day):
    return f"Log_{day}"


def tab_trades(day):
    return f"Trades_{day}"


STATUS_HEADERS = ["updated_at", "trading_date", "strategy", "status", "straddle_num",
                  "leg_symbol", "opt_type", "strike", "qty", "entry_time",
                  "entry_price", "sl", "ltp", "exit_time", "exit_price",
                  "exit_reason", "leg_pnl", "naked", "sl_tightened", "carry_bars"]

LOG_HEADERS = ["timestamp", "trading_date", "strategy", "spot", "trend",
               "straddles", "open_legs", "closed_legs", "realised_pnl",
               "unrealised_pnl", "total_pnl", "legs_detail"]

TRADE_HEADERS = ["trading_date", "strategy", "straddle_num", "leg_symbol", "opt_type",
                 "strike", "qty", "entry_time", "entry_price", "sl", "exit_time",
                 "exit_price", "exit_reason", "pnl", "naked", "sl_tightened",
                 "carry_bars"]

SUMMARY_HEADERS = ["run_type", "strategy", "trading_date", "total_pnl", "num_legs",
                   "num_straddles", "naked_legs", "reversal_exits", "redeploys"]

HEARTBEAT_HEADERS = ["trading_date", "bar_time", "spot", "adx", "adxr", "chop",
                     "loxx_width", "loxx_width_avg", "width_ok", "momentum",
                     "loxx_up", "loxx_dn", "kc_break_up", "kc_break_dn", "trend"]

MARKET_OPEN = dt.time(9, 15)
MARKET_CLOSE = dt.time(15, 30)
CANDLE_MIN = 3
POLL_BUFFER_SEC = 15

MASTER_URL = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
MASTER_FALLBACK = "https://margincalculator.angelone.in/OpenAPI_File/files/OpenAPIScripMaster.json"


# ============================== CONNECTIONS =================================
def angel_login():
    from SmartApi import SmartConnect
    import pyotp
    c = st.secrets["angel"]
    o = SmartConnect(api_key=c["api_key"])
    r = o.generateSession(c["client_code"], c["password"], pyotp.TOTP(c["totp_secret"]).now())
    if not r.get("status"):
        raise RuntimeError(f"Angel login failed: {r.get('message', r)}")
    return o


SHEET_KEYS = {"LIVE": "live_sheet_id", "BACKTEST": "backtest_sheet_id"}


@st.cache_resource(show_spinner=False)
def sheet(kind):
    """Live and backtest results go to separate spreadsheets so a backtest
    sweep can never be mistaken for accumulated live paper history."""
    import gspread
    from google.oauth2.service_account import Credentials
    creds = Credentials.from_service_account_info(
        dict(st.secrets["gcp_service_account"]),
        scopes=["https://www.googleapis.com/auth/spreadsheets"])
    key = st.secrets["sheets"][SHEET_KEYS[kind]]
    return gspread.authorize(creds).open_by_key(key)


def _j(v):
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return ""
    if isinstance(v, (np.bool_, bool)):
        return bool(v)
    if isinstance(v, np.integer):
        return int(v)
    if isinstance(v, np.floating):
        f = float(v)
        return "" if np.isnan(f) else round(f, 4)
    if isinstance(v, (dt.date, dt.datetime, pd.Timestamp)):
        return str(v)
    return v


def append_rows(kind, tab, headers, rows):
    if not rows:
        return 0
    import gspread
    sh = sheet(kind)
    try:
        ws = sh.worksheet(tab)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=tab, rows=5000, cols=max(len(headers), 12))
        ws.append_row(headers)
    ws.append_rows([[_j(v) for v in r] for r in rows], value_input_option="USER_ENTERED")
    return len(rows)


def write_snapshot(T, S, O, day, stamp):
    """A row per strategy per snapshot, so an intraday poll leaves a trail
    without touching the end-of-day record in Straddle_Summary."""
    rows = []
    for v in VARIANTS_LIVE:
        nm = v[0]
        ts_ = T[T.strategy == nm] if not T.empty else T
        os_ = O[O.strategy == nm] if not O.empty else O
        pnl = pd.to_numeric(ts_.pnl, errors="coerce").sum() if not ts_.empty else 0
        det = "; ".join(f"{r.leg_symbol}@{r.entry_price}" for r in os_.itertuples()) \
              if not os_.empty else ""
        rows.append([stamp, str(day), nm, round(float(pnl), 2), len(ts_), len(os_),
                     int(ts_.straddle_num.max()) if not ts_.empty else 0, det])
    return append_rows("LIVE", TAB_SNAPSHOT, SNAP_HEADERS, rows)


def replace_tab(kind, tab, headers, rows):
    """Rewrite a tab whole. Used for the status sheet, which mirrors current
    state rather than accumulating history."""
    import gspread
    sh = sheet(kind)
    try:
        ws = sh.worksheet(tab)
        ws.clear()
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=tab, rows=400, cols=len(headers))
    ws.update(values=[headers] + [[_j(v) for v in r] for r in rows],
              value_input_option="USER_ENTERED")
    return len(rows)


def rows_from_df(df, headers):
    """Read-back rows in header order, so a rewritten tab keeps its shape even
    if a column is missing from the sheet."""
    if df.empty:
        return []
    out = []
    for _, r in df.iterrows():
        out.append([r[h] if h in df.columns else "" for h in headers])
    return out


def list_tabs(kind, prefix):
    try:
        return sorted(w.title for w in sheet(kind).worksheets()
                      if w.title.startswith(prefix))
    except Exception:
        return []


def read_tab(kind, tab):
    import gspread
    try:
        ws = sheet(kind).worksheet(tab)
    except gspread.WorksheetNotFound:
        return pd.DataFrame()
    v = ws.get_all_records()
    return pd.DataFrame(v) if v else pd.DataFrame()


# ============================== MARKET DATA =================================
# ============================== DISCORD =====================================
def discord_url():
    try:
        return st.secrets["discord"]["webhook"]
    except Exception:
        return None


def discord(msg, tag=""):
    """Fire-and-forget notification. A webhook failure must never interrupt
    the engine, so every error is swallowed after one retry."""
    url = discord_url()
    if not url:
        return False
    body = {"content": (f"{tag} " if tag else "") + msg}
    for attempt in range(2):
        try:
            r = requests.post(url, json=body, timeout=6)
            if r.status_code in (200, 204):
                return True
            if r.status_code == 429:          # rate limited
                time.sleep(float(r.headers.get("Retry-After", 2)))
                continue
            return False
        except Exception:
            time.sleep(1)
    return False


CANDLE_COLS = ["ts", "open", "high", "low", "close", "volume"]


def empty_candles():
    """An empty frame WITH the expected columns. A bare pd.DataFrame() has no
    columns, so downstream `.ts` access raises AttributeError instead of just
    being empty — which is how a failed VIX or futures fetch used to crash a run."""
    return pd.DataFrame(columns=CANDLE_COLS)


def candles(smart, token, exchange, frm, to):
    p = {"exchange": exchange, "symboltoken": str(token), "interval": INTERVAL,
         "fromdate": frm.strftime("%Y-%m-%d %H:%M"), "todate": to.strftime("%Y-%m-%d %H:%M")}
    for a in range(3):
        try:
            r = smart.getCandleData(p)
        except Exception:
            time.sleep(API_SLEEP * 4 ** a); continue
        time.sleep(API_SLEEP)
        if r.get("status") and r.get("data"):
            df = pd.DataFrame(r["data"], columns=CANDLE_COLS)
            df["ts"] = pd.to_datetime(df["ts"]).dt.tz_localize(None)
            return df
        time.sleep(API_SLEEP * 4 ** a)
    return empty_candles()


@st.cache_data(ttl=3600, show_spinner=False)
def option_universe():
    import requests
    try:
        r = requests.get(MASTER_URL, timeout=30); r.raise_for_status()
    except Exception:
        r = requests.get(MASTER_FALLBACK, timeout=30); r.raise_for_status()
    lookup, exp = {}, set()
    for i in r.json():
        if i.get("name") != "NIFTY" or i.get("instrumenttype") != "OPTIDX":
            continue
        lookup[i["symbol"]] = i["token"]
        try:
            exp.add(dt.datetime.strptime(i["expiry"], "%d%b%Y").date())
        except Exception:
            pass
    return lookup, sorted(exp)


def nearest_expiry(expiries, day):
    f = [e for e in expiries if e >= day]
    if not f:
        raise RuntimeError(f"No NIFTY expiry on/after {day}")
    return f[0]


def expiry_choices(expiries, day, n=6):
    """Expiries on or after `day`, nearest first."""
    return [e for e in expiries if e >= day][:n]


def opt_symbol(expiry, strike, ot):
    return f"NIFTY{expiry.strftime('%d%b%y').upper()}{strike}{ot}"


# ============================== INDICATORS ==================================
def true_range(df):
    h, l, c = df.high, df.low, df.close
    return pd.concat([h-l, (h-c.shift()).abs(), (l-c.shift()).abs()], axis=1).max(axis=1)


def compute_adx_adxr(df, period=ADX_PERIOD):
    h, l = df.high, df.low
    up, dn = h.diff(), -l.diff()
    pdm = np.where((up > dn) & (up > 0), up, 0.0)
    mdm = np.where((dn > up) & (dn > 0), dn, 0.0)
    atr = true_range(df).ewm(alpha=1/period, adjust=False).mean()
    pdi = 100*pd.Series(pdm, index=df.index).ewm(alpha=1/period, adjust=False).mean()/atr
    mdi = 100*pd.Series(mdm, index=df.index).ewm(alpha=1/period, adjust=False).mean()/atr
    dx = 100*(pdi-mdi).abs()/(pdi+mdi)
    adx = dx.ewm(alpha=1/period, adjust=False).mean()
    return adx, (adx+adx.shift(period))/2.0, pdi, mdi


def is_falling(s, idx, lookback=FALLING_LOOKBACK):
    w = s.iloc[max(0, idx-lookback+1): idx+1]
    if len(w) < lookback or w.isna().any():
        return False
    return all(w.iloc[i] > w.iloc[i+1] for i in range(len(w)-1))


def compute_ema(s, period=EMA_PERIOD):
    return s.ewm(span=period, adjust=False).mean()


def compute_keltner(df, period=KC_PERIOD, mult=KC_ATR_MULT):
    basis = df.close.ewm(span=period, adjust=False).mean()
    atr = true_range(df).ewm(alpha=1/period, adjust=False).mean()
    return basis+mult*atr, basis, basis-mult*atr


def compute_chop(df, period=CHOP_PERIOD):
    atr_sum = true_range(df).rolling(period).sum()
    den = (df.high.rolling(period).max()-df.low.rolling(period).min()).replace(0, np.nan)
    return 100*np.log10(atr_sum/den)/np.log10(period)


def compute_lwma(s, p):
    w = np.arange(1, p+1)
    return s.rolling(p).apply(lambda x: (x*w).sum()/w.sum(), raw=True)


def compute_loxx_hlhvb(df, period=LOXX_PERIOD, dev=LOXX_DEV):
    """Loxx HLHVB. The MIDLINE supplies direction via its slope; band width is
    a separate squeeze filter. Using width alone as the trend test was why
    trend detection almost never fired."""
    me = compute_lwma(2*compute_lwma(df.close, int(period/2)) - compute_lwma(df.close, period),
                      int(period ** 0.5))
    d = (df.high-df.low).rolling(period).std(ddof=1)
    return me+d*dev, me, me-d*dev


def _rising(s):
    return bool(s.iloc[-1] > s.iloc[-2]) if len(s) > 1 and s.iloc[-2:].notna().all() else False


def _falling(s):
    return bool(s.iloc[-1] < s.iloc[-2]) if len(s) > 1 and s.iloc[-2:].notna().all() else False


# ============================== STATE =======================================
@dataclass
class Leg:
    symbol: str
    opt_type: str
    strike: int
    entry_price: float
    sl: float
    qty: int
    entry_time: dt.datetime
    entry_idx: int
    straddle_num: int = 1
    naked: bool = False
    naked_since: int = None
    sl_tightened: bool = False
    exit_price: float = None
    exit_time: dt.datetime = None
    exit_reason: str = None
    exit_idx: int = None

    @property
    def carry_bars(self):
        if self.naked_since is None or self.exit_idx is None:
            return 0
        return self.exit_idx - self.naked_since


@dataclass
class DayState:
    open_legs: list = field(default_factory=list)
    closed_legs: list = field(default_factory=list)
    straddle_count: int = 0
    redeploys: int = 0
    done: bool = False

    def pnl(self):
        return sum((l.entry_price-l.exit_price)*l.qty
                   for l in self.closed_legs if l.exit_price is not None)


# ============================== ENGINE ======================================
class StraddleEngine:
    def __init__(self, name, dynamic_sl, redeploy_mode, max_open_legs,
                 smart, lookup, expiry, option_cache):
        self.name = name
        self.dynamic_sl = dynamic_sl
        self.redeploy_mode = redeploy_mode
        self.max_open_legs = max_open_legs
        self.smart = smart
        self.lookup = lookup
        self.expiry = expiry
        self.cache = option_cache
        self.state = DayState()
        self.heartbeat = []
        self._prev_trend = None
        self._diag = None
        self.log = []
        self.notify = False          # only the live engine turns this on
        self._logged = set()
        self.last_ts = None

    # ---- data ----
    def option_df(self, day, strike, ot):
        key = (day, strike, ot)
        if key in self.cache:
            return self.cache[key]
        sym = opt_symbol(self.expiry, strike, ot)
        tok = self.lookup.get(sym)
        if tok is None:
            self.cache[key] = (sym, pd.DataFrame())
            return self.cache[key]
        df = candles(self.smart, tok, NFO,
                     dt.datetime.combine(day, dt.time(9, 15)),
                     dt.datetime.combine(day, dt.time(15, 30)))
        self.cache[key] = (sym, df)
        return self.cache[key]

    # ---- signals ----
    def entry_ok(self, nifty, vix, i, now):
        if not (ENTRY_START <= now.time() <= ENTRY_END):
            return False
        if i < ADX_PERIOD*2:
            return False
        adx, adxr, _, _ = compute_adx_adxr(nifty.iloc[: i+1], ADX_PERIOD)
        if vix is None or vix.empty or "ts" not in vix.columns:
            return False
        vr = vix[vix.ts <= nifty.ts.iloc[i]]
        if vr.empty:
            return False
        v = float(vr.close.iloc[-1])
        cond = (adx.iloc[-1] < adxr.iloc[-1] and is_falling(adx, len(adx)-1)
                and is_falling(adxr, len(adxr)-1))
        return bool(cond and VIX_LOW <= v <= VIX_HIGH)

    def detect_trend(self, nifty, i):
        self._diag = None
        if i < MIN_TREND_BARS:
            return None
        sub = nifty.iloc[: i+1]
        adx, adxr, pdi, mdi = compute_adx_adxr(sub, TREND_ADX_PERIOD)
        chop = compute_chop(sub)
        kcu, kcb, kcl = compute_keltner(sub)
        lu, lme, ld = compute_loxx_hlhvb(sub)
        width = lu-ld
        wavg = width.rolling(LOXX_PERIOD).mean()
        close = float(sub.close.iloc[-1]); hi = float(sub.high.iloc[-1]); lo = float(sub.low.iloc[-1])

        # NaN width_avg means the squeeze filter has not warmed up; skip it
        # rather than vetoing the whole check.
        wa = wavg.iloc[-1]
        width_ok = bool(width.iloc[-1] <= wa*LOXX_WIDTH_MULT) if pd.notna(wa) else True

        a, ar = adx.iloc[-1], adxr.iloc[-1]
        momentum = bool(pd.notna(a) and pd.notna(ar) and a > ADX_MIN and a > ar
                        and _rising(adx) and _rising(adxr) and _falling(chop))
        up_ok = bool(pd.notna(ld.iloc[-1]) and close > ld.iloc[-1] and _rising(lme))
        dn_ok = bool(pd.notna(lu.iloc[-1]) and close < lu.iloc[-1] and _falling(lme))
        bku = bool(close > kcu.iloc[-1]); bkd = bool(close < kcl.iloc[-1])

        trend = None
        if bku and _rising(kcb) and lo > kcl.iloc[-1] and pdi.iloc[-1] > mdi.iloc[-1] \
                and up_ok and momentum and width_ok:
            trend = "UP"
        elif bkd and _falling(kcb) and hi < kcu.iloc[-1] and mdi.iloc[-1] > pdi.iloc[-1] \
                and dn_ok and momentum and width_ok:
            trend = "DOWN"

        self._diag = [round(close, 2), _j(a), _j(ar), _j(chop.iloc[-1]),
                      _j(width.iloc[-1]), _j(wa), width_ok, momentum,
                      up_ok, dn_ok, bku, bkd, trend or ""]
        return trend

    def _may_tighten(self, leg, fresh):
        if leg.sl_tightened:
            return False
        if TIGHTEN_MODE == "FIRST_STRADDLE_ONLY":
            return leg.straddle_num == 1
        if TIGHTEN_MODE == "FRESH_TREND":
            return fresh
        return True

    # ---- positions ----
    def deploy(self, day, spot, fut, now, idx, tag="ENTRY"):
        if tag != "ENTRY" and now.time() > REDEPLOY_CUTOFF:
            self.log.append(f"{self.name}: {tag} skipped — past {REDEPLOY_CUTOFF:%H:%M}")
            return False
        if self.state.straddle_count >= MAX_STRADDLES_PER_DAY:
            return False
        if len(self.state.open_legs) + 2 > self.max_open_legs:
            return False

        base_price = fut if day.weekday() == 0 else spot
        atm = int(round(base_price / STRIKE_STEP) * STRIKE_STEP)
        candidates = [atm + k * STRIKE_STEP
                      for k in range(-STRIKE_SCAN, STRIKE_SCAN + 1)]

        best_diff, best_strike, best_legs = float("inf"), None, []
        probe = []
        for test_strike in candidates:
            temp_legs, valid = [], True
            for ot in ("CE", "PE"):
                sym, df = self.option_df(day, test_strike, ot)
                if df.empty:
                    valid = False; break
                row = df[df.ts <= now]
                if row.empty:
                    valid = False; break
                temp_legs.append((sym, ot, float(row.close.iloc[-1])))
            if not (valid and len(temp_legs) == 2):
                probe.append(f"{test_strike}: no data")
                continue
            diff = abs(temp_legs[0][2] - temp_legs[1][2])
            probe.append(f"{test_strike}: CE {temp_legs[0][2]:.1f} "
                         f"PE {temp_legs[1][2]:.1f} d{diff:.1f}")
            if diff < best_diff:
                best_diff, best_strike, best_legs = diff, test_strike, temp_legs

        self.log.append(f"{self.name}: scan (spot {base_price:.1f}, atm {atm}) — "
                        + " | ".join(probe))
        if best_strike is None:
            self.log.append(f"{self.name}: {tag} failed — no option data for {candidates}")
            return False

        strike, legs = best_strike, best_legs

        self.state.straddle_count += 1
        n = self.state.straddle_count
        for sym, ot, px in legs:
            self.state.open_legs.append(Leg(sym, ot, strike, px, px*SL_MULTIPLIER,
                                            LOT_SIZE, now, idx, straddle_num=n))
        self.log.append(f"{self.name}: {tag} straddle #{n} @ {now:%H:%M} strike {strike} "
                        f"CE {legs[0][2]:.2f} PE {legs[1][2]:.2f}")
        if self.notify:
            ce, pe = legs[0], legs[1]
            discord(
                f"**{self.name}** — {tag} straddle #{n}\n"
                f"`{now:%H:%M}`  strike **{strike}**  (spot {spot:.1f})\n"
                f"SELL `{ce[0]}` @ **{ce[2]:.2f}**  ·  SL {ce[2]*SL_MULTIPLIER:.2f}\n"
                f"SELL `{pe[0]}` @ **{pe[2]:.2f}**  ·  SL {pe[2]*SL_MULTIPLIER:.2f}",
                tag="🟢")
        return True

    def sl_fill(self, leg, day, now):
        _, df = self.option_df(day, leg.strike, leg.opt_type)
        if df.empty:
            return None
        row = df[df.ts == now]
        if row.empty:
            # Illiquid strikes skip bars with no trades. Falling back to the
            # newest bar at or before `now` keeps the stop evaluated instead
            # of silently skipping it.
            row = df[df.ts <= now].tail(1)
            if row.empty:
                return None
        hi, cl = float(row.high.iloc[0]), float(row.close.iloc[0])
        if hi >= leg.sl:
            return max(leg.sl, cl) if cl >= leg.sl else leg.sl
        return None

    def naked_reversal(self, leg, day, now):
        _, df = self.option_df(day, leg.strike, leg.opt_type)
        h = df[df.ts <= now] if not df.empty else pd.DataFrame()
        if len(h) < EMA_PERIOD+2:
            return False
        e = compute_ema(h.close)
        return bool(h.close.iloc[-2] <= e.iloc[-2] and h.close.iloc[-1] > e.iloc[-1])

    def exit_leg(self, leg, px, reason, now, idx):
        leg.exit_price = px; leg.exit_time = now
        leg.exit_reason = reason; leg.exit_idx = idx
        self.state.open_legs.remove(leg)
        self.state.closed_legs.append(leg)
        pnl = (leg.entry_price - px) * leg.qty
        self.log.append(f"{self.name}: exit {leg.symbol} @ {px:.2f} ({reason}) "
                        f"pnl {pnl:+.0f}")
        if self.notify:
            icon = {"SL_HIT": "🔴", "NAKED_SL_HIT": "🔴",
                    "REVERSAL_EMA_CROSS": "🟡", "EOD_15_10": "⚪",
                    "FORCED_DAY_END": "⚪"}.get(reason, "🔵")
            extra = ""
            if leg.naked:
                extra += f"  ·  carried naked {leg.carry_bars} bars"
            if leg.sl_tightened:
                extra += "  ·  SL was tightened"
            discord(
                f"**{self.name}** — exit  ({reason})\n"
                f"`{now:%H:%M}`  `{leg.symbol}`\n"
                f"entry {leg.entry_price:.2f} → exit **{px:.2f}**  ·  "
                f"SL {leg.sl:.2f}\n"
                f"P&L **{pnl:+,.0f}**  ·  day total {self.state.pnl() + pnl:+,.0f}{extra}",
                tag=icon)

    def partner(self, leg):
        c = [l for l in self.state.open_legs
             if l.opt_type != leg.opt_type and not l.naked
             and l.straddle_num == leg.straddle_num]
        return c[0] if c else None

    def on_bar(self, day, nifty, i, local_i, now, spot, fut):
        """local_i is unused; kept so run_day and the threaded engine
        can call this identically."""
        if now.time() >= HARD_EXIT and not self.state.done:
            for leg in list(self.state.open_legs):
                _, df = self.option_df(day, leg.strike, leg.opt_type)
                r = df[df.ts <= now]
                self.exit_leg(leg, float(r.close.iloc[-1]) if not r.empty else leg.entry_price,
                              "EOD_15_10", now, i)
            self.state.done = True
        if self.state.done:
            return

        if self.dynamic_sl:
            trend = self.detect_trend(nifty, i)
            if self._diag is not None:
                self.heartbeat.append([str(day), now.strftime("%H:%M:%S")] + self._diag)
            fresh = trend is not None and trend != self._prev_trend
            self._prev_trend = trend
            if trend and self.state.open_legs:
                hurt = "CE" if trend == "UP" else "PE"
                for leg in self.state.open_legs:
                    if leg.opt_type == hurt and self._may_tighten(leg, fresh):
                        leg.sl = leg.entry_price*TIGHT_SL_MULTIPLIER
                        leg.sl_tightened = True
                        self.log.append(f"{self.name}: {leg.symbol} SL -> {leg.sl:.2f} "
                                        f"(trend {trend}, straddle #{leg.straddle_num})")
                        if self.notify:
                            discord(f"**{self.name}** — SL tightened\n"
                                    f"`{now:%H:%M}`  `{leg.symbol}`\n"
                                    f"SL {leg.entry_price*SL_MULTIPLIER:.2f} → "
                                    f"**{leg.sl:.2f}**  (trend {trend})", tag="🟠")

        for leg in list(self.state.open_legs):
            fill = self.sl_fill(leg, day, now)
            if fill is not None and not leg.naked:
                self.exit_leg(leg, fill, "SL_HIT", now, i)
                p = self.partner(leg)
                if p:
                    p.naked = True; p.naked_since = i
                    self.log.append(f"{self.name}: {p.symbol} now naked")
                    if self.notify:
                        discord(f"**{self.name}** — leg now naked\n"
                                f"`{now:%H:%M}`  `{p.symbol}`  entry {p.entry_price:.2f}  "
                                f"SL {p.sl:.2f}", tag="⚠️")
                if self.redeploy_mode == REDEPLOY_IMMEDIATE \
                        and self.state.straddle_count < MAX_STRADDLES_PER_DAY:
                    self.deploy(day, spot, fut, now, i)
                continue
            if leg.naked:
                if fill is not None:
                    self.exit_leg(leg, fill, "NAKED_SL_HIT", now, i)
                    continue
                if self.naked_reversal(leg, day, now):
                    _, df = self.option_df(day, leg.strike, leg.opt_type)
                    r = df[df.ts == now]
                    self.exit_leg(leg, float(r.close.iloc[0]) if not r.empty else leg.entry_price,
                                  "REVERSAL_EMA_CROSS", now, i)
                    continue

        if not self.state.open_legs and self.state.straddle_count < MAX_STRADDLES_PER_DAY:
            if self.entry_ok(nifty.iloc[: i+1], self._vix, i, now):
                self.deploy(day, spot, fut, now, i)

    def prepare(self, day):
        """Reset for a fresh session. Used by the threaded live engine, which
        then feeds bars in one at a time rather than replaying the day."""
        self.state = DayState()
        self.heartbeat = []
        self.log = []
        self._prev_trend = None
        self._logged = set()
        self.last_ts = None

    def leg_ltp(self, day, leg, now):
        _, df = self.option_df(day, leg.strike, leg.opt_type)
        r = df[df.ts <= now] if not df.empty else pd.DataFrame()
        return float(r.close.iloc[-1]) if not r.empty else leg.entry_price

    def unrealised(self, day, now):
        return sum((l.entry_price - self.leg_ltp(day, l, now)) * l.qty
                   for l in self.state.open_legs)

    def status_rows(self, day, now, stamp):
        """Sheet 1 — the whole day's leg ledger, open and closed, leg-wise P&L."""
        rows = []
        for l in self.state.open_legs:
            ltp = self.leg_ltp(day, l, now)
            rows.append([stamp, str(day), self.name, "OPEN", l.straddle_num,
                         l.symbol, l.opt_type, l.strike, l.qty,
                         l.entry_time.strftime("%H:%M:%S"), round(l.entry_price, 2),
                         round(l.sl, 2), round(ltp, 2), "", "", "",
                         round((l.entry_price - ltp) * l.qty, 2),
                         l.naked, l.sl_tightened, l.carry_bars])
        for l in self.state.closed_legs:
            pnl = (l.entry_price - l.exit_price) * l.qty if l.exit_price is not None else 0
            rows.append([stamp, str(day), self.name, "CLOSED", l.straddle_num,
                         l.symbol, l.opt_type, l.strike, l.qty,
                         l.entry_time.strftime("%H:%M:%S"), round(l.entry_price, 2),
                         round(l.sl, 2), "",
                         l.exit_time.strftime("%H:%M:%S") if l.exit_time else "",
                         round(l.exit_price, 2) if l.exit_price is not None else "",
                         l.exit_reason or "", round(pnl, 2),
                         l.naked, l.sl_tightened, l.carry_bars])
        return rows

    def log_row(self, day, now, spot, stamp):
        """Sheet 2 — one row per strategy per 3-minute bar."""
        detail = []
        for l in self.state.open_legs:
            ltp = self.leg_ltp(day, l, now)
            detail.append(f"{l.opt_type}{l.strike} E{l.entry_price:.1f} "
                          f"L{ltp:.1f} SL{l.sl:.1f}" + (" NAKED" if l.naked else ""))
        real = self.state.pnl()
        unreal = self.unrealised(day, now)
        return [stamp, str(day), self.name, round(spot, 2),
                self._prev_trend or "NONE", self.state.straddle_count,
                len(self.state.open_legs), len(self.state.closed_legs),
                round(real, 2), round(unreal, 2), round(real + unreal, 2),
                " | ".join(detail) if detail else "flat"]

    def new_trade_rows(self, day):
        """Sheet 3 — appended once per leg, when it closes."""
        rows = []
        for l in self.state.closed_legs:
            k = f"{l.symbol}|{l.entry_time}|{l.exit_time}"
            if k in self._logged:
                continue
            self._logged.add(k)
            pnl = (l.entry_price - l.exit_price) * l.qty if l.exit_price is not None else 0
            rows.append([str(day), self.name, l.straddle_num, l.symbol, l.opt_type,
                         l.strike, l.qty, l.entry_time.strftime("%H:%M:%S"),
                         round(l.entry_price, 2), round(l.sl, 2),
                         l.exit_time.strftime("%H:%M:%S") if l.exit_time else "",
                         round(l.exit_price, 2) if l.exit_price is not None else "",
                         l.exit_reason or "", round(pnl, 2),
                         l.naked, l.sl_tightened, l.carry_bars])
        return rows

    def resume_from(self, day, status_df):
        """Rebuild state from Sheet 1 after a restart. Restores BOTH open and
        closed legs, so realised P&L and the straddle count survive. Restoring
        only open legs would understate P&L and could allow an extra straddle."""
        if status_df.empty or "strategy" not in status_df.columns:
            return 0, 0
        d = status_df[(status_df.strategy == self.name)
                      & (status_df.trading_date.astype(str) == str(day))]
        if d.empty:
            return 0, 0

        def as_bool(v):
            return str(v).strip().upper() in ("TRUE", "1", "YES")

        def num(v, cast=float, default=None):
            try:
                if v == "" or pd.isna(v):
                    return default
                return cast(float(v))
            except Exception:
                return default

        opened = closed = 0
        for _, r in d.iterrows():
            try:
                t = dt.datetime.strptime(str(r.entry_time), "%H:%M:%S").time()
                et = dt.datetime.combine(day, t)
            except Exception:
                et = dt.datetime.combine(day, MARKET_OPEN)
            leg = Leg(symbol=str(r.leg_symbol), opt_type=str(r.opt_type),
                      strike=int(num(r.strike, int, 0)),
                      entry_price=num(r.entry_price, float, 0.0),
                      sl=num(r.sl, float, 0.0),
                      qty=int(num(r.qty, int, LOT_SIZE)),
                      entry_time=et, entry_idx=0,
                      straddle_num=int(num(r.straddle_num, int, 1)),
                      naked=as_bool(r.naked), sl_tightened=as_bool(r.sl_tightened))
            if str(r.status).upper() == "CLOSED":
                leg.exit_price = num(r.exit_price, float, leg.entry_price)
                leg.exit_reason = str(r.exit_reason) or "RESUMED"
                try:
                    xt = dt.datetime.strptime(str(r.exit_time), "%H:%M:%S").time()
                    leg.exit_time = dt.datetime.combine(day, xt)
                except Exception:
                    leg.exit_time = et
                self.state.closed_legs.append(leg)
                self._logged.add(f"{leg.symbol}|{leg.entry_time}|{leg.exit_time}")
                closed += 1
            else:
                self.state.open_legs.append(leg)
                opened += 1

        nums = [l.straddle_num for l in self.state.open_legs + self.state.closed_legs]
        if nums:
            self.state.straddle_count = max(nums)
        return opened, closed

    def run_day(self, day, nifty, vix, fut, finalize=True):
        self.state = DayState(); self.heartbeat = []; self.log = []; self._prev_trend = None
        self._vix = vix
        mask = nifty.ts.dt.date == day
        pos = np.flatnonzero(mask.to_numpy())
        if len(pos) == 0:
            return
        dayc = nifty.loc[mask].reset_index(drop=True)
        for li in range(len(dayc)):
            i = int(pos[li]); now = dayc.ts.iloc[li]
            spot = float(dayc.close.iloc[li])
            fr = fut[fut.ts <= now] if ("ts" in fut.columns and not fut.empty) \
                else empty_candles()
            self.on_bar(day, nifty, i, li, now,
                        spot, float(fr.close.iloc[-1]) if not fr.empty else spot)
        # Only force-close when the session is actually over. On a mid-session
        # run these legs are still live — booking them at entry price would
        # invent zero-P&L exits that never happened.
        if finalize:
            for leg in list(self.state.open_legs):
                self.exit_leg(leg, leg.entry_price, "FORCED_DAY_END",
                              dayc.ts.iloc[-1], int(pos[-1]))


# ============================== RUN ==========================================
def run_dates(dates, run_type, variants, expiry_override=None,
              finalize=True, progress=None):
    """Backtest driver. Returns the same three shapes the live engine writes:
    trade rows, a per-day-per-strategy summary, heartbeat, and open legs."""
    smart = angel_login()
    lookup, expiries = option_universe()
    first, last = min(dates), max(dates)
    frm = dt.datetime.combine(first-dt.timedelta(days=WARMUP_CALENDAR_DAYS), dt.time(9, 15))
    to = min(dt.datetime.combine(last, dt.time(15, 30)), now_ist())

    nifty = candles(smart, NIFTY_INDEX_TOKEN, NSE, frm, to)
    vix = candles(smart, INDIA_VIX_TOKEN, NSE, frm, to)
    fut = candles(smart, NIFTY_FUT_TOKEN, NFO, frm, to)
    if nifty.empty:
        return None, None, None, "No NIFTY candles returned."

    warm = int((nifty.ts.dt.date < first).sum())
    notes = []
    if vix.empty:
        notes.append("India VIX returned no candles — the VIX entry filter cannot "
                     "pass, so no trades will be taken.")
    if fut.empty:
        notes.append("NIFTY futures returned no candles — Monday strike selection "
                     "falls back to spot. Check NIFTY_FUT_TOKEN after each roll.")
    if warm < FULL_WARMUP_BARS:
        notes.append(f"Only {warm} warm-up bars (want {FULL_WARMUP_BARS}); the Loxx "
                     "squeeze filter is skipped early in the day.")

    trades, summary, hb, logs, open_legs = [], [], [], [], []
    cache = {}
    for n, day in enumerate(sorted(dates)):
        expiry = expiry_override or nearest_expiry(expiries, day)
        engines = [StraddleEngine(nm, dyn, rd, ml, smart, lookup, expiry, cache)
                   for nm, dyn, rd, ml in variants]
        for e in engines:
            e.run_day(day, nifty, vix, fut, finalize=finalize)
            trades += e.new_trade_rows(day)
            summary.append([run_type, e.name, str(day), round(e.state.pnl(), 2),
                            len(e.state.closed_legs), e.state.straddle_count,
                            sum(1 for l in e.state.closed_legs if l.naked),
                            sum(1 for l in e.state.closed_legs
                                if l.exit_reason == "REVERSAL_EMA_CROSS"),
                            e.state.redeploys])
            for l in e.state.open_legs:
                open_legs.append([e.name, l.straddle_num, l.symbol, l.opt_type,
                                  l.strike, l.entry_time.strftime("%H:%M:%S"),
                                  round(l.entry_price, 2), round(l.sl, 2),
                                  l.naked, l.sl_tightened])
            hb += e.heartbeat
            logs += e.log
        if progress:
            progress.progress((n+1)/len(dates), text=f"{day} ({n+1}/{len(dates)})")

    O = pd.DataFrame(open_legs, columns=["strategy", "straddle_num", "leg_symbol",
                                         "opt_type", "strike", "entry_time",
                                         "entry_price", "sl", "naked", "sl_tightened"])
    return (pd.DataFrame(trades, columns=TRADE_HEADERS),
            pd.DataFrame(summary, columns=SUMMARY_HEADERS),
            pd.DataFrame(hb, columns=HEARTBEAT_HEADERS), O), logs, notes, None


# ============================== UI ===========================================
# ============================== THREADED LIVE ENGINE ========================
def _seconds_to_next_bar(now):
    """Sleep target: the next 3-minute boundary plus a buffer, so the bar has
    actually closed broker-side before we ask for it."""
    secs = (CANDLE_MIN - (now.minute % CANDLE_MIN)) * 60 - now.second
    if secs <= 0:
        secs = CANDLE_MIN * 60
    return secs + POLL_BUFFER_SEC


def live_engine_loop(stop_event, expiry, status, notify_on=False):
    """Daemon thread. Wakes just after each 3-minute close, feeds unseen bars
    into every engine, then writes the three sheets:

      Sheet 1  Live_Status        rewritten whole — current leg ledger
      Sheet 2  Log_<date>         appended — one row per strategy per bar
      Sheet 3  Trades_<date>      appended — one row per leg on close

    On startup it reads Sheet 1 and resumes that day's state, so a container
    restart continues rather than beginning flat.
    """
    day = today_ist()
    status["state"] = "starting"
    status["log"] = []

    def note(msg):
        status["log"] = (status.get("log", []) + [f"{now_ist():%H:%M:%S}  {msg}"])[-60:]

    try:
        smart = angel_login()
        lookup, _ = option_universe()
        cache = {}
        engines = [StraddleEngine(nm, dyn, rd, ml, smart, lookup, expiry, cache)
                   for nm, dyn, rd, ml in VARIANTS_LIVE]

        try:
            prev = read_tab("LIVE", TAB_STATUS)
        except Exception as e:
            prev = pd.DataFrame()
            note(f"could not read {TAB_STATUS}: {e}")

        # Sheet 1 accumulates. Previous days are held in memory and rewritten
        # ahead of today's rows each cycle, so history is preserved while
        # today's block stays current. Read once here rather than every cycle.
        carry_rows = []
        if not prev.empty and "trading_date" in prev.columns:
            older = prev[prev.trading_date.astype(str) != str(day)]
            carry_rows = rows_from_df(older, STATUS_HEADERS)
            if carry_rows:
                note(f"{TAB_STATUS}: carrying {len(carry_rows)} row(s) "
                     f"from {older.trading_date.nunique()} earlier day(s)")

        resumed_open = resumed_closed = 0
        for e in engines:
            e.prepare(day)
            e.notify = notify_on
            o, c = e.resume_from(day, prev)
            resumed_open += o
            resumed_closed += c

        msg = f"engine up · expiry {expiry} · {len(engines)} variants"
        if resumed_open or resumed_closed:
            msg += f" · resumed {resumed_open} open / {resumed_closed} closed legs"
        note(msg)
        if notify_on:
            discord(f"**Straddle Desk started**\n`{day}`  expiry `{expiry}`\n"
                    f"resumed: {resumed_open} open, {resumed_closed} closed\n"
                    f"variants: {', '.join(e.name for e in engines)}", tag="🚀")
    except Exception as e:
        status["state"] = "failed"
        note(f"startup failed: {type(e).__name__}: {e}")
        if notify_on:
            discord(f"**Startup failed**\n`{type(e).__name__}: {e}`", tag="❌")
        return

    status["state"] = "running"
    last_ts = None

    while not stop_event.is_set():
        now = now_ist()
        if now.time() < MARKET_OPEN:
            time.sleep(20)
            continue
        if now.time() >= MARKET_CLOSE:
            note("market closed — finalising")
            break

        target = now + dt.timedelta(seconds=_seconds_to_next_bar(now))
        while now_ist() < target:
            if stop_event.is_set():
                note("stopped from the dashboard")
                if notify_on:
                    discord("**Engine stopped from the dashboard**", tag="🛑")
                break
            time.sleep(1)
        if stop_event.is_set():
            break                    # fall through to finalisation, not return

        try:
            now = now_ist()
            frm = dt.datetime.combine(day - dt.timedelta(days=WARMUP_CALENDAR_DAYS),
                                      MARKET_OPEN)
            nifty = candles(smart, NIFTY_INDEX_TOKEN, NSE, frm, now)
            if nifty.empty:
                note("no NIFTY candles — retrying next cycle")
                continue
            vix = candles(smart, INDIA_VIX_TOKEN, NSE, frm, now)
            fut = candles(smart, NIFTY_FUT_TOKEN, NFO, frm, now)
            if vix.empty:
                note("VIX empty this cycle — entry filter cannot pass")
            if fut.empty:
                note("futures empty this cycle — using spot for strikes")

            mask = nifty.ts.dt.date == day
            pos = np.flatnonzero(mask.to_numpy())
            if len(pos) < 2:
                note("waiting for the session to produce bars")
                continue

            pos = pos[:-1]                       # drop the forming candle
            new_bars = [p for p in pos if last_ts is None or nifty.ts.iloc[p] > last_ts]

            # Option prices must be fresh each cycle, so the shared candle
            # cache is dropped before evaluating.
            cache.clear()

            spot = float(nifty.close.iloc[int(pos[-1])])
            if new_bars:
                for p in new_bars:
                    i = int(p)
                    bar_ts = nifty.ts.iloc[i]
                    spot = float(nifty.close.iloc[i])
                    fr = fut[fut.ts <= bar_ts] if ("ts" in fut.columns and not fut.empty) \
                        else empty_candles()
                    fv = float(fr.close.iloc[-1]) if not fr.empty else spot
                    for e in engines:
                        e._vix = vix
                        e.on_bar(day, nifty, i, None, bar_ts, spot, fv)
                    last_ts = bar_ts
                note(f"processed to {last_ts:%H:%M}")
            else:
                note("no new completed bar yet")

            stamp = now.strftime("%Y-%m-%d %H:%M:%S")
            mark = last_ts if last_ts is not None else nifty.ts.iloc[int(pos[-1])]

            # Sheet 3 — closed legs, appended once each
            wrote = 0
            for e in engines:
                rows = e.new_trade_rows(day)
                if rows:
                    wrote += append_rows("LIVE", tab_trades(day), TRADE_HEADERS, rows)
            if wrote:
                note(f"{wrote} leg(s) -> {tab_trades(day)}")

            # Sheet 2 — 3-minute log
            append_rows("LIVE", tab_log(day), LOG_HEADERS,
                        [e.log_row(day, mark, spot, stamp) for e in engines])

            # Sheet 1 — running ledger: earlier days kept, today refreshed
            replace_tab("LIVE", TAB_STATUS, STATUS_HEADERS,
                        carry_rows
                        + [r for e in engines for r in e.status_rows(day, mark, stamp)])

            status["summary"] = {
                e.name: dict(realised=round(e.state.pnl(), 2),
                             unrealised=round(e.unrealised(day, mark), 2),
                             total=round(e.state.pnl() + e.unrealised(day, mark), 2),
                             closed=len(e.state.closed_legs),
                             open=len(e.state.open_legs),
                             straddles=e.state.straddle_count)
                for e in engines}
            status["last_bar"] = f"{mark:%H:%M}"
            status["spot"] = round(spot, 2)
        except Exception as e:
            note(f"cycle error: {type(e).__name__}: {e}")
            time.sleep(5)

    # ---- finalisation: runs on market close AND on a manual stop ----
    try:
        now = now_ist()
        stamp = now.strftime("%Y-%m-%d %H:%M:%S")
        mark = last_ts if last_ts is not None else now
        wrote = 0
        for e in engines:
            rows = e.new_trade_rows(day)
            if rows:
                wrote += append_rows("LIVE", tab_trades(day), TRADE_HEADERS, rows)
            if e.heartbeat:
                append_rows("LIVE", f"Heartbeat_{day}", HEARTBEAT_HEADERS, e.heartbeat)
        replace_tab("LIVE", TAB_STATUS, STATUS_HEADERS,
                    carry_rows
                    + [r for e in engines for r in e.status_rows(day, mark, stamp)])
        note(f"finalised · {wrote} late leg(s) written · "
             f"{len(carry_rows)} earlier row(s) preserved")
        status["state"] = "stopped" if stop_event.is_set() else "finished"
        if notify_on:
            lines = [f"**{e.name}**  realised {e.state.pnl():+,.0f}  "
                     f"({len(e.state.closed_legs)} closed, "
                     f"{len(e.state.open_legs)} open)" for e in engines]
            discord(f"**Session ended** `{day}`\n" + "\n".join(lines), tag="🏁")
    except Exception as e:
        note(f"final write failed: {type(e).__name__}: {e}")
        status["state"] = "finished_with_errors"


# ============================== THEME + HELPERS =============================
CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&display=swap');

.stApp { background:
   radial-gradient(1100px 500px at 12% -8%, #ffe9f4 0%, transparent 60%),
   radial-gradient(900px 460px at 92% 4%, #e9edff 0%, transparent 58%),
   #f7f5fb; }
html, body, [class*="css"], .stMarkdown, p, div, span, label
   { font-family:'Plus Jakarta Sans',sans-serif; }
#MainMenu, footer { visibility:hidden; }
header[data-testid="stHeader"] { background:transparent; height:0; }

/* The sidebar is pinned open. Hiding the header also hid Streamlit's expand
   arrow, so a stray click on collapse left no way back — the collapse control
   is removed instead and the panel forced visible. */
[data-testid="stSidebarCollapseButton"],
[data-testid="collapsedControl"],
button[kind="header"] { display:none !important; }

section[data-testid="stSidebar"] {
  visibility:visible !important;
  transform:none !important;
  margin-left:0 !important;
  min-width:290px !important;
  width:290px !important; }
section[data-testid="stSidebar"][aria-expanded="false"] {
  visibility:visible !important; transform:none !important; }

/* On a phone a pinned 290px panel would swallow the screen, so let it behave
   normally there and restore the expand control. */
@media (max-width:640px) {
  section[data-testid="stSidebar"] { min-width:0 !important; width:auto !important; }
  [data-testid="stSidebarCollapseButton"],
  [data-testid="collapsedControl"] { display:block !important; }
}
.block-container { padding:1rem 2rem 3rem; max-width:1480px; }

/* ---------- sidebar ---------- */
section[data-testid="stSidebar"] {
  background:linear-gradient(180deg,#1c1030 0%,#2a1246 55%,#33144f 100%);
  border-right:none; }
section[data-testid="stSidebar"] * { color:#ede7f6; }
section[data-testid="stSidebar"] .block-container { padding-top:1.6rem; }
.brand { font-size:1.12rem; font-weight:800; letter-spacing:-.02em; color:#fff;
  display:flex; align-items:center; gap:.5rem; margin:0 0 .25rem 0; }
.brand-dot { width:9px; height:9px; border-radius:50%;
  background:linear-gradient(135deg,#f472b6,#a78bfa);
  box-shadow:0 0 12px rgba(244,114,182,.85); }
.brand-sub { font-size:.72rem; color:#b9a8d4; margin-bottom:1.5rem;
  letter-spacing:.04em; text-transform:uppercase; font-weight:600; }

/* nav: turn the radio group into nav rows */
section[data-testid="stSidebar"] div[role="radiogroup"] { gap:.3rem; }
section[data-testid="stSidebar"] div[role="radiogroup"] > label {
  background:rgba(255,255,255,.04); border:1px solid rgba(255,255,255,.06);
  border-radius:11px; padding:.6rem .85rem; width:100%; cursor:pointer;
  transition:all .16s ease; font-weight:600; font-size:.9rem; }
section[data-testid="stSidebar"] div[role="radiogroup"] > label:hover {
  background:rgba(255,255,255,.11); transform:translateX(2px); }
section[data-testid="stSidebar"] div[role="radiogroup"] > label > div:first-child
  { display:none; }
section[data-testid="stSidebar"] div[role="radiogroup"] > label[data-checked="true"],
section[data-testid="stSidebar"] div[role="radiogroup"] > label:has(input:checked) {
  background:linear-gradient(135deg,#ec4899,#a855f7);
  border-color:transparent; box-shadow:0 6px 18px rgba(168,85,247,.35); }
.side-sep { height:1px; background:rgba(255,255,255,.09); margin:1.3rem 0 1rem; }
.side-lbl { font-size:.66rem; text-transform:uppercase; letter-spacing:.09em;
  color:#a692c4; font-weight:700; margin-bottom:.5rem; }
.side-kv { display:flex; justify-content:space-between; font-size:.76rem;
  padding:.3rem 0; color:#cfc2e4; }
.side-kv b { color:#fff; font-weight:600; }

/* ---------- header ---------- */
.hdr { display:flex; align-items:flex-end; justify-content:space-between;
  margin:.2rem 0 1.4rem; flex-wrap:wrap; gap:.8rem; }
.hdr h1 { font-size:1.85rem; font-weight:800; letter-spacing:-.03em; margin:0;
  background:linear-gradient(120deg,#1e1035 20%,#7c3aed 60%,#ec4899 95%);
  -webkit-background-clip:text; -webkit-text-fill-color:transparent; }
.hdr .tag { font-size:.85rem; color:#7c6f8c; margin-top:.15rem; font-weight:500; }
.chip { display:inline-flex; align-items:center; gap:.4rem; padding:.42rem .85rem;
  border-radius:99px; font-size:.76rem; font-weight:700; background:#fff;
  border:1px solid #ece3f5; box-shadow:0 2px 8px rgba(80,20,120,.06); }
.chip .dot { width:7px; height:7px; border-radius:50%; }
.dot-live { background:#22c55e; box-shadow:0 0 9px #22c55e; }
.dot-off  { background:#cbd5e1; }

/* ---------- panels ---------- */
.panel { background:rgba(255,255,255,.82); backdrop-filter:blur(6px);
  border-radius:20px; padding:1.25rem 1.4rem; border:1px solid rgba(255,255,255,.9);
  box-shadow:0 8px 26px rgba(80,20,120,.07); margin-bottom:1.1rem; }
.panel h4 { margin:0; font-size:1.02rem; font-weight:700; color:#1e1035;
  letter-spacing:-.01em; }
.panel .sub { font-size:.79rem; color:#8b7d9c; margin-top:.2rem; font-weight:500; }

/* ---------- kpi ---------- */
.kpi { border-radius:20px; padding:1.2rem 1.3rem; color:#fff; position:relative;
  overflow:hidden; box-shadow:0 10px 28px rgba(90,25,120,.22);
  transition:transform .18s ease; }
.kpi:hover { transform:translateY(-3px); }
.kpi:after { content:""; position:absolute; right:-28px; top:-28px; width:110px;
  height:110px; border-radius:50%; background:rgba(255,255,255,.13); }
.kpi .lbl { font-size:.7rem; text-transform:uppercase; letter-spacing:.09em;
  font-weight:700; opacity:.92; }
.kpi .val { font-size:1.95rem; font-weight:800; letter-spacing:-.03em;
  line-height:1.15; margin-top:.35rem; }
.kpi .fin { font-size:.75rem; opacity:.88; margin-top:.25rem; font-weight:500; }
.g1 { background:linear-gradient(135deg,#f43f5e,#ec4899 55%,#d946ef); }
.g2 { background:linear-gradient(135deg,#8b5cf6,#6366f1 60%,#4f46e5); }
.g3 { background:linear-gradient(135deg,#06b6d4,#0ea5e9 60%,#3b82f6); }
.g4 { background:linear-gradient(135deg,#f59e0b,#f97316 60%,#ef4444); }
.gneg { background:linear-gradient(135deg,#475569,#64748b); }
.gmute { background:linear-gradient(135deg,#e6e0ee,#efeaf6); color:#7c6f8c;
  box-shadow:none; border:1px dashed #ddd2ea; }
.gmute .val { color:#a99bbd; }

/* ---------- stat tile ---------- */
.stat { background:#fff; border-radius:16px; padding:.95rem 1.1rem;
  border:1px solid #f0e9f7; box-shadow:0 2px 10px rgba(80,20,120,.045); }
.stat .lbl { font-size:.68rem; color:#9b8aa6; text-transform:uppercase;
  letter-spacing:.08em; font-weight:700; }
.stat .val { font-size:1.32rem; font-weight:800; color:#1e1035;
  letter-spacing:-.02em; margin-top:.15rem; }

/* ---------- empty state ---------- */
.empty { text-align:center; padding:2.6rem 1rem; color:#9b8aa6; }
.empty .ico { font-size:2rem; opacity:.5; }
.empty .t { font-weight:700; color:#5c4d70; margin-top:.5rem; font-size:.98rem; }
.empty .s { font-size:.82rem; margin-top:.25rem; }

/* ---------- widgets ---------- */
.stButton>button { border-radius:13px; font-weight:700; border:none; width:100%;
  background:linear-gradient(135deg,#ec4899,#a855f7); color:#fff;
  padding:.62rem 1.2rem; letter-spacing:.01em;
  box-shadow:0 6px 18px rgba(168,85,247,.32); transition:all .16s ease; }
.stButton>button:hover { transform:translateY(-2px); color:#fff;
  box-shadow:0 10px 24px rgba(168,85,247,.42); }
div[data-testid="stDataFrame"] { border-radius:14px; overflow:hidden;
  border:1px solid #efe6f6; box-shadow:0 3px 14px rgba(80,20,120,.05); }
div[data-baseweb="select"]>div, div[data-testid="stDateInput"] input {
  border-radius:11px !important; border-color:#eadff5 !important; }
.stProgress > div > div > div { background:linear-gradient(90deg,#ec4899,#a855f7); }
</style>
"""

GRADS = {"FIXED_1_3X": "g1", "DYNAMIC_SL": "g2"}


def kpi(label, value, footnote="", grad="g1"):
    return (f'<div class="kpi {grad}"><div class="lbl">{label}</div>'
            f'<div class="val">{value}</div><div class="fin">{footnote}</div></div>')


def stat(label, value):
    return f'<div class="stat"><div class="lbl">{label}</div><div class="val">{value}</div></div>'


def panel(title, sub=""):
    return f'<div class="panel"><h4>{title}</h4><div class="sub">{sub}</div></div>' 


def empty_state(title, sub, ico="◇"):
    return (f'<div class="panel"><div class="empty"><div class="ico">{ico}</div>'
            f'<div class="t">{title}</div><div class="s">{sub}</div></div></div>')


def header(title, tag="", live=False):
    """tag is accepted and ignored — the header subtitle was removed on
    request. Kept in the signature so existing call sites still work."""
    dot = "dot-live" if live else "dot-off"
    when = f"{now_ist():%H:%M}"
    txt = f"Session open · {when}" if live else f"Session closed · {when}"
    return (f'<div class="hdr"><div><h1>{title}</h1></div>'
            f'<div class="chip"><span class="dot {dot}"></span>{txt}</div></div>')


def money(x):
    try:
        return f"{float(x):+,.0f}"
    except Exception:
        return "--"


@st.cache_data(ttl=3600, show_spinner=False)
def expiries_for(day):
    _, exp = option_universe()
    return expiry_choices(exp, day)


def equity_chart(S):
    """Cumulative P&L per variant."""
    if S.empty:
        return None
    d = S.copy()
    d["total_pnl"] = pd.to_numeric(d.total_pnl, errors="coerce").fillna(0)
    d["trading_date"] = pd.to_datetime(d.trading_date)
    d = d.sort_values("trading_date")
    d["cum"] = d.groupby("strategy").total_pnl.cumsum()
    return (alt.Chart(d).mark_line(strokeWidth=2.5, point=alt.OverlayMarkDef(size=35))
            .encode(x=alt.X("trading_date:T", title=None,
                            axis=alt.Axis(grid=False, labelColor="#9b8aa6")),
                    y=alt.Y("cum:Q", title="cumulative P&L",
                            axis=alt.Axis(gridColor="#f4e9f0", labelColor="#9b8aa6")),
                    color=alt.Color("strategy:N", title=None,
                                    scale=alt.Scale(domain=list(GRADS),
                                                    range=["#ec4899", "#8b5cf6"]),
                                    legend=alt.Legend(orient="top")),
                    tooltip=["trading_date:T", "strategy:N", "cum:Q", "total_pnl:Q"])
            .properties(height=260).configure_view(strokeWidth=0))


def reason_chart(T):
    """Exit reasons by count."""
    if T.empty or "exit_reason" not in T.columns:
        return None
    g = T.groupby("exit_reason").size().reset_index(name="n")
    return (alt.Chart(g).mark_arc(innerRadius=52, stroke="#fff", strokeWidth=2)
            .encode(theta="n:Q",
                    color=alt.Color("exit_reason:N", title=None,
                                    scale=alt.Scale(scheme="purpleorange"),
                                    legend=alt.Legend(orient="right")),
                    tooltip=["exit_reason:N", "n:Q"])
            .properties(height=240).configure_view(strokeWidth=0))


st.markdown(CSS, unsafe_allow_html=True)

missing = [k for k in ("angel", "sheets", "gcp_service_account") if k not in st.secrets]
if missing:
    st.error(f"Missing secrets: {', '.join(missing)}. Add them in Settings → Secrets.")
    st.stop()
for _k, _v in SHEET_KEYS.items():
    if _v not in st.secrets["sheets"]:
        st.error(f"Missing [sheets] {_v} in secrets (needed for {_k}).")
        st.stop()

TODAY = today_ist()
NOW = now_ist()
SESSION_LIVE = (TODAY.weekday() < 5 and dt.time(9, 15) <= NOW.time() < dt.time(15, 30))
SESSION_OVER = NOW.time() >= dt.time(15, 30)

# ---------------- sidebar ----------------
with st.sidebar:
    st.markdown('<div class="brand"><span class="brand-dot"></span>Straddle Desk</div>'
                '<div class="brand-sub">NIFTY · short premium</div>',
                unsafe_allow_html=True)
    page = st.radio("nav", ["Live run", "Backtest", "History"],
                    label_visibility="collapsed")
    st.markdown('<div class="side-sep"></div><div class="side-lbl">Configuration</div>',
                unsafe_allow_html=True)
    st.markdown(
        f'<div class="side-kv"><span>Front-month</span><b>{NIFTY_FUT_TOKEN}</b></div>'
        f'<div class="side-kv"><span>Lot size</span><b>{LOT_SIZE}</b></div>'
        f'<div class="side-kv"><span>Base SL</span><b>{SL_MULTIPLIER}x</b></div>'
        f'<div class="side-kv"><span>Tight SL</span><b>{TIGHT_SL_MULTIPLIER}x</b></div>'
        f'<div class="side-kv"><span>Tighten</span><b>{TIGHTEN_MODE.split("_")[0].title()}</b></div>'
        f'<div class="side-kv"><span>Max/day</span><b>{MAX_STRADDLES_PER_DAY}</b></div>'
        f'<div class="side-kv"><span>Entry window</span>'
        f'<b>{ENTRY_START:%H:%M}-{ENTRY_END:%H:%M}</b></div>'
        f'<div class="side-kv"><span>Hard exit</span><b>{HARD_EXIT:%H:%M}</b></div>',
        unsafe_allow_html=True)
    st.markdown('<div class="side-sep"></div><div class="side-lbl">Alerts</div>',
                unsafe_allow_html=True)
    _hook = discord_url()
    if _hook:
        notify_on = st.checkbox("Discord notifications", value=True,
                                help="Entry, exit, SL tightened, leg naked, "
                                     "plus engine start and session summary.")
        if st.button("Send test"):
            ok = discord(f"Test from Straddle Desk · {now_ist():%d %b %H:%M} IST",
                         tag="🔔")
            st.success("Sent — check the channel.") if ok else \
                st.error("Failed. Check the webhook URL in secrets.")
    else:
        notify_on = False
        st.caption("No webhook configured. Add `[discord] webhook = \"...\"` "
                   "to secrets.")
    st.markdown('<div class="side-sep"></div>', unsafe_allow_html=True)
    st.caption("Paper only. No orders are ever placed.")


def render_kpis(S, variants, note=""):
    cols = st.columns(len(variants) + 1)
    for c, v in zip(cols, variants):
        g = S[S.strategy == v[0]] if not S.empty else S
        has = not g.empty
        pnl = pd.to_numeric(g.total_pnl, errors="coerce").sum() if has else 0
        legs = int(pd.to_numeric(g.num_legs, errors="coerce").sum()) if has else 0
        grad = ("gmute" if not has else
                (GRADS.get(v[0], "g1") if pnl >= 0 else "gneg"))
        with c:
            st.markdown(kpi(v[0].replace("_", " "), money(pnl) if has else "--",
                            f"{legs} legs" if has else "no data", grad),
                        unsafe_allow_html=True)
    with cols[-1]:
        n = S.trading_date.nunique() if not S.empty else 0
        st.markdown(kpi("Sessions", str(n), note or "recorded",
                        "g4" if n else "gmute"), unsafe_allow_html=True)


# ================================ LIVE ======================================
if page == "Live run":
    st.markdown(header("Live run", "FIXED_1_3X · DYNAMIC_SL — paper, no orders placed",
                       SESSION_LIVE), unsafe_allow_html=True)

    if "engine_thread" not in st.session_state:
        st.session_state.engine_thread = None
        st.session_state.stop_event = threading.Event()
        st.session_state.engine_status = {}

    running = (st.session_state.engine_thread is not None
               and st.session_state.engine_thread.is_alive())
    status = st.session_state.engine_status

    try:
        exps = expiries_for(TODAY)
    except Exception as e:
        st.error(f"Could not load expiries: {e}"); st.stop()

    c1, c2, c3, c4 = st.columns([1.05, 1.05, 1.1, 1])
    with c1:
        st.markdown(stat("Trading day", TODAY.strftime("%d %b %Y")), unsafe_allow_html=True)
    with c2:
        st.markdown(stat("Last bar", status.get("last_bar", "--")), unsafe_allow_html=True)
    with c3:
        exp_live = st.selectbox("Expiry", exps,
                                format_func=lambda d: f"{d:%d %b} · {(d-TODAY).days}d",
                                key="exp_live", label_visibility="collapsed",
                                disabled=running)
        st.caption("Expiry")
    with c4:
        st.write("")
        if not running:
            start = st.button("Start engine", type="primary")
            if start:
                st.session_state.stop_event = threading.Event()
                st.session_state.engine_status = {"state": "starting", "log": []}
                th = threading.Thread(target=live_engine_loop,
                                      args=(st.session_state.stop_event, exp_live,
                                            st.session_state.engine_status, notify_on),
                                      daemon=True)
                th.start()
                st.session_state.engine_thread = th
                time.sleep(1)
                st.rerun()
        else:
            if st.button("Stop engine"):
                st.session_state.stop_event.set()
                st.session_state.engine_thread.join(timeout=5)
                st.session_state.engine_thread = None
                st.rerun()

    state = status.get("state", "idle")
    if running:
        st.success(f"Engine {state} · polls each 3-min bar close and writes to Sheets. "
                   "It keeps running after you close this tab, until the market closes "
                   "or the container is recycled.")
    elif state in ("finished", "finished_with_errors"):
        st.info(f"Engine {state.replace('_', ' ')}. Daily summary written.")
    elif state == "failed":
        st.error("Engine failed to start — see the log below.")

    summ = status.get("summary")
    if summ:
        S_live = pd.DataFrame([{"strategy": k, "total_pnl": v["pnl"],
                                "num_legs": v["closed"], "trading_date": str(TODAY)}
                               for k, v in summ.items()])
        render_kpis(S_live, VARIANTS_LIVE, TODAY.strftime("%d %b"))
        det = pd.DataFrame([{"strategy": k, **v} for k, v in summ.items()])
        st.markdown(panel("Current state", f"as of {status.get('last_bar','--')}"),
                    unsafe_allow_html=True)
        st.dataframe(det, use_container_width=True, hide_index=True)
    else:
        try:
            ST = read_tab("LIVE", TAB_STATUS)
        except Exception:
            ST = pd.DataFrame()
        if ST.empty:
            render_kpis(pd.DataFrame(columns=["strategy", "total_pnl", "num_legs",
                                              "trading_date"]), VARIANTS_LIVE)
            st.markdown(empty_state("Nothing recorded yet",
                        "Start the engine — it writes the status sheet each bar.", "◇"),
                        unsafe_allow_html=True)
        else:
            ST["leg_pnl"] = pd.to_numeric(ST.leg_pnl, errors="coerce").fillna(0)
            agg = (ST.groupby("strategy")
                     .agg(total_pnl=("leg_pnl", "sum"), num_legs=("leg_pnl", "size"))
                     .reset_index())
            agg["trading_date"] = ST.trading_date.iloc[0] if len(ST) else str(TODAY)
            render_kpis(agg, VARIANTS_LIVE,
                        f"{ST.trading_date.iloc[0]}" if len(ST) else "")
            st.markdown(panel("Leg ledger",
                        f"Sheet 1 · updated {ST.updated_at.iloc[-1]}"),
                        unsafe_allow_html=True)
            st.dataframe(ST, use_container_width=True, hide_index=True, height=320)

    lg = status.get("log")
    if lg:
        st.markdown(panel("Engine log", "newest last"), unsafe_allow_html=True)
        st.code("\n".join(lg[-25:]))
    if running:
        if st.button("Refresh view"):
            st.rerun()
        st.caption("The engine runs independently — refreshing only updates this page.")

# ================================ BACKTEST =================================
elif page == "Backtest":
    st.markdown(header("Backtest", "", SESSION_LIVE), unsafe_allow_html=True)
    c1, c2, c3 = st.columns(3)
    mode = c1.radio("Range", ["Single day", "Date range"], key="bt_mode",
                    horizontal=True)
    if mode == "Single day":
        bt_day = c2.date_input("Trading day", value=TODAY, key="bt_day")
        days = [bt_day] if bt_day.weekday() < 5 else []
        ref = bt_day
    else:
        d1 = c2.date_input("From", value=TODAY-dt.timedelta(days=7), key="bt_from")
        d2 = c3.date_input("To", value=TODAY, key="bt_to")
        days = [d for d in (d1+dt.timedelta(days=k) for k in range((d2-d1).days+1))
                if d.weekday() < 5]
        ref = d1
    try:
        bexps = expiries_for(ref)
    except Exception as e:
        st.error(f"Could not load expiries: {e}"); st.stop()

    c4, c5, c6 = st.columns([1.3, 1.3, 1])
    auto = c4.checkbox("Nearest expiry per day", value=True,
                       help="Uncheck to force one expiry across the whole range.")
    exp_bt = None if auto else c5.selectbox("Expiry", bexps,
                                            format_func=lambda d: f"{d:%d %b %Y}",
                                            key="exp_bt")
    save = c6.checkbox("Save to sheet", value=False)
    st.caption(f"{len(days)} weekday(s) selected")
    run_bt = st.button("Run backtest", type="primary")

    if run_bt:
        if not days:
            st.warning("No weekdays selected.")
        else:
            p = st.progress(0.0, text="fetching candles")
            try:
                out, logs, notes, err = run_dates(days, "BACKTEST", VARIANTS_BACKTEST,
                                                  expiry_override=exp_bt,
                                                  finalize=True, progress=p)
                p.empty()
                if err:
                    st.error(err)
                else:
                    T, S, H, O = out
                    for n in notes:
                        st.warning(n)
                    if S.empty:
                        st.markdown(empty_state("No trades",
                                    "Entry conditions were not met in that range.", "○"),
                                    unsafe_allow_html=True)
                    else:
                        render_kpis(S, VARIANTS_BACKTEST, f"{len(days)} weekdays")
                        a, b = st.columns([1.9, 1])
                        with a:
                            ch = equity_chart(S)
                            if ch is not None:
                                st.markdown(panel("Cumulative P&L"), unsafe_allow_html=True)
                                st.altair_chart(ch, use_container_width=True)
                        with b:
                            rc = reason_chart(T)
                            if rc is not None:
                                st.markdown(panel("Exit reasons"), unsafe_allow_html=True)
                                st.altair_chart(rc, use_container_width=True)
                        st.markdown(panel("Per-session summary"), unsafe_allow_html=True)
                        st.dataframe(S.drop(columns=["run_type"], errors="ignore"),
                                     use_container_width=True, hide_index=True)
                        e1, e2 = st.columns(2)
                        e1.download_button("Download trades", T.to_csv(index=False),
                                           "straddle_trades.csv", "text/csv")
                        if not H.empty:
                            fired = (H.trend.astype(str) != "").sum()
                            st.caption(f"Heartbeat: {len(H)} bars evaluated, "
                                       f"trend fired on {fired}.")
                            e2.download_button("Download heartbeat", H.to_csv(index=False),
                                               "straddle_heartbeat.csv", "text/csv")
                        if save:
                            for _d, _g in T.groupby("trading_date"):
                                append_rows("BACKTEST", tab_trades(_d), TRADE_HEADERS,
                                            _g.values.tolist())
                            append_rows("BACKTEST", "Backtest_Summary",
                                        SUMMARY_HEADERS, S.values.tolist())
                            if not H.empty:
                                for _d, _g in H.groupby("trading_date"):
                                    append_rows("BACKTEST", f"Heartbeat_{_d}",
                                                HEARTBEAT_HEADERS, _g.values.tolist())
                            st.success("Written to the backtest sheet.")
                    with st.expander("Engine log"):
                        st.code("\n".join(logs) or "(nothing)")
            except Exception as e:
                p.empty(); st.error(f"{type(e).__name__}: {e}")
    else:
        st.markdown(empty_state("Ready", "Pick a range and press Run backtest.", "◈"),
                    unsafe_allow_html=True)

# ================================ HISTORY ==================================
else:
    st.markdown(header("History", "accumulated record", SESSION_LIVE),
                unsafe_allow_html=True)
    c1, c2 = st.columns([1.4, 1])
    src = c1.radio("Source", ["Live", "Backtest"], horizontal=True)
    kind = "LIVE" if src == "Live" else "BACKTEST"
    with c2:
        st.write("")
        load = st.button("Load")
    if load:
        tabs = list_tabs(kind, "Trades_")
        if not tabs:
            st.markdown(empty_state("Nothing recorded",
                        f"No Trades_<date> tabs in the {src.lower()} sheet.", "◇"),
                        unsafe_allow_html=True)
        else:
            frames = []
            for t in tabs:
                d = read_tab(kind, t)
                if not d.empty:
                    frames.append(d)
            if not frames:
                st.markdown(empty_state("Nothing recorded", "Tabs exist but are empty.",
                            "◇"), unsafe_allow_html=True)
            else:
                A = pd.concat(frames, ignore_index=True)
                A["pnl"] = pd.to_numeric(A.pnl, errors="coerce").fillna(0)
                S = (A.groupby(["strategy", "trading_date"])
                       .agg(total_pnl=("pnl", "sum"), num_legs=("pnl", "size"))
                       .reset_index())
                render_kpis(S, VARIANTS_LIVE,
                            f"{S.trading_date.nunique()} sessions")
                ch = equity_chart(S)
                if ch is not None:
                    st.markdown(panel("Cumulative P&L",
                                f"{len(tabs)} day tab(s)"), unsafe_allow_html=True)
                    st.altair_chart(ch, use_container_width=True)
                st.markdown(panel("Per-leg trades"), unsafe_allow_html=True)
                st.dataframe(A.tail(200), use_container_width=True, hide_index=True)
                st.download_button("Download all trades", A.to_csv(index=False),
                                   f"{kind.lower()}_trades.csv", "text/csv")
    else:
        st.markdown(empty_state("Ready", "Choose a source and press Load.", "◈"),
                    unsafe_allow_html=True)
