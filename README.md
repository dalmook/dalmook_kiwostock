# dalmook_kiwostock

키움증권 자동매매(실전/드라이런) 스크립트입니다.

## 목표 동작
- 키움 APP KEY/SECRET만 설정하면 실행
- 보유 중인 기존 종목과 무관하게(참조/청산 없이) 신규 300만원 포지션만 운용
- 코스피 상위 10개 대상 중 당일 최적 종목/전략 자동 선택
- 텔레그램으로 한글 알림(수익률/누적수익률/투자결과)

## 1) 설정 파일 만들기
```bash
cp kiwoom_runtime_config.template.json kiwoom_runtime_config.json
```

`kiwoom_runtime_config.json`에 아래 값 입력:
- `app_key`, `app_secret`
- `telegram.bot_token`, `telegram.chat_id`
- `runtime.invest_capital_krw` (기본 3000000)
- 실전 실행 시 `runtime.dry_run=false`

## 2) 단발 실행
```bash
python3 kiwoom_stock_agent.py --config ./kiwoom_runtime_config.json --live-once
```

## 3) Synology Container Manager (docker compose)
`docker-compose.synology.yml` 사용:
```bash
docker compose -f docker-compose.synology.yml up -d
```

## 텔레그램 알림 내용
- 선정 종목
- 전략
- 매수가/수량
- 투자금(300만원)
- 예상 수익률(백테스트 점수 기반)
- 누적 수익률
- 실행 모드(DRY_RUN/LIVE)
