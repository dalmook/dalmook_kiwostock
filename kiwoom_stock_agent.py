#!/usr/bin/env python3
import argparse
import json
import os
import time
from dataclasses import dataclass, asdict
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
        # 키움 스펙별 필드명 차이 대비
        access = data.get("access_token") or data.get("token")
        if not access:
            raise RuntimeError(f"token response parse failed: {data}")
        self.cfg.setdefault("token", {})["access_token"] = access
        self.cfg["token"]["expires_at"] = now_iso()

    def quote(self, symbol: str):
        # Kiwoom REST domestic stock basic info: POST /api/dostk/stkinfo, api-id ka10001
        url = self.base + "/api/dostk/stkinfo"
        return self._request("POST", url, headers=self._headers("quote_basic"), json_body={"stk_cd": symbol}, timeout=10)

    def daily(self, symbol: str):
        # Keep as optional; not used for live orders until response schema is verified.
        return self.quote(symbol)

    def balance(self):
        # Kiwoom account evaluation balance: POST /api/dostk/acnt, api-id kt00018
        url = self.base + "/api/dostk/acnt"
        return self._request("POST", url, headers=self._headers("balance_eval"), json_body={"qry_tp": "2", "dmst_stex_tp": "KRX"}, timeout=10)

    def order(self, side: str, symbol: str, qty: int, price: float = 0.0, order_type: str = "3"):
        # Kiwoom REST domestic cash order. order_type/trde_tp: 3=market in current Kiwoom schema.
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


