"""
MPCI 공용 코어 — EconDB 현재 지수 산식의 단일 출처(single source of truth).

fetch_econdb.calc_current_index 와 eta_engine.calc_econdb_mpci 가 동일한 산식을
중복 보유하던 문제를 제거하기 위해 분리. 정규화 상수와 가중치는 *오직 여기서만* 정의한다.

배치 주의:
    - eta_engine.py 가 속한 앱 패키지와 같은 디렉터리에 둔다.
    - fetch_econdb.py 가 다른 디렉터리에서 standalone 으로 돌면, 이 파일도
      그쪽에서 import 가능하도록(같은 폴더 복사 / PYTHONPATH / 패키지화) 해줄 것.
    두 파일 모두 아래의 dual-import 패턴으로 패키지/standalone 양쪽을 흡수한다.
"""

# 정규화 기준 (EconDB 실제 스케일이 확인되면 여기 한 곳만 보정하면 된다)
CONGESTION_FULL_SCALE = 18.0     # port_congestion 이 0~18 범위라는 가정
TURNAROUND_MIN_DAYS = 0.5        # 회항시간 하한(일)
TURNAROUND_SPAN_DAYS = 4.5       # 0.5~5.0일 → 0~100 매핑

# 가중치: (혼잡, 지연, 회항)
MPCI_WEIGHTS = (0.35, 0.35, 0.30)

# final_mpci 합성 가중치 (현재지수, PortWatch 역사지수, [AIS])
FINAL_WEIGHTS_AIS = (0.50, 0.35, 0.15)
FINAL_WEIGHTS_NOAIS = (0.60, 0.40)


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(value, high))


def normalize_components(
    congestion, delay_pct, turnaround
) -> tuple[float, float, float] | None:
    """원시 EconDB 값 3개를 각각 0~100 점수로 정규화. 하나라도 결측/비정상이면 None."""
    if congestion is None or delay_pct is None or turnaround is None:
        return None
    try:
        cong_score = clamp((float(congestion) / CONGESTION_FULL_SCALE) * 100.0, 0.0, 100.0)
        delay_score = clamp(float(delay_pct), 0.0, 100.0)
        turn_score = clamp(
            ((float(turnaround) - TURNAROUND_MIN_DAYS) / TURNAROUND_SPAN_DAYS) * 100.0,
            0.0, 100.0,
        )
    except (TypeError, ValueError):
        return None
    return cong_score, delay_score, turn_score


def compute_mpci(congestion, delay_pct, turnaround) -> float | None:
    """EconDB 현재 MPCI(0~100, 소수1자리) 계산. 입력이 부족하면 None.

    fetch_econdb 와 eta_engine 의 fallback 이 공통으로 호출하는 단일 진입점.
    """
    parts = normalize_components(congestion, delay_pct, turnaround)
    if parts is None:
        return None
    cong_score, delay_score, turn_score = parts
    wc, wd, wt = MPCI_WEIGHTS
    mpci = cong_score * wc + delay_score * wd + turn_score * wt
    return round(clamp(mpci, 0.0, 100.0), 1)


def combine_final_mpci(
    current_index, portwatch_index, ais_index=None
) -> tuple[float | None, str]:
    """현재지수(EconDB) + PortWatch 역사지수 (+선택적 AIS)를 final MPCI로 합성.

    final_mpci 합성식의 단일 출처. fetch_econdb 와 fetch_portwatch_ports 가 공통 호출한다.
    반환: (final_mpci 0~100 | None, confidence)
      - current 또는 portwatch 가 없거나 비정상 → (None, "portwatch_only")
      - AIS 있으면 (·, "portwatch_ais"), 없으면 (·, "portwatch_history")
    """
    if current_index is None or portwatch_index is None:
        return None, "portwatch_only"
    try:
        current_f = float(current_index)
        portwatch_f = float(portwatch_index)
    except (TypeError, ValueError):
        return None, "portwatch_only"

    if ais_index is not None:
        try:
            ais_f = clamp(float(ais_index), 0.0, 100.0)
            wc, wp, wa = FINAL_WEIGHTS_AIS
            final = current_f * wc + portwatch_f * wp + ais_f * wa
            return round(clamp(final, 0.0, 100.0), 1), "portwatch_ais"
        except (TypeError, ValueError):
            pass

    wc, wp = FINAL_WEIGHTS_NOAIS
    final = current_f * wc + portwatch_f * wp
    return round(clamp(final, 0.0, 100.0), 1), "portwatch_history"
