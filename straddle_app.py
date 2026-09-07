"""
NIFTY SHORT STRADDLE — STREAMLIT APP
=====================================
Three variants, one shared data feed, results persisted to Google Sheets.

  FIXED_1_3X   both legs SL at 1.3x entry. On SL, partner goes naked and a
               replacement straddle deploys immediately.
  DYNAMIC_SL   same, but when a trend confirms, the hurt leg tightens to
               1.25x. Only straddle #1 is eligible (TIGHTEN_MODE).
  NAKED_CARRY  no immediate replacement. The naked leg is carried until it
               closes by its own SL or a 21-EMA reversal; the replacement
               deploys then.

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
import time
from dataclasses import dataclass, field

import altair as alt
import numpy as np
import pandas as pd
import streamlit as st

st.set_page_config(page_title="Straddle Desk", page_icon="◆", layout="wide",
                   initial_sidebar_state="expanded")

# ============================== CONSTANTS ===================================
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
REDEPLOY_AFTER_NAKED = "AFTER_NAKED_EXIT"

# name,          dynamic_sl, redeploy_mode,        max_open_legs
FIXED   = ("FIXED_1_3X",  False, REDEPLOY_IMMEDIATE,   3)
DYNAMIC = ("DYNAMIC_SL",  True,  REDEPLOY_IMMEDIATE,   3)
NAKED   = ("NAKED_CARRY", False, REDEPLOY_AFTER_NAKED, 2)

VARIANTS_LIVE = [FIXED, DYNAMIC]                 # live run
VARIANTS_BACKTEST = [FIXED, DYNAMIC, NAKED]      # backtest

TAB_TRADES = "Straddle_Trades"
TAB_SUMMARY = "Straddle_Summary"
TAB_HEARTBEAT = "Straddle_Heartbeat"

TRADE_HEADERS = ["run_type", "trading_date", "strategy", "straddle_num", "leg_symbol",
                 "opt_type", "strike", "entry_time", "exit_time", "entry_price",
                 "exit_price", "sl", "qty", "exit_reason", "pnl", "naked",
                 "sl_tightened", "carry_bars"]
SUMMARY_HEADERS = ["run_type", "strategy", "trading_date", "total_pnl", "num_legs",
                   "num_straddles", "naked_legs", "reversal_exits", "redeploys"]
HEARTBEAT_HEADERS = ["trading_date", "bar_time", "spot", "adx", "adxr", "chop",
                     "loxx_width", "loxx_width_avg", "width_ok", "momentum",
                     "loxx_up", "loxx_dn", "kc_break_up", "kc_break_dn", "trend"]

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


def read_tab(kind, tab):
    import gspread
    try:
        ws = sheet(kind).worksheet(tab)
    except gspread.WorksheetNotFound:
        return pd.DataFrame()
    v = ws.get_all_records()
    return pd.DataFrame(v) if v else pd.DataFrame()


# ============================== MARKET DATA =================================
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
            df = pd.DataFrame(r["data"], columns=["ts", "open", "high", "low", "close", "volume"])
            df["ts"] = pd.to_datetime(df["ts"]).dt.tz_localize(None)
            return df
        time.sleep(API_SLEEP * 4 ** a)
    return pd.DataFrame()


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
        if self.state.straddle_count >= MAX_STRADDLES_PER_DAY:
            return False
        if len(self.state.open_legs)+2 > self.max_open_legs:
            return False
        strike = int(round((fut if day.weekday() == 0 else spot)/STRIKE_STEP)*STRIKE_STEP)
        legs = []
        for ot in ("CE", "PE"):
            sym, df = self.option_df(day, strike, ot)
            if df.empty:
                self.log.append(f"{self.name}: no data for {sym}")
                return False
            row = df[df.ts <= now]
            if row.empty:
                return False
            legs.append((sym, ot, float(row.close.iloc[-1])))
        self.state.straddle_count += 1
        n = self.state.straddle_count
        for sym, ot, px in legs:
            self.state.open_legs.append(Leg(sym, ot, strike, px, px*SL_MULTIPLIER,
                                            LOT_SIZE, now, idx, straddle_num=n))
        self.log.append(f"{self.name}: {tag} straddle #{n} @ {now:%H:%M} strike {strike} "
                        f"CE {legs[0][2]:.2f} PE {legs[1][2]:.2f}")
        return True

    def sl_fill(self, leg, day, now):
        _, df = self.option_df(day, leg.strike, leg.opt_type)
        if df.empty:
            return None
        row = df[df.ts == now]
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
        self.log.append(f"{self.name}: exit {leg.symbol} @ {px:.2f} ({reason}) "
                        f"pnl {(leg.entry_price-px)*leg.qty:+.0f}")

    def partner(self, leg):
        c = [l for l in self.state.open_legs
             if l.opt_type != leg.opt_type and not l.naked
             and l.straddle_num == leg.straddle_num]
        return c[0] if c else None

    def redeploy_after_naked(self, day, spot, fut, now, idx):
        if self.redeploy_mode != REDEPLOY_AFTER_NAKED:
            return
        if self.state.open_legs or self.state.straddle_count >= MAX_STRADDLES_PER_DAY:
            return
        if now.time() > ENTRY_END:
            self.log.append(f"{self.name}: naked closed {now:%H:%M}, past window")
            return
        if self.deploy(day, spot, fut, now, idx, tag="REDEPLOY"):
            self.state.redeploys += 1

    # ---- one bar ----
    def on_bar(self, day, nifty, i, local_i, now, spot, fut):
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

        for leg in list(self.state.open_legs):
            fill = self.sl_fill(leg, day, now)
            if fill is not None and not leg.naked:
                self.exit_leg(leg, fill, "SL_HIT", now, i)
                p = self.partner(leg)
                if p:
                    p.naked = True; p.naked_since = i
                    self.log.append(f"{self.name}: {p.symbol} now naked")
                if self.redeploy_mode == REDEPLOY_IMMEDIATE \
                        and self.state.straddle_count < MAX_STRADDLES_PER_DAY:
                    self.deploy(day, spot, fut, now, i)
                continue
            if leg.naked:
                if fill is not None:
                    self.exit_leg(leg, fill, "NAKED_SL_HIT", now, i)
                    self.redeploy_after_naked(day, spot, fut, now, i)
                    continue
                if self.naked_reversal(leg, day, now):
                    _, df = self.option_df(day, leg.strike, leg.opt_type)
                    r = df[df.ts == now]
                    self.exit_leg(leg, float(r.close.iloc[0]) if not r.empty else leg.entry_price,
                                  "REVERSAL_EMA_CROSS", now, i)
                    self.redeploy_after_naked(day, spot, fut, now, i)
                    continue

        if not self.state.open_legs and self.state.straddle_count < MAX_STRADDLES_PER_DAY:
            if self.entry_ok(nifty.iloc[: i+1], self._vix, i, now):
                self.deploy(day, spot, fut, now, i)

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
            fr = fut[fut.ts <= now]
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
    smart = angel_login()
    lookup, expiries = option_universe()
    first, last = min(dates), max(dates)
    frm = dt.datetime.combine(first-dt.timedelta(days=WARMUP_CALENDAR_DAYS), dt.time(9, 15))
    # Cap at "now" so a mid-session run asks only for candles that exist.
    # Bars from 09:15 up to the current 3-min boundary are all included.
    to = min(dt.datetime.combine(last, dt.time(15, 30)), dt.datetime.now())

    nifty = candles(smart, NIFTY_INDEX_TOKEN, NSE, frm, to)
    vix = candles(smart, INDIA_VIX_TOKEN, NSE, frm, to)
    fut = candles(smart, NIFTY_FUT_TOKEN, NFO, frm, to)
    if nifty.empty:
        return None, None, None, "No NIFTY candles returned."

    warm = int((nifty.ts.dt.date < first).sum())
    notes = []
    if warm < FULL_WARMUP_BARS:
        notes.append(f"Only {warm} warm-up bars (want {FULL_WARMUP_BARS}); the Loxx "
                     "squeeze filter will be skipped early in the day.")

    trades, summary, hb, logs, open_legs = [], [], [], [], []
    cache = {}
    for n, day in enumerate(sorted(dates)):
        expiry = expiry_override or nearest_expiry(expiries, day)
        engines = [StraddleEngine(nm, dyn, rd, ml, smart, lookup, expiry, cache)
                   for nm, dyn, rd, ml in variants]
        for e in engines:
            e.run_day(day, nifty, vix, fut, finalize=finalize)
            for leg in e.state.closed_legs:
                pnl = (leg.entry_price-leg.exit_price)*leg.qty if leg.exit_price is not None else 0
                trades.append([run_type, str(day), e.name, leg.straddle_num, leg.symbol,
                               leg.opt_type, leg.strike, leg.entry_time.strftime("%H:%M:%S"),
                               leg.exit_time.strftime("%H:%M:%S") if leg.exit_time else "",
                               round(leg.entry_price, 2),
                               round(leg.exit_price, 2) if leg.exit_price is not None else "",
                               round(leg.sl, 2), leg.qty, leg.exit_reason, round(pnl, 2),
                               leg.naked, leg.sl_tightened, leg.carry_bars])
            summary.append([run_type, e.name, str(day), round(e.state.pnl(), 2),
                            len(e.state.closed_legs), e.state.straddle_count,
                            sum(1 for l in e.state.closed_legs if l.naked),
                            sum(1 for l in e.state.closed_legs
                                if l.exit_reason == "REVERSAL_EMA_CROSS"),
                            e.state.redeploys])
            for leg in e.state.open_legs:
                open_legs.append([e.name, leg.straddle_num, leg.symbol, leg.opt_type,
                                  leg.strike, leg.entry_time.strftime("%H:%M:%S"),
                                  round(leg.entry_price, 2), round(leg.sl, 2),
                                  leg.naked, leg.sl_tightened])
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
# ============================== THEME + HELPERS =============================
CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');

.stApp { background:#fdf4f8; }
html, body, [class*="css"] { font-family:'Inter',sans-serif; }
#MainMenu, footer, header { visibility:hidden; }
.block-container { padding-top:1.2rem; padding-bottom:2rem; max-width:1500px; }

section[data-testid="stSidebar"] { background:#ffffff; border-right:1px solid #f2e3ec; }
section[data-testid="stSidebar"] .block-container { padding-top:1.5rem; }

.brand { display:flex; align-items:center; gap:.55rem; padding:.7rem .9rem;
  background:linear-gradient(135deg,#ec4899,#d946ef); border-radius:12px;
  color:#fff; font-weight:700; font-size:1.05rem; margin-bottom:1.4rem; }

.panel { background:#fff; border-radius:16px; padding:1.15rem 1.3rem;
  box-shadow:0 1px 3px rgba(80,20,60,.06); border:1px solid #f6e8f0;
  margin-bottom:1rem; }
.panel h4 { margin:0 0 .15rem 0; font-size:1rem; font-weight:600; color:#1f1235; }
.panel .sub { font-size:.78rem; color:#9b8aa6; }

.kpi { border-radius:16px; padding:1.05rem 1.15rem; color:#fff;
  box-shadow:0 4px 14px rgba(120,40,90,.16); }
.kpi .lbl { font-size:.74rem; text-transform:uppercase; letter-spacing:.06em;
  opacity:.9; font-weight:600; }
.kpi .val { font-size:1.75rem; font-weight:700; line-height:1.25; margin-top:.2rem; }
.kpi .fin { font-size:.76rem; opacity:.9; margin-top:.1rem; }
.g1 { background:linear-gradient(135deg,#ec4899,#f43f5e); }
.g2 { background:linear-gradient(135deg,#8b5cf6,#6366f1); }
.g3 { background:linear-gradient(135deg,#0ea5e9,#06b6d4); }
.g4 { background:linear-gradient(135deg,#f59e0b,#f97316); }
.gneg { background:linear-gradient(135deg,#64748b,#475569); }

.stat { background:#fff; border-radius:14px; padding:.85rem 1rem;
  border:1px solid #f6e8f0; }
.stat .lbl { font-size:.72rem; color:#9b8aa6; text-transform:uppercase;
  letter-spacing:.05em; font-weight:600; }
.stat .val { font-size:1.3rem; font-weight:700; color:#1f1235; }

.pill { display:inline-block; padding:.2rem .6rem; border-radius:20px;
  font-size:.7rem; font-weight:600; }
.pill-live { background:#dcfce7; color:#15803d; }
.pill-off  { background:#f1f5f9; color:#64748b; }

.stButton>button { border-radius:10px; font-weight:600; border:none;
  background:linear-gradient(135deg,#ec4899,#d946ef); color:#fff; padding:.5rem 1.2rem; }
.stButton>button:hover { filter:brightness(1.07); color:#fff; }
div[data-testid="stDataFrame"] { border-radius:12px; overflow:hidden;
  border:1px solid #f0e3ec; }
</style>
"""

