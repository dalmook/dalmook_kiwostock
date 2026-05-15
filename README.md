# dalmook_kiwostock

키움증권 자동매매(실전/드라이런) 스크립트입니다.

## 415 오류(Unsupported Media Type) 해결
키움 토큰 발급 API는 환경에 따라 JSON이 아니라 `application/x-www-form-urlencoded`를 요구합니다.
현재 코드는 기본적으로 form 방식으로 토큰을 요청합니다.

- `runtime.token_content_type` 기본값: `form`
- 필요 시 `json`으로 변경 가능

## 설정 파일 만들기
```bash
cp kiwoom_runtime_config.template.json kiwoom_runtime_config.json
```

필수 입력:
- `app_key`, `app_secret`
- `telegram.bot_token`, `telegram.chat_id`

권장 설정:
- `runtime.invest_capital_krw`: `3000000`
- `runtime.dry_run`: 실전 전 `true`
- `runtime.token_content_type`: `form`

## 실행
```bash
python3 kiwoom_stock_agent.py --config ./kiwoom_runtime_config.json --live-once
```

## Synology Container Manager
```bash
docker compose -f docker-compose.synology.yml up -d
```

> compose는 `restart: on-failure:3`로 설정되어, 인증 오류 시 무한 재시작 스팸을 줄입니다.
