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
  homepage      TEXT,
  recommend     TEXT,
  concept_tag   TEXT,
  photo_spot    INTEGER,
  has_workshop  INTEGER,
  blog_url_1    TEXT,
  blog_url_2    TEXT,
  blog_url_3    TEXT,
  signature_menu TEXT
);

CREATE INDEX IF NOT EXISTS idx_place_category ON place(category);
CREATE INDEX IF NOT EXISTS idx_place_latlng ON place(lat, lng);
CREATE INDEX IF NOT EXISTS idx_place_category_recommend
ON place(category, recommend);

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
  stop_id    TEXT PRIMARY KEY,     -- TAGO nodeId (예: DJB8005621)
  name       TEXT NOT NULL,
  type       TEXT NOT NULL DEFAULT 'bus',
  lat        REAL NOT NULL,
  lng        REAL NOT NULL,
  routes     TEXT,                 -- bus_route_stop에서 파생한 표시용 캐시
  source_api TEXT NOT NULL DEFAULT 'tago' CHECK (source_api = 'tago')
);

CREATE INDEX IF NOT EXISTS idx_transport_latlng ON transport(lat, lng);

-- 버스 노선 메타정보 (TAGO BusRouteInfoInqireService/getRouteNoList)
CREATE TABLE IF NOT EXISTS bus_route (
  route_id    TEXT PRIMARY KEY,   -- TAGO routeid
  route_no    TEXT NOT NULL,      -- 노선번호(표시용, 예: "705")
  route_type  TEXT,               -- 마을버스/간선버스/급행버스/광역버스 등
  collected_at TEXT NOT NULL      -- TAGO에서 이 노선 정보를 수집한 시점(정확도 판단용)
);

-- 노선별 경유 정류소 순서 (동선/환승 탐색의 기반 데이터)
-- updowncd: TAGO가 제공하는 방향 구분(0/1, 편도순환 노선은 한쪽만 존재)
CREATE TABLE IF NOT EXISTS bus_route_stop (
  route_id    TEXT NOT NULL REFERENCES bus_route(route_id) ON DELETE CASCADE,
  updowncd    INTEGER NOT NULL,
  node_order  INTEGER NOT NULL,
  stop_id     TEXT NOT NULL REFERENCES transport(stop_id),
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

CREATE TABLE IF NOT EXISTS subway_line (
  line_id TEXT PRIMARY KEY,
  name_ko TEXT NOT NULL,
  name_en TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS subway_station (
  station_id TEXT PRIMARY KEY,
  line_id TEXT NOT NULL REFERENCES subway_line(line_id) ON DELETE CASCADE,
  station_no INTEGER NOT NULL UNIQUE CHECK (station_no BETWEEN 101 AND 122),
  name_ko TEXT NOT NULL,
  name_en TEXT NOT NULL,
  address TEXT,
  lat REAL NOT NULL CHECK (lat BETWEEN -90 AND 90),
  lng REAL NOT NULL CHECK (lng BETWEEN -180 AND 180),
  coordinate_source TEXT NOT NULL CHECK (coordinate_source = 'derived_bus_stops')
);

CREATE INDEX IF NOT EXISTS idx_subway_station_line_no ON subway_station(line_id, station_no);
CREATE INDEX IF NOT EXISTS idx_subway_station_latlng ON subway_station(lat, lng);

CREATE TABLE IF NOT EXISTS subway_edge (
  line_id TEXT NOT NULL REFERENCES subway_line(line_id) ON DELETE CASCADE,
  sequence INTEGER NOT NULL CHECK (sequence BETWEEN 1 AND 21),
  from_station_id TEXT NOT NULL REFERENCES subway_station(station_id),
  to_station_id TEXT NOT NULL REFERENCES subway_station(station_id),
  travel_seconds INTEGER NOT NULL CHECK (travel_seconds > 0),
  distance_km REAL NOT NULL CHECK (distance_km > 0),
  PRIMARY KEY (line_id, sequence),
  UNIQUE (line_id, from_station_id, to_station_id),
  CHECK (from_station_id <> to_station_id)
);

CREATE INDEX IF NOT EXISTS idx_subway_edge_from ON subway_edge(from_station_id);
CREATE INDEX IF NOT EXISTS idx_subway_edge_to ON subway_edge(to_station_id);

CREATE TABLE IF NOT EXISTS subway_schedule (
  station_id TEXT NOT NULL REFERENCES subway_station(station_id) ON DELETE CASCADE,
  day_type TEXT NOT NULL CHECK (day_type IN ('01', '02', '03')),
  direction TEXT NOT NULL CHECK (direction IN ('up', 'down')),
  train_no TEXT NOT NULL,
  arrival_time TEXT CHECK (
    arrival_time IS NULL OR (length(arrival_time) = 6 AND arrival_time NOT GLOB '*[^0-9]*')
  ),
  departure_time TEXT NOT NULL CHECK (
    length(departure_time) = 6 AND departure_time NOT GLOB '*[^0-9]*'
  ),
  PRIMARY KEY (station_id, day_type, direction, train_no, departure_time)
);

CREATE INDEX IF NOT EXISTS idx_subway_schedule_lookup
  ON subway_schedule(station_id, day_type, direction, departure_time);

CREATE TABLE IF NOT EXISTS transit_transfer (
  station_id TEXT NOT NULL REFERENCES subway_station(station_id) ON DELETE CASCADE,
  stop_id TEXT NOT NULL REFERENCES transport(stop_id) ON DELETE CASCADE,
  distance_m REAL NOT NULL CHECK (distance_m >= 0 AND distance_m <= 600),
  walking_minutes REAL NOT NULL CHECK (walking_minutes >= 0),
  PRIMARY KEY (station_id, stop_id)
);

CREATE INDEX IF NOT EXISTS idx_transit_transfer_stop ON transit_transfer(stop_id);
