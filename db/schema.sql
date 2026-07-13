CREATE TABLE IF NOT EXISTS place (
  place_id      TEXT PRIMARY KEY,
  name          TEXT NOT NULL,
  category      TEXT NOT NULL,
  address       TEXT,
  lat           REAL,
  lng           REAL,
  open_time     TEXT,
  close_day     TEXT,
  fee           TEXT,
  has_parking   INTEGER,
  tel           TEXT,
  source_api    TEXT,
  extra_json    TEXT,
  overview      TEXT,
  homepage      TEXT
);

CREATE INDEX IF NOT EXISTS idx_place_category ON place(category);
CREATE INDEX IF NOT EXISTS idx_place_latlng ON place(lat, lng);

CREATE TABLE IF NOT EXISTS event (
  event_id    TEXT PRIMARY KEY,
  name        TEXT NOT NULL,
  place_name  TEXT,
  address     TEXT,
  lat         REAL,
  lng         REAL,
  start_date  TEXT,
  end_date    TEXT,
  fee         TEXT,
  source_api  TEXT
);

CREATE INDEX IF NOT EXISTS idx_event_dates ON event(start_date, end_date);

CREATE TABLE IF NOT EXISTS parking (
  parking_id    TEXT PRIMARY KEY,
  name          TEXT,
  address       TEXT,
  lat           REAL,
  lng           REAL,
  operate_time  TEXT,
  fee           TEXT,
  capacity      INTEGER
);

CREATE INDEX IF NOT EXISTS idx_parking_latlng ON parking(lat, lng);

CREATE TABLE IF NOT EXISTS weather (
  location_key  TEXT,
  fetched_at    TEXT,
  temp          REAL,
  pop           INTEGER,
  pty           INTEGER,
  pm10          INTEGER,
  pm25          INTEGER,
  khai_grade    INTEGER,
  PRIMARY KEY (location_key, fetched_at)
);

CREATE TABLE IF NOT EXISTS transport (
  stop_id  TEXT PRIMARY KEY,
  name     TEXT,
  type     TEXT,
  lat      REAL,
  lng      REAL,
  routes   TEXT
);

CREATE INDEX IF NOT EXISTS idx_transport_latlng ON transport(lat, lng);

-- 버스 노선 메타정보 (TAGO BusRouteInfoInqireService/getRouteNoList)
CREATE TABLE IF NOT EXISTS bus_route (
  route_id    TEXT PRIMARY KEY,   -- TAGO routeid
  route_no    TEXT,               -- 노선번호(표시용, 예: "705")
  route_type  TEXT,               -- 마을버스/간선버스/급행버스/광역버스 등
  collected_at TEXT               -- TAGO에서 이 노선 정보를 수집한 시점(정확도 판단용)
);

-- 노선별 경유 정류소 순서 (동선/환승 탐색의 기반 데이터)
-- updowncd: TAGO가 제공하는 방향 구분(0/1, 편도순환 노선은 한쪽만 존재)
CREATE TABLE IF NOT EXISTS bus_route_stop (
  route_id    TEXT,
  updowncd    INTEGER,
  node_order  INTEGER,
  stop_id     TEXT,               -- transport.stop_id 참조
  PRIMARY KEY (route_id, updowncd, node_order)
);

CREATE INDEX IF NOT EXISTS idx_route_stop_stop ON bus_route_stop(stop_id);

CREATE TABLE IF NOT EXISTS medical (
  medical_id  TEXT PRIMARY KEY,
  name        TEXT NOT NULL,
  category    TEXT,
  address     TEXT,
  lat         REAL,
  lng         REAL,
  tel         TEXT,
  source_api  TEXT,
  extra_json  TEXT
);

CREATE INDEX IF NOT EXISTS idx_medical_latlng ON medical(lat, lng);

CREATE TABLE IF NOT EXISTS course_log (
  log_id                    INTEGER PRIMARY KEY AUTOINCREMENT,
  user_query                TEXT,
  extracted_conditions_json TEXT,
  recommended_places_json   TEXT,
  fallback_type             TEXT,   -- NULL(정상) / 'partial_filter' / 'full_rule_based'
  created_at                TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_course_log_fallback ON course_log(fallback_type);