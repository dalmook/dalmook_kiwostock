#!/usr/bin/env python3
import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import urllib.parse
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

    def _request(self, method: str, url: str, headers=None, json_body=None, form_body=None, timeout=10):
        headers = dict(headers or {})
        data = None
        if form_body is not None:
            data = urllib.parse.urlencode(form_body).encode("utf-8")
            headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
        elif json_body is not None:
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
        body = {
            "grant_type": "client_credentials",
            "appkey": self.cfg["app_key"],
            "appsecret": self.cfg["app_secret"],
            "secretkey": self.cfg["app_secret"],
        }
        token_ct = self.cfg.get("runtime", {}).get("token_content_type", "form")
        if token_ct == "json":
            data = self._request("POST", self.base + self.cfg["endpoints"]["token_issue"], headers={"Content-Type": "application/json; charset=utf-8"}, json_body=body, timeout=15)
        else:
            data = self._request("POST", self.base + self.cfg["endpoints"]["token_issue"], headers={"Content-Type": "application/x-www-form-urlencoded"}, form_body=body, timeout=15)
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


class Agent:
    def __init__(self, cfg_path: str):
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
    try:
        agent.client.refresh_token()
        agent._save()
    except Exception as e:
        print(f"[warn] token refresh failed: {e}")
        # 토큰 실패 시 무한 재시작 루프를 줄이기 위해 live-once가 아니면 종료
        if not args.live_once:
            raise

    if args.live_once:
        print(json.dumps(agent.run_live_once(), ensure_ascii=False, indent=2))
        return


if __name__ == "__main__":
    main()
