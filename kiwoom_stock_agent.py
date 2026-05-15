#!/usr/bin/env python3
import argparse
import json
import os
import random
from dataclasses import dataclass
from datetime import datetime, timezone
try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None
from pathlib import Path
from typing import Dict, List, Optional

import urllib.parse
import urllib.request
import urllib.error


def now_iso():
    return datetime.now(timezone.utc).isoformat()




def generate_sample_backtest_data(data_root: str):
    root = Path(data_root)
    random.seed(42)
    specs = {
        "kospi50": [
            "005930","000660","005380","012330","035420","051910","006400","068270","207940","035720",
            "028260","105560","055550","032830","066570","096770","003670","015760","034730","086790",
            "010950","017670","003550","018260","259960","011200","024110","009150","000270","010130",
            "323410","003490","000810","329180","000100","011790","030200","004020","000720","009540",
            "138040","071050","047810","011170","021240","006800","036570","302440","161390","078930"
        ],
        "kosdaq100": ["091990", "247540", "066970"],
    }
    for universe, symbols in specs.items():
        udir = root / universe
        udir.mkdir(parents=True, exist_ok=True)
        for sym in symbols:
            rows = []
            price = 50000.0 + random.randint(1000, 5000)
            for d in range(120):
                drift = 0.0011 if universe == "kospi50" else 0.0018
                shock = random.uniform(-0.02, 0.02)
                ret = drift + shock
                open_p = price
                close_p = max(1000.0, open_p * (1 + ret))
                high = max(open_p, close_p) * (1 + random.uniform(0, 0.01))
                low = min(open_p, close_p) * (1 - random.uniform(0, 0.01))
                rows.append({
                    "date": f"2025-{(d // 20) + 1:02d}-{(d % 20) + 1:02d}",
                    "open": round(open_p, 2),
                    "high": round(high, 2),
                    "low": round(low, 2),
                    "close": round(close_p, 2),
                    "volume": int(100000 + random.randint(0, 250000)),
                })
                price = close_p
            (udir / f"{sym}.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2))
@dataclass
class Position:
    symbol: str
    qty: int
    entry_price: float
    strategy: str
    entered_at: str
    stop_price: float
    tp_price: float


class KiwoomRestClient:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.base = cfg["base_url"].rstrip("/")

    def _headers(self, tr_key: Optional[str] = None):
        h = {
            "Content-Type": "application/json; charset=utf-8",
            "appkey": self.cfg["app_key"],
            "appsecret": self.cfg["app_secret"],
            "secretkey": self.cfg["app_secret"],
        }
        tok = self.cfg.get("token", {}).get("access_token")
        if tok:
            h["authorization"] = f"Bearer {tok}"
        if tr_key:
            h["api-id"] = self.cfg.get("tr_id", {}).get(tr_key, tr_key)
        return h

    def _url(self, key: str):
        return self.base + self.cfg["endpoints"][key]

    def _request(self, method: str, url: str, headers=None, params=None, json_body=None, timeout=10):
        headers = dict(headers or {})
        if params:
            url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        data = None
        if json_body is not None:
            data = json.dumps(json_body).encode("utf-8")
            headers.setdefault("Content-Type", "application/json; charset=utf-8")
        req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {e.code} {e.reason}: {raw[:500]}")

    def refresh_token(self):
        endpoint = self._url("token_issue")
        body = {
            "grant_type": "client_credentials",
            "appkey": self.cfg["app_key"],
            "appsecret": self.cfg["app_secret"],
            "secretkey": self.cfg["app_secret"],
        }
        data = self._request("POST", endpoint, json_body=body, timeout=15)
        access = data.get("access_token") or data.get("token")
        if not access:
            raise RuntimeError(f"token response parse failed: {data}")
        self.cfg.setdefault("token", {})["access_token"] = access
        self.cfg["token"]["expires_at"] = now_iso()

    def quote(self, symbol: str):
        url = self.base + "/api/dostk/stkinfo"
        return self._request("POST", url, headers=self._headers("quote_basic"), json_body={"stk_cd": symbol}, timeout=10)

    def daily(self, symbol: str):
        return self.quote(symbol)

    def balance(self):
        url = self.base + "/api/dostk/acnt"
        return self._request("POST", url, headers=self._headers("balance_eval"), json_body={"qry_tp": "2", "dmst_stex_tp": "KRX"}, timeout=10)

    def order(self, side: str, symbol: str, qty: int, price: float = 0.0, order_type: str = "3"):
        if qty <= 0:
            raise ValueError("qty must be positive")
        body = {
            "dmst_stex_tp": "KRX",
            "stk_cd": symbol,
            "ord_qty": str(int(qty)),
            "ord_uv": str(int(price)) if price else "",
            "trde_tp": order_type,
            "cond_uv": "",
        }
        tr = "order_cash_buy" if side == "buy" else "order_cash_sell"
        return self._request("POST", self._url("order_cash"), headers=self._headers(tr), json_body=body, timeout=10)


class Backtester:
    def __init__(self, data_root: str, fee_bps: float = 5.0, slippage_bps: float = 3.0, initial_cash: float = 10_000_000.0):
        self.data_root = Path(data_root)
        self.fee_bps = fee_bps
        self.slippage_bps = slippage_bps
        self.initial_cash = initial_cash

    @staticmethod
    def _num(v, default=0.0):
        try:
            if isinstance(v, str):
                v = v.replace(",", "").strip()
            return float(v)
        except Exception:
            return default

    def _load_series(self, universe: str) -> Dict[str, List[dict]]:
        path = self.data_root / universe
        if not path.exists():
            raise FileNotFoundError(f"backtest data path not found: {path}")
        out = {}
        for f in sorted(path.glob("*.json")):
            rows = json.loads(f.read_text())
            out[f.stem] = rows
        if not out:
            raise RuntimeError(f"no json files under {path}")
        return out

    def _signal(self, closes: List[float], i: int, strategy: str, params: dict) -> int:
        lookback = int(params.get("lookback", 20))
        if i < max(lookback + 1, 6):
            return 0
        px = closes[i]
        if strategy == "trend_momentum":
            ma = sum(closes[i-lookback:i]) / lookback
            return 1 if px > ma else 0
        if strategy == "mean_reversion":
            ma = sum(closes[i-lookback:i]) / lookback
            band = float(params.get("entry_band", 0.985))
            return 1 if px < ma * band else 0
        recent_high = max(closes[i-lookback:i])
        return 1 if px >= recent_high else 0

    def _simulate(self, rows: List[dict], strategy: str, params: Optional[dict] = None) -> dict:
        params = params or {}
        closes = [self._num(r.get("close")) for r in rows if self._num(r.get("close")) > 0]
        dates = [r.get("date") for r in rows if self._num(r.get("close")) > 0]
        if len(closes) < 25:
            return {
                "final_equity": self.initial_cash,
                "total_return_pct": 0.0,
                "max_drawdown_pct": 0.0,
                "win_rate": 0.0,
                "trade_count": 0,
                "equity_curve": [],
                "trades": [],
            }

        cash = self.initial_cash
        qty = 0
        entry_price = 0.0
        entry_date = None
        equity_curve = []
        trades = []
        fee = self.fee_bps / 10000.0
        slip = self.slippage_bps / 10000.0

        for i in range(len(closes)):
            signal = self._signal(closes, i, strategy, params)
            px = closes[i]
            dt = dates[i]

            if qty == 0 and signal == 1:
                buy_px = px * (1 + slip)
                qty = int(cash // buy_px)
                if qty > 0:
                    cost = qty * buy_px
                    fee_amt = cost * fee
                    cash -= (cost + fee_amt)
                    entry_price = buy_px
                    entry_date = dt
            elif qty > 0:
                exit_lb = int(params.get("exit_lookback", 5))
                ma_exit = sum(closes[max(0, i-exit_lb):i+1]) / min(i+1, exit_lb + 1)
                exit_band = float(params.get("exit_band", 0.99))
                exit_signal = signal == 0 or px < ma_exit * exit_band
                if exit_signal:
                    sell_px = px * (1 - slip)
                    gross = qty * sell_px
                    fee_amt = gross * fee
                    pnl = gross - fee_amt - (qty * entry_price)
                    cash += (gross - fee_amt)
                    trades.append({
                        "entry_date": entry_date,
                        "exit_date": dt,
                        "entry_price": round(entry_price, 4),
                        "exit_price": round(sell_px, 4),
                        "qty": qty,
                        "pnl": round(pnl, 2),
                        "return_pct": round((sell_px - entry_price) / entry_price * 100, 4),
                        "strategy": strategy,
                    })
                    qty = 0
                    entry_price = 0.0
                    entry_date = None

            equity = cash + qty * px
            equity_curve.append(equity)

        if qty > 0:
            px = closes[-1] * (1 - slip)
            gross = qty * px
            fee_amt = gross * fee
            pnl = gross - fee_amt - (qty * entry_price)
            cash += (gross - fee_amt)
            trades.append({
                "entry_date": entry_date,
                "exit_date": dates[-1],
                "entry_price": round(entry_price, 4),
                "exit_price": round(px, 4),
                "qty": qty,
                "pnl": round(pnl, 2),
                "return_pct": round((px - entry_price) / entry_price * 100, 4),
                "strategy": strategy,
            })
            equity_curve[-1] = cash

        final_equity = equity_curve[-1] if equity_curve else self.initial_cash
        total_return_pct = (final_equity / self.initial_cash - 1) * 100

        peak = self.initial_cash
        mdd = 0.0
        for eq in equity_curve:
            peak = max(peak, eq)
            dd = (eq / peak - 1) * 100
            mdd = min(mdd, dd)

        wins = sum(1 for t in trades if t["pnl"] > 0)
        trade_count = len(trades)
        win_rate = (wins / trade_count * 100) if trade_count else 0.0

        return {
            "final_equity": round(final_equity, 2),
            "total_return_pct": round(total_return_pct, 4),
            "max_drawdown_pct": round(mdd, 4),
            "win_rate": round(win_rate, 2),
            "trade_count": trade_count,
            "trades": trades,
        }


    def _param_grid(self, strategy: str):
        if strategy == "trend_momentum":
            return [{"lookback": l, "exit_lookback": e, "exit_band": b} for l in (10, 20, 40) for e in (3, 5, 10) for b in (0.985, 0.99)]
        if strategy == "mean_reversion":
            return [{"lookback": l, "entry_band": eb, "exit_lookback": e, "exit_band": b} for l in (5, 10, 20) for eb in (0.975, 0.98, 0.985) for e in (3, 5, 10) for b in (0.995, 1.0)]
        return [{"lookback": l, "exit_lookback": e, "exit_band": b} for l in (10, 20, 40) for e in (3, 5, 10) for b in (0.985, 0.99)]

    def _best_simulation(self, rows: List[dict], strategy: str) -> dict:
        best = None
        for params in self._param_grid(strategy):
            m = self._simulate(rows, strategy, params)
            score = (m.get("total_return_pct", -999), m.get("win_rate", 0.0), -abs(m.get("max_drawdown_pct", 0.0)))
            if best is None or score > best[0]:
                best = (score, params, m)
        out = dict(best[2])
        out["params"] = best[1]
        return out
    def run(self, universes: List[str]) -> dict:
        strategy_pool = ["trend_momentum", "mean_reversion", "breakout"]
        result = {
            "generated_at": now_iso(),
            "assumptions": {"fee_bps": self.fee_bps, "slippage_bps": self.slippage_bps, "initial_cash": self.initial_cash},
            "universes": {},
            "best_global": None,
        }
        global_returns = {s: [] for s in strategy_pool}

        for uni in universes:
            data = self._load_series(uni)
            per_symbol = {}
            for sym, rows in data.items():
                strategies = {}
                for st in strategy_pool:
                    m = self._best_simulation(rows, st)
                    strategies[st] = m
                    global_returns[st].append(m["total_return_pct"])
                best = max(strategies.items(), key=lambda x: x[1]["total_return_pct"])[0]
                per_symbol[sym] = {"strategies": strategies, "best": best}

            avg = {}
            for st in strategy_pool:
                vals = [per_symbol[s]["strategies"][st]["total_return_pct"] for s in per_symbol]
                avg[st] = round(sum(vals) / max(1, len(vals)), 4)

            result["universes"][uni] = {
                "symbol_count": len(per_symbol),
                "avg_total_return_pct": avg,
                "symbol_results": per_symbol,
                "universe_best": max(avg.items(), key=lambda x: x[1])[0],
            }

        global_avg = {s: round(sum(vs) / max(1, len(vs)), 4) for s, vs in global_returns.items()}
        result["best_global"] = max(global_avg.items(), key=lambda x: x[1])[0]
        result["global_avg_total_return_pct"] = global_avg
        return result


def optimize_backtest(data_root: str, universes: Optional[List[str]] = None):
    universes = universes or ["kospi50", "kosdaq100"]
    fee_grid = [3.0, 5.0, 8.0, 12.0]
    slippage_grid = [1.0, 3.0, 5.0, 8.0]
    initial_cash = 10_000_000.0

    trials = []
    best = None
    for fee in fee_grid:
        for slip in slippage_grid:
            bt = Backtester(data_root, fee_bps=fee, slippage_bps=slip, initial_cash=initial_cash)
            r = bt.run(universes)
            score = float((r.get("global_avg_total_return_pct") or {}).get(r.get("best_global"), -999.0))
            trial = {
                "fee_bps": fee,
                "slippage_bps": slip,
                "best_global": r.get("best_global"),
                "global_avg_total_return_pct": r.get("global_avg_total_return_pct"),
                "score": round(score, 4),
            }
            trials.append(trial)
            if best is None or score > best["score"]:
                best = trial

    best_bt = Backtester(data_root, fee_bps=best["fee_bps"], slippage_bps=best["slippage_bps"], initial_cash=initial_cash)
    best_result = best_bt.run(universes)

    out = {
        "generated_at": now_iso(),
        "universes": universes,
        "search_space": {"fee_bps": fee_grid, "slippage_bps": slippage_grid},
        "best": best,
        "top5": sorted(trials, key=lambda x: x["score"], reverse=True)[:5],
        "best_result": best_result,
    }
    return out


class Agent:
    def __init__(self, cfg_path: str, backtest_data_root: str = "./backtest_data"):
        self.backtest_data_root = backtest_data_root
        self.cfg_path = Path(cfg_path)
        self.cfg = json.loads(self.cfg_path.read_text())
        self.cfg.setdefault("tr_id", {})
        self.cfg["tr_id"].setdefault("quote_basic", "ka10001")
        self.cfg["tr_id"].setdefault("balance_eval", "kt00018")
        self.client = KiwoomRestClient(self.cfg)

        runtime = self.cfg["runtime"]
        self.report_dir = Path(runtime["report_dir"])
        self.report_dir.mkdir(parents=True, exist_ok=True)
        self.journal_path = Path(runtime["journal_path"])
        self.dry_run = bool(runtime.get("dry_run", True))

        self.journal = self._load_journal()

    def _load_journal(self):
        if self.journal_path.exists():
            return json.loads(self.journal_path.read_text())
        return {
            "created_at": now_iso(),
            "positions": [],
            "trades": [],
            "daily": {},
            "weights": {"A": 0.15, "B": 0.35, "C": 0.05, "D": 0.45},
            "regime": "RISK_OFF"
        }

    def _save(self):
        self.journal_path.write_text(json.dumps(self.journal, ensure_ascii=False, indent=2))
        self.cfg_path.write_text(json.dumps(self.cfg, ensure_ascii=False, indent=2))

    @staticmethod
    def _num(v, default=0.0):
        try:
            if isinstance(v, str):
                v = v.replace(",", "").strip()
            return float(v)
        except Exception:
            return default

    def _is_regular_session(self):
        if not self.cfg.get("runtime", {}).get("trade_only_regular_session", True):
            return True, "disabled"
        if ZoneInfo is None:
            return False, "timezone unavailable"
        kst = datetime.now(ZoneInfo("Asia/Seoul"))
        if kst.weekday() >= 5:
            return False, "weekend"
        hhmm = kst.hour * 100 + kst.minute
        if 900 <= hhmm <= 1520:
            return True, kst.strftime("%H:%M KST")
        return False, kst.strftime("%H:%M KST outside regular session")

    def _scan_symbols(self):
        watch = self.cfg["universe"].get("watch_symbols", [])
        if not watch:
            return []
        limit = int(self.cfg.get("runtime", {}).get("scan_limit", 8) or 8)
        limit = max(1, min(limit, len(watch)))
        cursor = int(self.journal.get("scan_cursor", 0) or 0) % len(watch)
        if limit >= len(watch):
            symbols = watch
            next_cursor = 0
        else:
            symbols = [watch[(cursor + i) % len(watch)] for i in range(limit)]
            next_cursor = (cursor + limit) % len(watch)
        self.journal["scan_cursor"] = next_cursor
        return symbols

    def select_strategy(self, regime: str) -> str:
        backtest = self.journal.get("latest_backtest") or {}
        global_best = backtest.get("best_global")
        mapping = {
            "TREND_UP": "trend_momentum",
            "RANGE": "mean_reversion",
            "HIGH_VOL_EVENT": "breakout",
            "RISK_OFF": "risk_off_cash",
        }
        if global_best and regime in ("TREND_UP", "RANGE", "HIGH_VOL_EVENT"):
            return global_best
        return mapping.get(regime, "risk_off_cash")



    def run_backtest_and_store(self, universes: Optional[List[str]] = None):
        universes = universes or ["kospi50", "kosdaq100"]
        bt = Backtester(self.backtest_data_root)
        result = bt.run(universes)
        self.journal["latest_backtest"] = result
        out = self.report_dir / f"backtest_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        out.write_text(json.dumps(result, ensure_ascii=False, indent=2))
        self.journal["latest_backtest_file"] = str(out)
        self._save()
        return result, out

    def _select_symbols_from_backtest(self, regime: str, topn: int = 20) -> List[str]:
        backtest = self.journal.get("latest_backtest") or {}
        universes = backtest.get("universes") or {}
        strategy = self.select_strategy(regime)
        ranking = []
        for _, uni in universes.items():
            for sym, info in (uni.get("symbol_results") or {}).items():
                sc = ((info.get("strategies") or {}).get(strategy) or {}).get("total_return_pct")
                if isinstance(sc, (int, float)):
                    ranking.append((sym, sc))
        ranking.sort(key=lambda x: x[1], reverse=True)
        return [sym for sym, _ in ranking[:topn]]
    def classify_regime(self):
        watch = self._scan_symbols()
        rows = []
        errors = []
        for sym in watch:
            try:
                q = self.client.quote(sym)
                row = {"symbol": sym, "name": q.get("stk_nm", sym), "price": abs(self._num(q.get("cur_prc"))), "changePct": self._num(q.get("flu_rt")), "volume": abs(self._num(q.get("trde_qty")))}
                if row["price"] <= 0:
                    errors.append(f"{sym}:zero_or_closed_quote")
                    continue
                rows.append(row)
            except Exception as e:
                errors.append(f"{sym}:{str(e)[:80]}")
        if not rows:
            score = {"Trend": 0, "Volatility": 80, "Breadth": 0, "Flow": 0}
            return "MARKET_CLOSED_OR_API_DEGRADED", score, rows, errors
        pos = sum(1 for r in rows if r["changePct"] > 0)
        avg = sum(r["changePct"] for r in rows) / len(rows)
        breadth = round(pos / len(rows) * 100)
        trend = max(0, min(100, round(50 + avg * 10)))
        vol = max(20, min(90, round(sum(abs(r["changePct"]) for r in rows) / len(rows) * 18)))
        score = {"Trend": trend, "Volatility": vol, "Breadth": breadth, "Flow": breadth}
        if vol > 70:
            regime = "HIGH_VOL_EVENT"
        elif breadth >= 62 and avg > 0.4:
            regime = "TREND_UP"
        elif breadth <= 35 and avg < -0.4:
            regime = "RISK_OFF"
        else:
            regime = "RANGE"
        return regime, score, rows, errors


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="/home/node/.openclaw/workspace/kiwoom_rest_config.json")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--backtest-data-root", default="./backtest_data")
    ap.add_argument("--run-backtest", action="store_true")
    ap.add_argument("--init-sample-backtest-data", action="store_true")
    ap.add_argument("--optimize-backtest", action="store_true")
    args = ap.parse_args()

    if args.init_sample_backtest_data:
        generate_sample_backtest_data(args.backtest_data_root)

    if (args.run_backtest or args.optimize_backtest) and not os.path.exists(args.config):
        if args.optimize_backtest:
            result = optimize_backtest(args.backtest_data_root, ["kospi50", "kosdaq100"])
            out = Path(args.backtest_data_root) / f"backtest_optimization_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
            out.write_text(json.dumps(result, ensure_ascii=False, indent=2))
            print(json.dumps({"saved_to": str(out), "result": result}, ensure_ascii=False, indent=2))
            return
        bt = Backtester(args.backtest_data_root)
        result = bt.run(["kospi50", "kosdaq100"])
        out = Path(args.backtest_data_root) / f"backtest_result_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        out.write_text(json.dumps(result, ensure_ascii=False, indent=2))
        print(json.dumps({"saved_to": str(out), "result": result}, ensure_ascii=False, indent=2))
        return

    if not os.path.exists(args.config):
        raise SystemExit("config not found")

    agent = Agent(args.config, backtest_data_root=args.backtest_data_root)

    if args.optimize_backtest:
        opt = optimize_backtest(args.backtest_data_root, ["kospi50", "kosdaq100"])
        out = agent.report_dir / f"backtest_optimization_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        out.write_text(json.dumps(opt, ensure_ascii=False, indent=2))
        agent.journal["latest_backtest_optimization"] = opt
        agent.journal["latest_backtest_optimization_file"] = str(out)
        agent.journal["latest_backtest"] = opt.get("best_result")
        agent.journal["latest_backtest_file"] = str(out)
        agent._save()
        print(json.dumps({"saved_to": str(out), "result": opt}, ensure_ascii=False, indent=2))
        return

    if args.run_backtest:
        result, out = agent.run_backtest_and_store(["kospi50", "kosdaq100"])
        print(json.dumps({"saved_to": str(out), "result": result}, ensure_ascii=False, indent=2))
        return

    if args.once:
        regime, score, rows, errors = agent.classify_regime()
        strategy = agent.select_strategy(regime)
        backtest_symbols = agent._select_symbols_from_backtest(regime, topn=20)
        live_symbols = {r.get("symbol") for r in rows}
        tradable_priority = [s for s in backtest_symbols if s in live_symbols]
        print(json.dumps({
            "regime": regime,
            "score": score,
            "strategy": strategy,
            "backtest_file": agent.journal.get("latest_backtest_file"),
            "preferred_symbols": backtest_symbols[:10],
            "tradable_priority": tradable_priority[:5],
            "quotes": rows[:5],
            "errors": errors[:3]
        }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
