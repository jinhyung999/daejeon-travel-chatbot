"""Cached multimodal transit graph built from the local SQLite snapshot."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache

from bus_graph import BusGraph, DB_PATH
from geo import CAR_SPEED_KMH, haversine_km


ServiceKey = tuple[str, str, str]
Transfer = dict[str, object]


@dataclass
class TransitGraph:
    service_sequences: dict[ServiceKey, list[str]]
    adjacent_minutes: dict[tuple[ServiceKey, int, int], float]
    node_services: dict[str, list[tuple[ServiceKey, int]]]
    coords: dict[str, tuple[float, float]]
    names: dict[str, str]
    service_meta: dict[ServiceKey, dict[str, str | None]]
    transfer_adjacency: dict[str, list[Transfer]]
    schedules: dict[tuple[str, str, str], list[str]]
    bus_graph: BusGraph


@lru_cache(maxsize=4)
def get_transit_graph(db_path=DB_PATH) -> TransitGraph:
    connection = sqlite3.connect(db_path)
    try:
        cursor = connection.cursor()
        bus_by_route: dict[tuple[str, int], list[tuple[int, str]]] = {}
        bus_by_stop: dict[str, list[tuple[str, int, int]]] = {}
        for route_id, updowncd, node_order, stop_id in cursor.execute(
            "SELECT route_id, updowncd, node_order, stop_id "
            "FROM bus_route_stop ORDER BY route_id, updowncd, node_order"
        ):
            bus_by_route.setdefault((route_id, updowncd), []).append((node_order, stop_id))
            bus_by_stop.setdefault(stop_id, []).append((route_id, updowncd, node_order))

        bus_coords: dict[str, tuple[float, float]] = {}
        bus_names: dict[str, str] = {}
        for stop_id, name, lat, lng in cursor.execute(
            "SELECT stop_id, name, lat, lng FROM transport"
        ):
            bus_names[stop_id] = name
            if lat is not None and lng is not None:
                bus_coords[stop_id] = (lat, lng)

        bus_route_meta = {
            route_id: (route_no, route_type)
            for route_id, route_no, route_type in cursor.execute(
                "SELECT route_id, route_no, route_type FROM bus_route"
            )
        }
        bus_edge_minutes = {}
        for (route_id, updowncd), stops in bus_by_route.items():
            for (left_order, left_stop), (right_order, right_stop) in zip(stops, stops[1:]):
                if left_stop in bus_coords and right_stop in bus_coords:
                    bus_edge_minutes[(route_id, updowncd, left_order, right_order)] = (
                        haversine_km(*bus_coords[left_stop], *bus_coords[right_stop])
                        / CAR_SPEED_KMH
                        * 60
                    )
        bus_graph = BusGraph(
            bus_by_route,
            bus_by_stop,
            bus_coords,
            bus_names,
            bus_route_meta,
            bus_edge_minutes,
        )

        service_sequences: dict[ServiceKey, list[str]] = {}
        adjacent_minutes: dict[tuple[ServiceKey, int, int], float] = {}
        node_services: dict[str, list[tuple[ServiceKey, int]]] = {}
        coords = {f"bus:{stop_id}": value for stop_id, value in bus_coords.items()}
        names = {f"bus:{stop_id}": value for stop_id, value in bus_names.items()}
        service_meta: dict[ServiceKey, dict[str, str | None]] = {}

        for (route_id, updowncd), stops in bus_by_route.items():
            service = ("bus", route_id, str(updowncd))
            sequence = [f"bus:{stop_id}" for _order, stop_id in stops]
            service_sequences[service] = sequence
            route_no, route_type = bus_route_meta.get(route_id, (None, None))
            service_meta[service] = {"route_no": route_no, "route_type": route_type}
            for index, node in enumerate(sequence):
                node_services.setdefault(node, []).append((service, index))
            for index, ((left_order, _), (right_order, _)) in enumerate(zip(stops, stops[1:])):
                minutes = bus_edge_minutes.get((route_id, updowncd, left_order, right_order))
                if minutes is not None:
                    adjacent_minutes[(service, index, index + 1)] = minutes

        lines = {
            line_id: (name_ko, name_en)
            for line_id, name_ko, name_en in cursor.execute(
                "SELECT line_id, name_ko, name_en FROM subway_line"
            )
        }
        stations_by_line: dict[str, list[str]] = {}
        for station_id, line_id, _station_no, name_ko, _name_en, lat, lng in cursor.execute(
            "SELECT station_id, line_id, station_no, name_ko, name_en, lat, lng "
            "FROM subway_station ORDER BY line_id, station_no"
        ):
            stations_by_line.setdefault(line_id, []).append(station_id)
            node = f"subway:{station_id}"
            names[node] = name_ko
            coords[node] = (lat, lng)

        edge_seconds = {
            (line_id, from_station, to_station): travel_seconds
            for line_id, from_station, to_station, travel_seconds in cursor.execute(
                "SELECT line_id, from_station_id, to_station_id, travel_seconds "
                "FROM subway_edge ORDER BY line_id, sequence"
            )
        }
        for line_id, station_ids in stations_by_line.items():
            line_name_ko, line_name_en = lines[line_id]
            line_meta = {"name_ko": line_name_ko, "name_en": line_name_en}
            for direction, ordered_ids in (("up", station_ids), ("down", list(reversed(station_ids)))):
                service = ("subway", line_id, direction)
                sequence = [f"subway:{station_id}" for station_id in ordered_ids]
                service_sequences[service] = sequence
                service_meta[service] = dict(line_meta)
                for index, node in enumerate(sequence):
                    node_services.setdefault(node, []).append((service, index))
                for index, (left, right) in enumerate(zip(ordered_ids, ordered_ids[1:])):
                    seconds = edge_seconds.get((line_id, left, right))
                    if seconds is None:
                        seconds = edge_seconds.get((line_id, right, left))
                    if seconds is not None:
                        adjacent_minutes[(service, index, index + 1)] = seconds / 60.0

        transfer_adjacency: dict[str, list[Transfer]] = {}
        for station_id, stop_id, distance_m, walking_minutes in cursor.execute(
            "SELECT station_id, stop_id, distance_m, walking_minutes FROM transit_transfer"
        ):
            subway_node = f"subway:{station_id}"
            bus_node = f"bus:{stop_id}"
            transfer_adjacency.setdefault(bus_node, []).append(
                {"node": subway_node, "distance_m": distance_m, "walking_minutes": walking_minutes}
            )
            transfer_adjacency.setdefault(subway_node, []).append(
                {"node": bus_node, "distance_m": distance_m, "walking_minutes": walking_minutes}
            )

        schedules: dict[tuple[str, str, str], list[str]] = {}
        for station_id, day_type, direction, departure_time in cursor.execute(
            "SELECT station_id, day_type, direction, departure_time FROM subway_schedule "
            "ORDER BY station_id, day_type, direction, departure_time"
        ):
            schedules.setdefault((station_id, day_type, direction), []).append(departure_time)

        return TransitGraph(
            service_sequences,
            adjacent_minutes,
            node_services,
            coords,
            names,
            service_meta,
            transfer_adjacency,
            schedules,
            bus_graph,
        )
    finally:
        connection.close()


def clear_transit_graph_cache() -> None:
    get_transit_graph.cache_clear()


def subway_wait_minutes(
    graph: TransitGraph,
    station_id: str,
    direction: str,
    board_at: datetime,
) -> tuple[float, bool]:
    day_type = "01" if board_at.weekday() < 5 else "02" if board_at.weekday() == 5 else "03"
    board_seconds = board_at.hour * 3600 + board_at.minute * 60 + board_at.second
    for departure in graph.schedules.get((station_id, day_type, direction), []):
        if (
            not isinstance(departure, str)
            or len(departure) != 6
            or not departure.isascii()
            or not departure.isdigit()
        ):
            continue
        hour = int(departure[:2])
        minute = int(departure[2:4])
        second = int(departure[4:6])
        if hour > 23 or minute > 59 or second > 59:
            continue
        departure_seconds = hour * 3600 + minute * 60 + second
        if departure_seconds >= board_seconds:
            return ((departure_seconds - board_seconds) / 60.0, False)
    return (5.0, True)
