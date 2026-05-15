#!/usr/bin/env python3
import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

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
    ap.add_argument("--live-once", action="store_true", help="호환 옵션(현재 1회 실행 기본)")
    args = ap.parse_args()

    if not os.path.exists(args.config):
        raise SystemExit("config not found")

    agent = Agent(args.config)
    agent.client.refresh_token()
    agent._save()
    print(json.dumps({"ok": True, "token_mode": agent.cfg.get("runtime", {}).get("token_content_type"), "live_once": bool(args.live_once)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
