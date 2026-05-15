# dalmook_kiwostock

## 415 오류 즉시 대응
- 토큰 요청은 기본 `form`으로 전송하고, 실패 시 자동으로 `json` 재시도합니다.
- 성공한 방식은 `runtime.token_content_type`에 자동 저장됩니다.

## 실행
```bash
python3 kiwoom_stock_agent.py --config ./kiwoom_runtime_config.json
```

## 설정
- `kiwoom_runtime_config.template.json` 복사 후 키 입력
- `runtime.token_content_type` 기본값: `form`


호환 실행(기존 compose 설정):
```bash
python3 kiwoom_stock_agent.py --config ./kiwoom_runtime_config.json --live-once
```