class Agent:
    def __init__(self, cfg_path: str):
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
            # Fail closed if timezone support is unavailable.
            return False, "timezone unavailable"
        kst = datetime.now(ZoneInfo("Asia/Seoul"))
        if kst.weekday() >= 5:
            return False, "weekend"
        hhmm = kst.hour * 100 + kst.minute
        # Regular cash session only. Avoid pre-open/after-hours market orders.
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

    def classify_regime(self):
        # Live quote based regime: breadth + average price change among rotating KOSPI200 watch symbols.
        watch = self._scan_symbols()
        rows = []
        errors = []
        for sym in watch:
            try:
                q = self.client.quote(sym)
                row = {
                    "symbol": sym,
                    "name": q.get("stk_nm", sym),
                    "price": abs(self._num(q.get("cur_prc"))),
                    "changePct": self._num(q.get("flu_rt")),
                    "volume": abs(self._num(q.get("trde_qty"))),
                }
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
        flow = breadth
        score = {"Trend": trend, "Volatility": vol, "Breadth": breadth, "Flow": flow}

        if vol > 70:
            regime = "HIGH_VOL_EVENT"
        elif breadth >= 62 and avg > 0.4:
            regime = "TREND_UP"
        elif breadth <= 35 and avg < -0.4:
            regime = "RISK_OFF"
        else:
            regime = "RANGE"
        return regime, score, rows, errors

    def target_weights(self, regime: str):
        if regime == "TREND_UP":
            return {"A": 0.55, "B": 0.05, "C": 0.15, "D": 0.25}
        if regime == "RANGE":
            return {"A": 0.15, "B": 0.35, "C": 0.05, "D": 0.45}
        if regime == "HIGH_VOL_EVENT":
            return {"A": 0.15, "B": 0.0, "C": 0.20, "D": 0.65}
        return {"A": 0.05, "B": 0.0, "C": 0.0, "D": 0.95}

    def pre_open_report(self):
        cr = self.classify_regime()
        regime, score = cr[0], cr[1]
        quote_rows = cr[2] if len(cr) > 2 else []
        quote_errors = cr[3] if len(cr) > 3 else []
        weights = self.target_weights(regime)
        watch = self.cfg["universe"]["watch_symbols"][:5]
        report = {
            "ts": now_iso(),
            "type": "pre_open",
            "regime": regime,
            "scores": score,
            "weights": weights,
            "watch5": watch,
            "quotes": quote_rows[:5],
            "quote_errors": quote_errors[:5],
            "buy_conditions": [
                "국면-전략 일치",
                "유동성 충분",
                "손절 기준 명확",
                "손익비 >= 1.5",
                "시장 역풍 아님"
            ],
            "forbidden": ["물타기", "손실포지션 추가매수", "저유동성 종목 진입", "무리한 추격매수"],
            "risk_status": {
                "daily_stop": "-2.0%",
                "weekly_stop": "-4.0%",
                "monthly_stop": "-8.0%"
            }
        }
        out = self.report_dir / f"pre_open_{datetime.now().strftime('%Y%m%d')}.json"
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        self.journal["regime"] = regime
        self.journal["weights"] = weights
        self._save()
        return report

    def run_once(self):
        regime, score, quote_rows, quote_errors = self.classify_regime()
        balance_summary = None
        balance_error = None
        try:
            b = self.client.balance()
            account_positions = b.get("acnt_evlt_remn_indv_tot", []) or []
            account_symbols = []
            for pos in account_positions:
                sym = str(pos.get("stk_cd") or pos.get("pdno") or pos.get("symbol") or "").strip()
                # Kiwoom may prefix domestic stock codes with A. Normalize to 6-digit code.
                if sym.startswith("A") and len(sym) == 7:
                    sym = sym[1:]
                if sym:
                    account_symbols.append(sym)
            balance_summary = {
                "totalPurchase": b.get("tot_pur_amt"),
                "totalEval": b.get("tot_evlt_amt"),
                "totalPnl": b.get("tot_evlt_pl"),
                "profitRate": b.get("tot_prft_rt"),
                "positionsCount": len(account_positions),
                "accountSymbols": account_symbols,
            }
        except Exception as e:
            balance_error = str(e)[:200]

        self.journal["regime"] = regime
        self.journal["weights"] = self.target_weights(regime)
        self.journal["last_quotes"] = quote_rows
        self.journal["last_balance"] = balance_summary
        self.journal["last_errors"] = {"quotes": quote_errors, "balance": balance_error}
        self.journal["updated_at"] = now_iso()
        self._save()
        action = "WAIT"
        action_reason = "no eligible live stock order"
        # Live stock rule: only buy our strategy positions in TREND_UP, never average down, never sell pre-existing holdings.
        # Current account already has positions; respect max_positions cap.
        try:
            risk = self.cfg.get("risk", {})
            # max_positions is for positions opened by this agent, not unrelated legacy/manual holdings.
            # Previously we counted every account holding here, so pre-existing holdings blocked the agent forever.
            max_strategy_pos = int(risk.get("max_strategy_positions", risk.get("max_positions", 3)))
            strategy_symbols = set()
            for pos in self.journal.get("positions", []) or []:
                sym = str(pos.get("symbol", "")).strip()
                if sym:
                    strategy_symbols.add(sym)
            for order in self.journal.get("orders", []) or []:
                if order.get("side") == "buy":
                    sym = str(order.get("symbol", "")).strip()
                    if sym:
                        strategy_symbols.add(sym)
            account_symbols = set((balance_summary or {}).get("accountSymbols") or [])
            market_open, market_reason = self._is_regular_session()
            if self.dry_run:
                action_reason = "dry_run true"
            elif not market_open:
                action_reason = f"market closed: {market_reason}"
            elif balance_error or quote_errors:
                action_reason = "api degraded"
            elif len(strategy_symbols) >= max_strategy_pos:
                action_reason = f"strategy max_positions reached ({len(strategy_symbols)}/{max_strategy_pos})"
            elif regime in ("TREND_UP", "HIGH_VOL_EVENT") and quote_rows and not quote_errors:
                if regime == "HIGH_VOL_EVENT" and (score.get("Trend", 0) < 70 or score.get("Breadth", 0) < 60):
                    action_reason = f"regime {regime}; high-vol trend/breadth guard not met"
                else:
                    capital = float(self.cfg.get("capital_krw", 0) or 0)
                    max_stock_pct = float(risk.get("max_per_stock_pct", 0.25))
                    max_notional = capital * max_stock_pct if capital > 0 else None
                    candidates = [
                        r for r in quote_rows
                        if r.get("symbol") not in strategy_symbols
                        and r.get("symbol") not in account_symbols
                        and (max_notional is None or float(r.get("price") or 0) <= max_notional)
                    ]
                    if not candidates:
                        action_reason = "no eligible symbol: already held or 1-share price exceeds risk cap"
                    else:
                        candidate = max(candidates, key=lambda r: (r.get("changePct", 0), r.get("volume", 0)))
                        # One-share starter only; no averaging into existing account/strategy holdings; per-stock cap checked above.
                        resp = self.client.order("buy", candidate["symbol"], 1, 0, "3")
                        action = "BUY_SENT"
                        action_reason = f"market buy 1 share {candidate['symbol']}"
                        self.journal.setdefault("orders", []).append({"ts": now_iso(), "side": "buy", "symbol": candidate["symbol"], "qty": 1, "response": resp})
                        self._save()
            else:
                action_reason = f"regime {regime}; waiting"
        except Exception as e:
            action = "ORDER_ERROR"
            action_reason = str(e)[:300]

        return {
            "regime": regime,
            "score": score,
            "status": "ok" if not quote_errors and not balance_error else "degraded",
            "dry_run": self.dry_run,
            "quotes": quote_rows[:5],
            "balance": balance_summary,
            "errors": {"quotes": quote_errors[:3], "balance": balance_error},
            "liveOrdersEnabled": not self.dry_run,
            "action": action,
            "reason": action_reason
        }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="/home/node/.openclaw/workspace/kiwoom_rest_config.json")
    ap.add_argument("--preopen-report", action="store_true")
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()

    if not os.path.exists(args.config):
        raise SystemExit(f"config not found: {args.config}\ncopy kiwoom_rest_config.template.json -> kiwoom_rest_config.json and fill values")

    agent = Agent(args.config)

    try:
        agent.client.refresh_token()
        agent._save()
    except Exception as e:
        print(f"[warn] token refresh skipped/failed: {e}")

    if args.preopen_report:
        r = agent.pre_open_report()
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return

    if args.once:
        print(json.dumps(agent.run_once(), ensure_ascii=False, indent=2))
        return

    poll = int(agent.cfg["runtime"].get("poll_seconds", 20))
    while True:
        try:
            print(json.dumps(agent.run_once(), ensure_ascii=False))
        except Exception as e:
            print(f"[error] {e}")
        time.sleep(max(5, poll))


if __name__ == "__main__":
    main()
