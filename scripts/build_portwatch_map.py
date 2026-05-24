"""
PortWatch 수동 매핑 생성기 (run-once helper).

watchlist 의 LOCODE 를 PortWatch 카탈로그의 portid 로 *정확 LOCODE 매칭만* 사용해 해석하고,
fetch_portwatch_ports 가 읽는 PORTWATCH_PORT_MAP 환경변수 문자열을 출력한다.
런타임에서 퍼지(geo_name) 매칭을 배제하기 위한 도구 — portid 는 PortWatch 실제 데이터에서만
가져오며 절대 추정하지 않는다.

의존성: httpx 만 필요 (Supabase/mpci_core 불필요).
사용:
    python build_portwatch_map.py
    PORTWATCH_WATCHLIST="USLAX,KRPUS,..."  python build_portwatch_map.py   # 대상 지정
출력:
    PORTWATCH_PORT_MAP=USLAX:<portid>,KRPUS:<portid>,...   (그대로 env/secret 에 설정)
    + LOCODE 매칭 실패 목록(카탈로그에서 직접 portid 확인 후 수동 추가)
"""

import os
import sys

import httpx

CATALOG_URL = (
    "https://services9.arcgis.com/weJ1QsnbMYJlCHdG/arcgis/rest/services"
    "/PortWatch_ports_database/FeatureServer/0/query"
)
PAGE_SIZE = 1000

# fetch_portwatch_ports.DEFAULT_WATCHLIST 와 동일 (단일 출처로 빼면 더 좋음)
DEFAULT_WATCHLIST = {
    "USLAX", "USLGB", "USNYC", "USSAV", "CAVAN",
    "CNSHA", "CNNGB", "CNSZX", "CNQIN", "CNYTN", "CNYAT", "HKHKG",
    "SGSIN", "MYTPP", "MYPKG", "THLCH", "VNTOT", "PHMNL",
    "KRPUS", "JPTYO", "JPYOK", "JPNGO",
    "NLRTM", "BEANR", "DEHAM", "GBFXT", "FRLEH", "ESVLC", "ITGOA",
    "INNSA", "INMUN", "LKCMB", "AEJEA", "OMSLL",
    "BRSSZ", "MXZLO", "ZADUR", "EGPSD",
}


def normalize_code(value: str | None) -> str:
    return "".join((value or "").upper().split())


def get_watchlist() -> set[str] | None:
    raw = (os.getenv("PORTWATCH_WATCHLIST") or os.getenv("ECONDB_WATCHLIST") or "").strip()
    if raw.upper() == "ALL":
        return None
    if raw:
        return {normalize_code(part) for part in raw.split(",") if part.strip()}
    return set(DEFAULT_WATCHLIST)


def fetch_catalog() -> list[dict]:
    records: list[dict] = []
    offset = 0
    with httpx.Client(timeout=45) as client:
        while True:
            params = {
                "where": "1=1",
                "outFields": "portid,portname,country,LOCODE,lat,lon",
                "f": "json",
                "resultRecordCount": PAGE_SIZE,
                "resultOffset": offset,
            }
            resp = client.get(CATALOG_URL, params=params)
            resp.raise_for_status()
            data = resp.json()
            if data.get("error"):
                raise RuntimeError(data["error"])
            for feature in data.get("features", []):
                attr = feature.get("attributes") or {}
                if attr.get("portid"):
                    records.append(attr)
            if not data.get("exceededTransferLimit"):
                break
            offset += PAGE_SIZE
    return records


def main() -> None:
    watchlist = get_watchlist()
    if watchlist is None:
        print("ALL 모드에서는 수동맵을 생성하지 않습니다. PORTWATCH_WATCHLIST 로 대상을 지정하세요.")
        sys.exit(1)

    print(f"# PortWatch 카탈로그 조회 중... (watchlist {len(watchlist)}개)", file=sys.stderr)
    catalog = fetch_catalog()
    print(f"# 카탈로그 {len(catalog)}개 항만 로드됨", file=sys.stderr)

    by_locode: dict[str, dict] = {}
    for row in catalog:
        lc = normalize_code(row.get("LOCODE"))
        if lc and lc not in by_locode:
            by_locode[lc] = row

    mapped: dict[str, tuple[str, str]] = {}   # code -> (portid, name)
    missing: list[str] = []
    for code in sorted(watchlist):
        row = by_locode.get(code)
        if row and row.get("portid"):
            mapped[code] = (str(row["portid"]), row.get("portname") or "")
        else:
            missing.append(code)

    pairs = ",".join(f"{code}:{pid}" for code, (pid, _) in mapped.items())

    print("\n# ===== 복사해서 PORTWATCH_PORT_MAP 으로 설정 =====")
    print(f"PORTWATCH_PORT_MAP={pairs}")

    print(f"\n# ----- LOCODE 정확매칭 {len(mapped)}/{len(watchlist)} -----")
    for code, (pid, name) in mapped.items():
        print(f"#   {code} -> {pid}  ({name})")

    if missing:
        print(f"\n# ----- 매칭 실패 {len(missing)}개 (PortWatch 카탈로그에서 portid 직접 확인 후 수동 추가) -----")
        for code in missing:
            print(f"#   {code}  : <portid 미상>")
    else:
        print("\n# 전 항만 LOCODE 매칭 성공 — 퍼지 매칭 불필요")


if __name__ == "__main__":
    main()
