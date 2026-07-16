import sqlite3
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from geo import CAR_SPEED_KMH, haversine_km


DB_PATH = Path(__file__).parent.parent / "db" / "travel.db"


@dataclass
class BusGraph:
    by_route: dict
    by_stop: dict
    coords: dict
    stop_names: dict
    route_meta: dict
    edge_minutes: dict


@lru_cache(maxsize=4)
def get_bus_graph(db_path=DB_PATH):
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        by_route = {}
        by_stop = {}
        for route_id, updowncd, node_order, stop_id in cur.execute(
            "SELECT route_id, updowncd, node_order, stop_id "
            "FROM bus_route_stop ORDER BY route_id, updowncd, node_order"
        ):
            by_route.setdefault((route_id, updowncd), []).append((node_order, stop_id))
            by_stop.setdefault(stop_id, []).append((route_id, updowncd, node_order))

        coords = {}
        stop_names = {}
        for stop_id, name, lat, lng in cur.execute(
            "SELECT stop_id, name, lat, lng FROM transport"
        ):
            stop_names[stop_id] = name
            if lat is not None and lng is not None:
                coords[stop_id] = (lat, lng)

        route_meta = {
            route_id: (route_no, route_type)
            for route_id, route_no, route_type in cur.execute(
                "SELECT route_id, route_no, route_type FROM bus_route"
            )
        }

        edge_minutes = {}
        for (route_id, updowncd), stops in by_route.items():
            for (from_order, from_stop), (to_order, to_stop) in zip(stops, stops[1:]):
                if from_stop in coords and to_stop in coords:
                    minutes = haversine_km(*coords[from_stop], *coords[to_stop]) / CAR_SPEED_KMH * 60
                    edge_minutes[(route_id, updowncd, from_order, to_order)] = minutes

        return BusGraph(by_route, by_stop, coords, stop_names, route_meta, edge_minutes)
    finally:
        conn.close()


def clear_bus_graph_cache():
    get_bus_graph.cache_clear()


def static_segment_minutes(graph, route_id, updowncd, from_order, to_order):
    stops = graph.by_route[(route_id, updowncd)]
    orders = [order for order, _stop_id in stops if from_order <= order <= to_order]
    return sum(
        graph.edge_minutes.get((route_id, updowncd, left, right), 0.0)
        for left, right in zip(orders, orders[1:])
    )
