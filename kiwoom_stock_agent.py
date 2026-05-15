#!/usr/bin/env python3
import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import urllib.request
import urllib.error


def now_iso():
    return datetime.now(timezone.utc).isoformat()


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

    def _request(self, method: str, url: str, headers=None, json_body=None, timeout=10):
        headers = dict(headers or {})
        data = json.dumps(json_body).encode("utf-8") if json_body is not None else None
        req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {e.code} {e.reason}: {raw[:500]}")

    def refresh_token(self):
        body = {
            "grant_type": "client_credentials",
            "appkey": self.cfg["app_key"],
            "appsecret": self.cfg["app_secret"],
            "secretkey": self.cfg["app_secret"],
        }
        data = self._request("POST", self.base + self.cfg["endpoints"]["token_issue"], json_body=body, timeout=15)
        access = data.get("access_token") or data.get("token")
        if not access:
            raise RuntimeError(f"token response parse failed: {data}")
        self.cfg.setdefault("token", {})["access_token"] = access
        self.cfg["token"]["expires_at"] = now_iso()

    def quote(self, symbol: str):
        return self._request("POST", self.base + "/api/dostk/stkinfo", headers=self._headers("quote_basic"), json_body={"stk_cd": symbol}, timeout=10)

    def order(self, side: str, symbol: str, qty: int):
        body = {"dmst_stex_tp": "KRX", "stk_cd": symbol, "ord_qty": str(int(qty)), "ord_uv": "", "trde_tp": "3", "cond_uv": ""}
        tr = "order_cash_buy" if side == "buy" else "order_cash_sell"
        return self._request("POST", self.base + self.cfg["endpoints"]["order_cash"], headers=self._headers(tr), json_body=body, timeout=10)


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str):
        self.token = token
        self.chat_id = chat_id

    def send(self, text: str):
        if not self.token or not self.chat_id:
            return
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        payload = json.dumps({"chat_id": self.chat_id, "text": text}).encode("utf-8")
        req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
        try:
            urllib.request.urlopen(req, timeout=8).read()
        except Exception:
            pass


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
        self.client = KiwoomRestClient(self.cfg)
        self.journal_path = Path(self.cfg["runtime"]["journal_path"])
        self.journal = self._load_journal()
        tcfg = self.cfg.get("telegram", {})
        self.notifier = TelegramNotifier(tcfg.get("bot_token", ""), tcfg.get("chat_id", ""))

    def _load_journal(self):
        if self.journal_path.exists():
            return json.loads(self.journal_path.read_text())
        return {"created_at": now_iso(), "orders": [], "daily_pnl": [], "invested_capital": 3_000_000, "cum_return_pct": 0.0}

    def _save(self):
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        self.journal_path.write_text(json.dumps(self.journal, ensure_ascii=False, indent=2))
        self.cfg_path.write_text(json.dumps(self.cfg, ensure_ascii=False, indent=2))

    def _top10(self):
        return ["005930","000660","005380","000270","207940","035420","051910","068270","105560","035720"]

    def _select_symbol_strategy(self):
        backtest = self.journal.get("latest_backtest") or {}
        uni = (backtest.get("universes") or {}).get("kospi_top10") or {}
        best = None
        for sym, info in (uni.get("symbol_results") or {}).items():
            for st, m in (info.get("strategies") or {}).items():
                row = {"symbol": sym, "strategy": st, "score": float(m.get("total_return_pct", -999)), "params": m.get("params") or {}}
                if best is None or row["score"] > best["score"]:
                    best = row
        if best:
            return best
        return {"symbol": "005930", "strategy": "trend_momentum", "score": 0.0, "params": {}}

    def run_live_once(self):
        capital = int(self.cfg.get("runtime", {}).get("invest_capital_krw", 3_000_000))
        pick = self._select_symbol_strategy()
        q = self.client.quote(pick["symbol"])
        price = abs(float(str(q.get("cur_prc", "0")).replace(",", "") or 0))
        qty = int(capital // price) if price > 0 else 0
        order_resp = {"skipped": True, "reason": "qty=0 or dry_run"}
        dry_run = bool(self.cfg.get("runtime", {}).get("dry_run", True))
        if qty > 0 and not dry_run:
            order_resp = self.client.order("buy", pick["symbol"], qty)

        est_value = qty * price
        pnl_pct = round(pick["score"], 4)
        result = {
            "ts": now_iso(),
            "symbol": pick["symbol"],
            "strategy": pick["strategy"],
            "params": pick["params"],
            "price": price,
            "qty": qty,
            "invest_capital": capital,
            "est_position_value": est_value,
            "expected_return_pct": pnl_pct,
            "order_response": order_resp,
            "dry_run": dry_run,
        }
        self.journal.setdefault("orders", []).append(result)
        self.journal["cum_return_pct"] = round((self.journal.get("cum_return_pct", 0.0) + pnl_pct), 4)
        self._save()

        msg = (
            f"[키움 자동매매 알림]\n"
            f"선정종목: {pick['symbol']}\n"
            f"전략: {pick['strategy']}\n"
            f"매수가(참고): {price:,.0f}원 / 수량: {qty}주\n"
            f"투자금: {capital:,.0f}원\n"
            f"예상 수익률(백테스트 기반): {pnl_pct:.2f}%\n"
            f"누적 수익률: {self.journal.get('cum_return_pct',0.0):.2f}%\n"
            f"실행모드: {'DRY_RUN' if dry_run else 'LIVE'}"
        )
        self.notifier.send(msg)
        return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="./kiwoom_runtime_config.json")
    ap.add_argument("--live-once", action="store_true")
    args = ap.parse_args()

    if not os.path.exists(args.config):
        raise SystemExit("config not found")

    agent = Agent(args.config)
    agent.client.refresh_token()
    agent._save()

    if args.live_once:
        print(json.dumps(agent.run_live_once(), ensure_ascii=False, indent=2))
        return


if __name__ == "__main__":
    main()
