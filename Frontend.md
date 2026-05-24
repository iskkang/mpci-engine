# MPCI Engine — Frontend PRD
> 파일 위치: `mpci-engine/frontend/index.html` (단일 파일)
> 배포: Vercel 또는 GitHub Pages
> 스택: Vanilla HTML + CSS + JavaScript (빌드 도구 없음)
> 외부 라이브러리: Leaflet.js (지도), Supabase JS SDK

---

## 0. 작업 원칙

- 단일 파일 `frontend/index.html`로 완성한다. CSS, JS 모두 이 파일 안에 넣는다.
- Supabase anon key는 파일 상단 CONFIG 객체에 위치시킨다. (공개 읽기 전용 키라 코드에 포함 가능)
- 빌드 없이 브라우저에서 바로 열리는 정적 HTML이어야 한다.
- 모바일 대응 불필요. 데스크톱 기준 1280px 이상.
- 완료 후 git add -A && git commit -m "feat: frontend dashboard" && git push

---

## 1. CONFIG 객체 (파일 최상단)

```javascript
const CONFIG = {
  SUPABASE_URL: "https://xxxx.supabase.co",   // 실제 값으로 교체 필요
  SUPABASE_ANON_KEY: "eyJhbGci...",            // 실제 값으로 교체 필요
  REFRESH_INTERVAL_MS: 5 * 60 * 1000,         // 5분 자동 갱신
};
```

---

## 2. 전체 레이아웃

```
┌─────────────────────────────────────────────────────────┐
│  HEADER: "MPCI · MTL Port Congestion Index"  [갱신시각] │
├──────────────────────────┬──────────────────────────────┤
│                          │  PANEL A: 초크포인트 리스크  │
│   WORLD MAP              │  (Suez / Panama / Malacca 등)│
│   (Leaflet.js)           ├──────────────────────────────┤
│   50개 항만 버블         │  PANEL B: 이상 감지 알림     │
│                          │  (anomaly_flags 최근 5건)    │
│                          ├──────────────────────────────┤
│                          │  PANEL C: 레벨별 항만 요약   │
│                          │  CONGESTED / BUSY / STABLE   │
├──────────────────────────┴──────────────────────────────┤
│  FOOTER: 항만 테이블 (50개, 정렬/필터 가능)              │
└─────────────────────────────────────────────────────────┘
```

---

## 3. 색상 및 레벨 정의

```javascript
const LEVEL_CONFIG = {
  CONGESTED: { color: "#ef4444", label: "혼잡",   bg: "#fef2f2" },
  BUSY:      { color: "#f97316", label: "주의",   bg: "#fff7ed" },
  STABLE:    { color: "#eab308", label: "보통",   bg: "#fefce8" },
  LOW:       { color: "#22c55e", label: "원활",   bg: "#f0fdf4" },
};
```

전체 배경: `#0f172a` (다크), 패널: `#1e293b`, 텍스트: `#e2e8f0`

---

## 4. WORLD MAP (Leaflet.js)

### 4-1. 설정

```javascript
// CDN
// https://unpkg.com/leaflet@1.9.4/dist/leaflet.css
// https://unpkg.com/leaflet@1.9.4/dist/leaflet.js

const map = L.map("map", {
  center: [20, 10],
  zoom: 2,
  minZoom: 2,
  maxZoom: 6,
  zoomControl: true,
});

// 다크 타일
L.tileLayer("https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png", {
  attribution: "© OpenStreetMap © CARTO",
  subdomains: "abcd",
}).addTo(map);
```

### 4-2. 항만 마커

port_snapshots 테이블에서 데이터를 가져와 CircleMarker로 표시한다.

