"""
MPCI Engine — EconDB 공용 HTTP 유틸리티
region widget 엔드포인트(단건 GET)용 공유 클라이언트/헬퍼.

단건 GET은 search 페이지네이션과 달리 403/WAF 없이 plain httpx로 통과.
make_region_client()는 워밍업 없이 브라우저 헤더만 설정한다.

재시도: 지수 백오프, 최대 3회, 타임아웃 60s.
응답이 JSON이 아니거나 비표준(plots 없음 등)이면 None 반환(로그+스킵, 예외 전파 금지).
"""

import logging
import time

import httpx

logger = logging.getLogger(__name__)

ECONDB_BASE = "https://www.econdb.com"
WARMUP_URL = f"{ECONDB_BASE}/maritime/search/"

# page>=2 WAF 우회용 브라우저 헤더 (region 단건 GET도 동일 헤더 재사용)
BROWSER_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9,ko;q=0.8",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": WARMUP_URL,
}

# EconDB locode → 내부 코드 매핑 (실제 불일치만 등록)
#   CN TAO = Qingdao  → 내부 CNQIN
#   VN VUT = Vung Tau(≈Cai Mep) → 내부 VNTOT
ECONDB_LOCODE_ALIAS: dict[str, str] = {
    "CNTAO": "CNQIN",
    "VNVUT": "VNTOT",
}


def normalize_locode(locode: str) -> str:
    """'CN TAO' → 'CNTAO' (공백 제거, 대문자 정규화)."""
    return "".join(locode.strip().upper().split())


def make_region_client() -> httpx.Client:
    """
    Region widget 엔드포인트용 단순 httpx 클라이언트.
    단건 GET이라 워밍업/쿠키 불필요. 브라우저 헤더로 WAF 우회.
    호출자가 close()를 담당한다(또는 컨텍스트 매니저 사용).
    """
    return httpx.Client(
        headers=BROWSER_HEADERS,
        follow_redirects=True,
        timeout=60.0,
    )


def region_get(
    client: httpx.Client,
    url: str,
    max_retries: int = 3,
    timeout: float = 60.0,
) -> dict | None:
    """
    단건 GET + 지수 백오프 재시도(최대 max_retries회, 타임아웃 timeout초).

    반환:
      - 정상 JSON dict → dict
      - 403/429/비정상/JSON 아님 → None (로그+스킵, 예외 전파 금지)

    가드레일:
      - 응답이 dict가 아니면 None
      - 값 날조/추정 채움 금지
    """
    for attempt in range(1, max_retries + 1):
        try:
            resp = client.get(url, timeout=timeout)
            if resp.status_code in (403, 429):
                logger.warning(
                    "EconDB region GET blocked status=%s url=%s", resp.status_code, url
                )
                return None
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, dict):
                logger.warning(
                    "Unexpected response type %s url=%s", type(data).__name__, url
                )
                return None
            return data
        except Exception as exc:
            if attempt == max_retries:
                logger.error(
                    "EconDB region GET failed after %s attempts url=%s: %s",
                    max_retries, url, exc,
                )
                return None
            sleep_s = 2 ** attempt  # 2s, 4s
            logger.warning(
                "Retrying region GET (attempt %s/%s) in %ss url=%s: %s",
                attempt, max_retries, sleep_s, url, exc,
            )
            time.sleep(sleep_s)

    return None  # unreachable, but satisfies type checker