GRADS = {"FIXED_1_3X": "g1", "DYNAMIC_SL": "g2", "NAKED_CARRY": "g3"}


def kpi(label, value, footnote="", grad="g1"):
    return (f'<div class="kpi {grad}"><div class="lbl">{label}</div>'
            f'<div class="val">{value}</div><div class="fin">{footnote}</div></div>')


def stat(label, value):
    return f'<div class="stat"><div class="lbl">{label}</div><div class="val">{value}</div></div>'


def panel_open(title, sub=""):
    return f'<div class="panel"><h4>{title}</h4><div class="sub">{sub}</div>'


def money(x):
    try:
        return f"{float(x):+,.0f}"
    except Exception:
        return "-"


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
                                                    range=["#ec4899", "#8b5cf6", "#0ea5e9"]),
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

# ---------------- sidebar ----------------
with st.sidebar:
    st.markdown('<div class="brand">◆ Straddle Desk</div>', unsafe_allow_html=True)
    page = st.radio("", ["Live run", "Backtest", "History"], label_visibility="collapsed")
    st.markdown("---")
    _now = dt.datetime.now()
    _over = _now.time() >= dt.time(15, 30)
    _wk = dt.date.today().weekday() >= 5
    _pill = ("pill-off", "Closed — weekend") if _wk else             (("pill-off", f"Session over · {_now:%H:%M}") if _over
             else ("pill-live", f"Session open · {_now:%H:%M}"))
    st.markdown(f'<span class="pill {_pill[0]}">{_pill[1]}</span>', unsafe_allow_html=True)
    st.caption(f"Front-month token `{NIFTY_FUT_TOKEN}` — update after each roll.")
    st.caption(f"Tighten mode: `{TIGHTEN_MODE}`")