```javascript
// 각 항만마다 CircleMarker 생성
L.circleMarker([port.lat, port.lon], {
  radius: 8 + Math.min(port.tpfs / 10, 8),  // tpfs에 따라 크기 8~16px
  fillColor: LEVEL_CONFIG[port.level].color,
  color: "#ffffff",
  weight: 1,
  opacity: 0.9,
  fillOpacity: 0.8,
})
.bindPopup(`
  <b>${port.port_name}</b><br>
  레벨: ${LEVEL_CONFIG[port.level].label}<br>
  TPFS: ${port.tpfs.toFixed(1)}<br>
  묘박: ${port.vessels_anchored}척 | 접안: ${port.vessels_berthed}척<br>
  갱신: ${formatTime(port.updated_at)}
`)
.addTo(map);
```

### 4-3. 초크포인트 라인 (선택 표시)

chokepoint_daily에서 오늘 데이터를 가져와 주요 초크포인트 위치에 아이콘 표시.

```javascript
const CHOKEPOINT_COORDS = {
  "Suez Canal":           [30.5, 32.3],
  "Panama Canal":         [9.1,  -79.7],
  "Strait of Malacca":    [2.5,  101.5],
  "Bab-el-Mandeb":        [12.5, 43.3],
  "Strait of Hormuz":     [26.5, 56.5],
  "Turkish Straits":      [41.0, 29.0],
  "Danish Straits":       [57.5, 10.5],
};

// 각 초크포인트에 다이아몬드 마커 표시
// 리스크 HIGH면 빨간색, 정상이면 흰색
```

---

## 5. PANEL A — 초크포인트 리스크

chokepoint_daily에서 각 portid의 최근 7일 데이터를 가져와 트렌드를 표시한다.

```
┌─────────────────────────────────────┐
│ 🚢 초크포인트 현황                   │
├─────────┬──────┬────────┬───────────┤
│ 이름    │ 오늘 │ 7일평균 │ 상태      │
├─────────┼──────┼────────┼───────────┤
│ Suez    │  12  │  17.3  │ ⚠️ 주의   │
│ Panama  │  25  │  24.1  │ ✅ 정상   │
│ Malacca │   8  │   9.2  │ ✅ 정상   │
└─────────┴──────┴────────┴───────────┘
```

- 오늘 값 / 7일 평균 비교
- ratio < 0.6이면 ⚠️ 주의, < 0.4이면 🔴 위험
- 데이터 없으면 "데이터 수집 중" 표시

---

## 6. PANEL B — 이상 감지 알림

anomaly_flags 테이블에서 최근 5건, acknowledged=false인 것 우선 표시.

```
┌─────────────────────────────────────────┐
│ 🚨 이상 감지 알림                        │
├─────────────────────────────────────────┤
│ [HIGH] SGSIN · 묘박 선박 급증           │
│ ratio 3.2x · 2026-05-24 09:00          │
│                               [확인] 버튼│
├─────────────────────────────────────────┤
│ 알림 없음 (정상 운영 중)                 │
└─────────────────────────────────────────┘
```

- [확인] 버튼 클릭 시 `acknowledged=true`로 UPDATE (service key 없이 anon으로는 불가 → 이 기능은 일단 시각적으로만 구현하고 실제 DB 업데이트는 생략)

---

## 7. PANEL C — 레벨별 요약

port_snapshots에서 level 컬럼 집계.

```
┌──────────────────────────────────┐
│  🔴 CONGESTED  2개               │
│  🟠 BUSY       7개               │
│  🟡 STABLE    18개               │
│  🟢 LOW       23개               │
└──────────────────────────────────┘
```

숫자 클릭 시 하단 테이블을 해당 레벨로 필터링.

---

## 8. FOOTER — 항만 테이블

port_snapshots 전체를 tpfs 내림차순으로 정렬한 테이블.

| 항만 | 국가 | 레벨 | TPFS | 묘박 | 접안 | 갱신 |
|------|------|------|------|------|------|------|
| Singapore | SG | 🟠 BUSY | 48.0 | 7 | 2 | 13분 전 |

