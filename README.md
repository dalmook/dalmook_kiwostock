# dalmook_kiwostock

키움 REST 기반 자동매매 에이전트 초안입니다.

## 핵심 기능
- 실시간 시세 기반 시장 국면 분류(TREND_UP/RANGE/HIGH_VOL_EVENT/RISK_OFF)
- 국면 + 백테스트 결과 기반 자동 전략 선택
- KOSPI 상위 50 / KOSDAQ100 백테스트 스코어링
- 백테스트 결과 파일 저장 + 결과 기반 우선 종목 자동 전환
- 백테스트 파라미터(수수료/슬리피지) 그리드 탐색 최적화
- 종목별 전략 파라미터(lookback, entry/exit band) 자동 탐색

## 빠른 백테스트 실행(샘플 데이터 자동 생성)
```bash
python3 kiwoom_stock_agent.py --run-backtest --init-sample-backtest-data --backtest-data-root ./backtest_data
```
- config 파일이 없어도 샘플 데이터 기반 백테스트를 실행합니다.
- 결과 파일은 `./backtest_data/backtest_result_YYYYmmdd_HHMMSS.json` 에 저장됩니다.

## 백테스트 최적화 실행(추천)
```bash
python3 kiwoom_stock_agent.py --optimize-backtest --init-sample-backtest-data --backtest-data-root ./backtest_data
```
- 수수료(`fee_bps`) x 슬리피지(`slippage_bps`) 조합을 전수 탐색합니다.
- `best`, `top5`, `best_result`를 포함한 결과를 `backtest_optimization_*.json`로 저장합니다.

## 실제 config 사용 백테스트
```bash
python3 kiwoom_stock_agent.py --config <config.json> --run-backtest --backtest-data-root ./backtest_data
```
- 결과는 `runtime.report_dir/backtest_YYYYmmdd_HHMMSS.json`와 journal(`latest_backtest`)에 저장됩니다.

## 런타임 1회 확인
```bash
python3 kiwoom_stock_agent.py --config <config.json> --once
```
출력 포함 항목: `strategy`, `preferred_symbols`, `tradable_priority`, `backtest_file`.

## 백테스트 산출 지표
- 종목별/전략별: `final_equity`, `total_return_pct`, `max_drawdown_pct`, `win_rate`, `trade_count`
- 거래 로그: 각 전략별 `trades` 배열에 `entry_date`, `exit_date`, `entry_price`, `exit_price`, `qty`, `pnl`, `return_pct` 저장
- 비용 반영: `fee_bps`, `slippage_bps` 적용