def render_kpis(S, variants, sessions_note=""):
    cols = st.columns(len(variants) + 1)
    for c, v in zip(cols, variants):
        g = S[S.strategy == v[0]]
        pnl = pd.to_numeric(g.total_pnl, errors="coerce").sum() if not g.empty else 0
        grad = GRADS.get(v[0], "g1") if pnl >= 0 else "gneg"
        legs = int(pd.to_numeric(g.num_legs, errors="coerce").sum()) if not g.empty else 0
        with c:
            st.markdown(kpi(v[0].replace("_", " "), money(pnl), f"{legs} legs", grad),
                        unsafe_allow_html=True)
    with cols[-1]:
        n = S.trading_date.nunique() if not S.empty else 0
        st.markdown(kpi("Sessions", str(n), sessions_note, "g4"), unsafe_allow_html=True)


# ---------------- Live run ----------------
if page == "Live run":
    st.markdown(panel_open("Live run",
                "FIXED_1_3X and DYNAMIC_SL · paper only, no orders placed") +
                "</div>", unsafe_allow_html=True)
    today = dt.date.today()
    c1, c2, c3 = st.columns([1.1, 1.1, 1])
    with c1:
        st.markdown(stat("Trading day", today.strftime("%d %b %Y")), unsafe_allow_html=True)
    try:
        exps = expiries_for(today)
    except Exception as e:
        st.error(f"Could not load expiries: {e}"); st.stop()
    with c2:
        exp_live = st.selectbox("Expiry", exps,
                                format_func=lambda d: f"{d:%d %b %Y} · {(d-today).days}d",
                                key="exp_live")
    session_over = dt.datetime.now().time() >= dt.time(15, 30)
    with c3:
        st.write("")
        go = st.button("Run session", type="primary", use_container_width=True)

    if today.weekday() >= 5:
        st.warning("Weekend — no session.")
    elif not session_over:
        st.info(f"Session still open. Results are provisional and nothing is written "
                f"to the sheet until you run after 15:30.")

    if go:
        p = st.progress(0.0, text="fetching")
        try:
            out, logs, notes, err = run_dates([today], "LIVE", VARIANTS_LIVE,
                                              expiry_override=exp_live,
                                              finalize=session_over, progress=p)
            p.empty()
            if err:
                st.error(err)
            else:
                T, S, H, O = out
                for n in notes:
                    st.warning(n)
                if not S.empty:
                    render_kpis(S, VARIANTS_LIVE, today.strftime("%d %b"))
                if not O.empty:
                    st.markdown(panel_open("Still open",
                                "session unfinished — not written to the sheet") +
                                "</div>", unsafe_allow_html=True)
                    st.dataframe(O, use_container_width=True, hide_index=True)
                if not T.empty:
                    a, b = st.columns([2, 1])
                    with a:
                        st.markdown(panel_open("Closed legs") + "</div>",
                                    unsafe_allow_html=True)
                        st.dataframe(T.drop(columns=["run_type"]),
                                     use_container_width=True, hide_index=True)
                    with b:
                        ch = reason_chart(T)
                        if ch is not None:
                            st.markdown(panel_open("Exit reasons") + "</div>",
                                        unsafe_allow_html=True)
                            st.altair_chart(ch, use_container_width=True)
                if T.empty and O.empty:
                    st.info("No trades — entry conditions not met.")

                if session_over and not T.empty:
                    append_rows("LIVE", TAB_TRADES, TRADE_HEADERS, T.values.tolist())
                    append_rows("LIVE", TAB_SUMMARY, SUMMARY_HEADERS, S.values.tolist())
                    if not H.empty:
                        append_rows("LIVE", TAB_HEARTBEAT, HEARTBEAT_HEADERS, H.values.tolist())
                    st.success(f"{len(T)} legs written to the live sheet.")
                with st.expander("Engine log"):
                    st.code("\n".join(logs) or "(nothing)")
        except Exception as e:
            p.empty(); st.error(f"{type(e).__name__}: {e}")