- 레벨별 필터 버튼: [전체] [혼잡] [주의] [보통] [원활]
- 국가별 필터: 드롭다운
- 정렬: 컬럼 헤더 클릭으로 오름/내림차순

---

## 9. 데이터 로딩 로직

```javascript
// Supabase JS SDK CDN
// https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2/dist/umd/supabase.min.js

const { createClient } = supabase;
const db = createClient(CONFIG.SUPABASE_URL, CONFIG.SUPABASE_ANON_KEY);

async function loadAllData() {
  showLoading(true);
  try {
    // 1. 항만 현황 (port_snapshots 전체)
    const { data: ports } = await db
      .from("port_snapshots")
      .select("*")
      .order("tpfs", { ascending: false });

    // 2. 초크포인트 최근 8일 (chokepoint_daily)
    const since = new Date();
    since.setDate(since.getDate() - 8);
    const { data: chokepoints } = await db
      .from("chokepoint_daily")
      .select("portid, portname, recorded_date, n_container, n_total")
      .gte("recorded_date", since.toISOString().split("T")[0])
      .order("recorded_date", { ascending: false });

    // 3. 미확인 이상 플래그
    const { data: anomalies } = await db
      .from("anomaly_flags")
      .select("*")
      .eq("acknowledged", false)
      .order("detected_at", { ascending: false })
      .limit(5);

    renderMap(ports);
    renderChokepointPanel(chokepoints);
    renderAnomalyPanel(anomalies);
    renderSummaryPanel(ports);
    renderTable(ports);
    updateLastRefresh();
  } catch (err) {
    console.error("Data load error:", err);
  } finally {
    showLoading(false);
  }
}

// 5분마다 자동 갱신
loadAllData();
setInterval(loadAllData, CONFIG.REFRESH_INTERVAL_MS);
```

---

## 10. 헬퍼 함수

```javascript
// 시간 포맷: "13분 전", "2시간 전"
function formatRelativeTime(isoString) { ... }

// 숫자 포맷: 소수점 1자리
function fmt(n) { return n?.toFixed(1) ?? "-"; }

// 레벨 뱃지 HTML
function levelBadge(level) {
  const c = LEVEL_CONFIG[level] || LEVEL_CONFIG.LOW;
  return `<span style="background:${c.color};color:#fff;padding:2px 8px;border-radius:4px;font-size:11px;">${c.label}</span>`;
}
```

---

## 11. 배포 방법

### GitHub Pages (무료)
```
레포 Settings → Pages → Source: main branch / frontend 폴더
→ https://iskkang.github.io/mpci-engine/
```

### Vercel (무료)
```
vercel.com → New Project → mpci-engine 연결
→ Root Directory: frontend
→ Deploy
```

---

## 12. 구현 순서

```
Step 1: frontend/ 폴더 생성, index.html 기본 뼈대 작성
        (DOCTYPE, head, body, CONFIG 객체, CDN 링크)

Step 2: 레이아웃 CSS 작성
        (header, map-container, right-panels, footer-table)

Step 3: Leaflet 지도 초기화 + 다크 타일 레이어

Step 4: Supabase 데이터 로딩 함수 작성

Step 5: 항만 마커 렌더링 (CircleMarker + Popup)

Step 6: 초크포인트 패널 렌더링

Step 7: 이상 감지 패널 렌더링

Step 8: 레벨 요약 패널 렌더링

Step 9: 하단 항만 테이블 + 필터/정렬

Step 10: 자동 갱신 + 로딩 상태 처리

Step 11: 전체 검토 후 git push
```

---

## 13. 절대 하지 말 것

- SUPABASE_SERVICE_KEY를 프론트엔드 코드에 넣지 않는다. anon key만 사용한다.
- React, Vue, npm 빌드 도구 사용 금지. 순수 HTML/JS만.
- 외부 CDN은 Leaflet, Supabase JS SDK 두 개만 사용한다.
- port_history 테이블을 프론트엔드에서 직접 쿼리하지 않는다. (데이터 너무 많음)