# MPCI Engine

글로벌 항만 혼잡도 지수(MPCI) 수집 · 이상 감지 · 초크포인트 리스크 모니터링 시스템

## 스택
- Python 3.11
- Supabase (PostgreSQL)
- GitHub Actions (스케줄 수집)
- Railway (AIS 수집기 24/7)

## 구성 요소

| 파일 | 역할 | 실행 환경 |
|------|------|-----------|
| `collector.py` | AIS WebSocket 수집기 | Railway 24/7 |
| `scripts/fetch_portwatch.py` | PortWatch 초크포인트 일별 수집 | GitHub Actions (매일 KST 09:00) |
| `scripts/fetch_econdb.py` | EconDB 항만 지표 4시간 수집 | GitHub Actions (매 4시간) |
| `scripts/anomaly_detector.py` | 이상 감지 + 기준선 갱신 | GitHub Actions (매 2시간) |

## 환경변수

`.env.example`을 `.env`로 복사한 뒤 값을 채우세요.  
프로덕션 환경에서는 **절대 `.env`를 커밋하지 마세요** — GitHub Secrets / Railway Variables에만 저장합니다.

## 스키마 초기화

`schema.sql`을 Supabase SQL Editor에서 한 번 실행하면 모든 테이블이 생성됩니다.

## Railway 배포

1. Railway 프로젝트에서 이 레포를 연결합니다.
2. 환경변수를 Railway Variables에 설정합니다.
3. `railway.toml`의 `startCommand`가 자동으로 `collector.py`를 실행합니다.