# ---------------- Backtest ----------------
elif page == "Backtest":
    st.markdown(panel_open("Backtest",
                "FIXED_1_3X · DYNAMIC_SL · NAKED_CARRY — one shared data feed") +
                "</div>", unsafe_allow_html=True)
    c1, c2, c3 = st.columns(3)
    mode = c1.radio("Range", ["Single day", "Date range"], key="bt_mode")
    if mode == "Single day":
        bt_day = c2.date_input("Trading day", value=dt.date.today(), key="bt_day")
        days = [bt_day] if bt_day.weekday() < 5 else []
        ref = bt_day
    else:
        d1 = c2.date_input("From", value=dt.date.today()-dt.timedelta(days=7), key="bt_from")
        d2 = c3.date_input("To", value=dt.date.today(), key="bt_to")
        days = [d for d in (d1+dt.timedelta(days=k) for k in range((d2-d1).days+1))
                if d.weekday() < 5]
        ref = d1
    try:
        bexps = expiries_for(ref)
    except Exception as e:
        st.error(f"Could not load expiries: {e}"); st.stop()
    c4, c5, c6 = st.columns([1.2, 1.2, 1])
    auto = c4.checkbox("Nearest expiry per day", value=True,
                       help="Uncheck to force one expiry across the range.")
    exp_bt = None if auto else c5.selectbox("Expiry", bexps,
                                            format_func=lambda d: f"{d:%d %b %Y}",
                                            key="exp_bt")
    save = c6.checkbox("Save to sheet", value=False)
    if st.button("Run backtest", type="primary"):
        if not days:
            st.warning("No weekdays selected.")
        else:
            p = st.progress(0.0, text="fetching")
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
                        st.info("No trades over that range.")
                    else:
                        render_kpis(S, VARIANTS_BACKTEST, f"{len(days)} weekdays")
                        a, b = st.columns([2, 1])
                        with a:
                            ch = equity_chart(S)
                            if ch is not None:
                                st.markdown(panel_open("Cumulative P&L") + "</div>",
                                            unsafe_allow_html=True)
                                st.altair_chart(ch, use_container_width=True)
                        with b:
                            rc = reason_chart(T)
                            if rc is not None:
                                st.markdown(panel_open("Exit reasons") + "</div>",
                                            unsafe_allow_html=True)
                                st.altair_chart(rc, use_container_width=True)
                        st.markdown(panel_open("Per-session summary") + "</div>",
                                    unsafe_allow_html=True)
                        st.dataframe(S.drop(columns=["run_type"]),
                                     use_container_width=True, hide_index=True)
                        d1_, d2_ = st.columns(2)
                        d1_.download_button("Download trades", T.to_csv(index=False),
                                            "straddle_trades.csv", "text/csv",
                                            use_container_width=True)
                        if not H.empty:
                            fired = (H.trend.astype(str) != "").sum()
                            st.caption(f"Heartbeat: {len(H)} bars evaluated, "
                                       f"trend fired on {fired}.")
                            d2_.download_button("Download heartbeat", H.to_csv(index=False),
                                                "straddle_heartbeat.csv", "text/csv",
                                                use_container_width=True)
                        if save:
                            append_rows("BACKTEST", TAB_TRADES, TRADE_HEADERS, T.values.tolist())
                            append_rows("BACKTEST", TAB_SUMMARY, SUMMARY_HEADERS, S.values.tolist())
                            if not H.empty:
                                append_rows("BACKTEST", TAB_HEARTBEAT, HEARTBEAT_HEADERS,
                                            H.values.tolist())
                            st.success("Written to the backtest sheet.")
                    with st.expander("Engine log"):
                        st.code("\n".join(logs) or "(nothing)")
            except Exception as e:
                p.empty(); st.error(f"{type(e).__name__}: {e}")

# ---------------- History ----------------
else:
    st.markdown(panel_open("History", "accumulated live paper record") + "</div>",
                unsafe_allow_html=True)
    src = st.radio("Source", ["Live", "Backtest"], horizontal=True)
    kind = "LIVE" if src == "Live" else "BACKTEST"
    variants = VARIANTS_LIVE if kind == "LIVE" else VARIANTS_BACKTEST
    if st.button("Load"):
        S = read_tab(kind, TAB_SUMMARY)
        if S.empty:
            st.info("Nothing recorded yet.")
        else:
            S["total_pnl"] = pd.to_numeric(S.total_pnl, errors="coerce")
            render_kpis(S, variants, f"{S.trading_date.nunique()} recorded")
            ch = equity_chart(S)
            if ch is not None:
                st.markdown(panel_open("Cumulative P&L") + "</div>", unsafe_allow_html=True)
                st.altair_chart(ch, use_container_width=True)
            st.markdown(panel_open("Sessions") + "</div>", unsafe_allow_html=True)
            st.dataframe(S.tail(80), use_container_width=True, hide_index=True)
            st.download_button("Download summary", S.to_csv(index=False),
                               f"{kind.lower()}_summary.csv", "text/csv")
