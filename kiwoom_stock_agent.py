#!/usr/bin/env python3
import argparse
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

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
        token_url = self.base + self.cfg["endpoints"]["token_issue"]
        mode = self.cfg.get("runtime", {}).get("token_content_type", "form")

        # 기본: form. 실패하면 json 자동 fallback
        if mode == "json":
            first, second = "json", "form"
        else:
            first, second = "form", "json"

        last_err = None
        for attempt in (first, second):
            try:
                if attempt == "form":
                    data = self._request("POST", token_url, headers={"Content-Type": "application/x-www-form-urlencoded"}, form_body=body, timeout=15)
                else:
                    data = self._request("POST", token_url, headers={"Content-Type": "application/json; charset=utf-8"}, json_body=body, timeout=15)
                access = data.get("access_token") or data.get("token")
                if not access:
                    raise RuntimeError(f"token response parse failed: {data}")
                self.cfg.setdefault("token", {})["access_token"] = access
                self.cfg["token"]["expires_at"] = now_iso()
                self.cfg.setdefault("runtime", {})["token_content_type"] = attempt
                return
            except Exception as e:
                last_err = e
        raise RuntimeError(f"token refresh failed in both form/json mode: {last_err}")

    def quote(self, symbol: str):
        return self._request("POST", self.base + "/api/dostk/stkinfo", headers={**self._headers("quote_basic"), "Content-Type": "application/json; charset=utf-8"}, json_body={"stk_cd": symbol}, timeout=10)


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
        self.cfg.setdefault("tr_id", {})
        self.cfg["tr_id"].setdefault("quote_basic", "ka10001")
        self.client = KiwoomRestClient(self.cfg)
        self.journal_path = Path(self.cfg["runtime"]["journal_path"])
        self.journal = self._load_journal()
        tcfg = self.cfg.get("telegram", {})
        self.notifier = TelegramNotifier(tcfg.get("bot_token", ""), tcfg.get("chat_id", ""))

    def _load_journal(self):
        if self.journal_path.exists():
            return json.loads(self.journal_path.read_text())
        return {"created_at": now_iso(), "orders": [], "cum_return_pct": 0.0}

    def _save(self):
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        self.journal_path.write_text(json.dumps(self.journal, ensure_ascii=False, indent=2))
        self.cfg_path.write_text(json.dumps(self.cfg, ensure_ascii=False, indent=2))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="./kiwoom_runtime_config.json")
    ap.add_argument("--live-once", action="store_true", help="1회 실행 후 종료")
    ap.add_argument("--loop", action="store_true", help="주기 실행(컨테이너 상시 구동용)")
    ap.add_argument("--interval", type=int, default=60, help="loop 모드 주기(초)")
    args = ap.parse_args()

    if not os.path.exists(args.config):
        raise SystemExit("config not found")

    agent = Agent(args.config)

    def run_tick():
        agent.client.refresh_token()
        agent._save()
        print(json.dumps({"ok": True, "token_mode": agent.cfg.get("runtime", {}).get("token_content_type"), "live_once": bool(args.live_once), "ts": now_iso()}, ensure_ascii=False), flush=True)

    if args.loop:
        interval = max(5, int(args.interval or 60))
        while True:
            try:
                run_tick()
            except Exception as e:
                print(json.dumps({"ok": False, "error": str(e), "ts": now_iso()}, ensure_ascii=False), flush=True)
            time.sleep(interval)
        return

    run_tick()


if __name__ == "__main__":
    main()
