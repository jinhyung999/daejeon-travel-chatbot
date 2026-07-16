"""Bounded bus/subway itinerary search over :mod:`transit_graph`."""

from __future__ import annotations

import math
import heapq
from datetime import datetime, timedelta
from itertools import count

import transit
from geo import haversine_km
from place_lookup import resolve_place
from transit_graph import get_transit_graph, subway_wait_minutes


MAX_NEARBY_KM = 1.0
NEAREST_PER_MODE = 8
MAX_BEAM_STATES = 400
MAX_COMPLETED_PATHS = 200
MAX_EXPANSIONS_PER_LAYER = 2000
MIN_EXPANSIONS_PER_MODE = 100
BUS_WAIT_MINUTES = 5.0
MAX_TRANSFER_DISTANCE_M = 600.0


def _nearby_nodes(graph, lat, lng):
    by_mode = {"bus": [], "subway": []}
    for node, (node_lat, node_lng) in graph.coords.items():
        mode = node.split(":", 1)[0]
        if mode not in by_mode:
            continue
        distance = haversine_km(lat, lng, node_lat, node_lng)
        if distance <= MAX_NEARBY_KM:
            by_mode[mode].append((distance, node))
    selected = []
    for mode in ("bus", "subway"):
        selected.extend(sorted(by_mode[mode], key=lambda item: (item[0], item[1]))[:NEAREST_PER_MODE])
    return selected


def _walk_minutes(graph, from_lat, from_lng, node):
    node_lat, node_lng = graph.coords[node]
    return transit._walk_minutes(from_lat, from_lng, node_lat, node_lng)


def _boarding_points(graph, node):
    yield node, 0.0
    valid_transfers = []
    for transfer in graph.transfer_adjacency.get(node, []):
        try:
            transfer_node = transfer["node"]
            distance = float(transfer["distance_m"])
            minutes = float(transfer["walking_minutes"])
        except (KeyError, TypeError, ValueError):
            continue
        if (
            not isinstance(transfer_node, str)
            or not math.isfinite(distance)
            or not math.isfinite(minutes)
            or distance < 0
            or distance > MAX_TRANSFER_DISTANCE_M
            or minutes < 0
        ):
            continue
        valid_transfers.append((minutes, distance, transfer_node))
    for minutes, _distance, transfer_node in sorted(valid_transfers):
        yield transfer_node, minutes


def _wait_minutes(graph, service, board_node, board_at):
    if service[0] == "bus":
        return BUS_WAIT_MINUTES, True
    station_id = board_node.split(":", 1)[1]
    return subway_wait_minutes(graph, station_id, service[2], board_at)


def _push_bounded(heap, limit, rank, serial, value):
    """Keep the smallest numeric ranks in a max-heap represented by negatives."""
    if limit <= 0:
        return
    item = (-rank[0], -rank[1], -serial, value)
    if len(heap) < limit:
        heapq.heappush(heap, item)
        return
    worst_rank = (-heap[0][0], -heap[0][1], -heap[0][2])
    if (rank[0], rank[1], serial) < worst_rank:
        heapq.heapreplace(heap, item)


def _heap_values_best_first(heap):
    return [
        item[3]
        for item in sorted(heap, key=lambda item: (-item[0], -item[1], -item[2]))
    ]


def _available_service_modes(graph, frontier):
    modes = set()
    for state in frontier:
        for board_node, _transfer_walk in _boarding_points(graph, state["node"]):
            for service, board_index in graph.node_services.get(board_node, []):
                sequence = graph.service_sequences.get(service, [])
                if (
                    service not in state["used"]
                    and board_index < len(sequence) - 1
                    and (service, board_index, board_index + 1) in graph.adjacent_minutes
                ):
                    modes.add(service[0])
    return modes


def _search(graph, origin, destination, max_legs, departure_at):
    starts = _nearby_nodes(graph, origin["lat"], origin["lng"])
    ends = {
        node: _walk_minutes(graph, destination["lat"], destination["lng"], node)
        for _distance, node in _nearby_nodes(graph, destination["lat"], destination["lng"])
    }
    if not starts or not ends:
        return []

    frontier = [
        {
            "node": node,
            "elapsed": _walk_minutes(graph, origin["lat"], origin["lng"], node),
            "access_walk": _walk_minutes(graph, origin["lat"], origin["lng"], node),
            "legs": [],
            "used": frozenset(),
        }
        for _distance, node in starts
    ]
    completed_heap = []
    tie_breaker = count()

    for _leg_number in range(max_legs):
        next_heap = []
        expansions = 0
        mode_expansions = {"bus": 0, "subway": 0}
        available_modes = _available_service_modes(graph, frontier)
        mode_limit = (
            MAX_EXPANSIONS_PER_LAYER - MIN_EXPANSIONS_PER_MODE
            if {"bus", "subway"} <= available_modes
            else MAX_EXPANSIONS_PER_LAYER
        )
        limit_reached = False
        for state in frontier:
            if limit_reached:
                break
            for board_node, transfer_walk in _boarding_points(graph, state["node"]):
                if limit_reached:
                    break
                memberships = sorted(
                    graph.node_services.get(board_node, []),
                    key=lambda item: (item[0], item[1]),
                )
                for service, board_index in memberships:
                    if limit_reached:
                        break
                    if service in state["used"]:
                        continue
                    sequence = graph.service_sequences.get(service, [])
                    if board_index >= len(sequence) - 1:
                        continue
                    board_elapsed = state["elapsed"] + transfer_walk
                    wait, wait_estimated = _wait_minutes(
                        graph,
                        service,
                        board_node,
                        departure_at + timedelta(minutes=board_elapsed),
                    )
                    try:
                        wait = float(wait)
                    except (TypeError, ValueError):
                        continue
                    if not math.isfinite(wait) or wait < 0:
                        continue

                    ride = 0.0
                    for alight_index in range(board_index + 1, len(sequence)):
                        if expansions >= MAX_EXPANSIONS_PER_LAYER:
                            limit_reached = True
                            break
                        if mode_expansions.get(service[0], 0) >= mode_limit:
                            break
                        edge = graph.adjacent_minutes.get((service, alight_index - 1, alight_index))
                        expansions += 1
                        mode_expansions[service[0]] = mode_expansions.get(service[0], 0) + 1
                        if edge is None:
                            break
                        try:
                            edge = float(edge)
                        except (TypeError, ValueError):
                            break
                        if not math.isfinite(edge) or edge < 0:
                            break
                        ride += edge
                        alight_node = sequence[alight_index]
                        leg = {
                            "service": service,
                            "board_node": board_node,
                            "board_index": board_index,
                            "alight_node": alight_node,
                            "alight_index": alight_index,
                            "wait_minutes": wait,
                            "wait_estimated": bool(wait_estimated),
                            "ride_minutes": ride,
                            "ride_estimated": service[0] == "bus",
                            "walk_transfer_minutes": transfer_walk,
                        }
                        elapsed = board_elapsed + wait + ride
                        legs = state["legs"] + [leg]
                        if alight_node in ends:
                            candidate = {
                                "legs": legs,
                                "access_walk": state["access_walk"],
                                "egress_walk": ends[alight_node],
                                "total": elapsed + ends[alight_node],
                            }
                            _push_bounded(
                                completed_heap,
                                MAX_COMPLETED_PATHS,
                                (candidate["total"], len(candidate["legs"])),
                                next(tie_breaker),
                                candidate,
                            )
                        if len(legs) < max_legs:
                            next_state = {
                                "node": alight_node,
                                "elapsed": elapsed,
                                "access_walk": state["access_walk"],
                                "legs": legs,
                                "used": state["used"] | {service},
                            }
                            _push_bounded(
                                next_heap,
                                MAX_BEAM_STATES,
                                (elapsed, len(legs)),
                                next(tie_breaker),
                                next_state,
                            )

        frontier = _heap_values_best_first(next_heap)
        if not frontier:
            break
    return _heap_values_best_first(completed_heap)


def _legacy_bus_leg(graph, leg):
    service = leg["service"]
    route_id, direction = service[1], int(service[2])
    ordered = graph.bus_graph.by_route.get((route_id, direction), [])
    if leg["board_index"] >= len(ordered) or leg["alight_index"] >= len(ordered):
        return None
    board_order, board_stop = ordered[leg["board_index"]]
    alight_order, alight_stop = ordered[leg["alight_index"]]
    return {
        "route_id": route_id,
        "updowncd": direction,
        "board_stop_id": board_stop,
        "board_order": board_order,
        "alight_stop_id": alight_stop,
        "alight_order": alight_order,
    }


def _refine_first_bus(graph, candidate):
    legs = candidate["legs"]
    if not legs or legs[0]["service"][0] != "bus":
        return
    legacy = _legacy_bus_leg(graph, legs[0])
    if legacy is None:
        return
    try:
        refined = transit._refine_legs_realtime(
            graph.bus_graph.by_route,
            graph.bus_graph.coords,
            [legacy],
            graph=graph.bus_graph,
        )
        values = refined[0]
        wait = float(values["wait_minutes"])
        ride = float(values["ride_minutes"])
        if not all(math.isfinite(value) and value >= 0 for value in (wait, ride)):
            return
    except (IndexError, KeyError, TypeError, ValueError):
        return
    legs[0]["wait_minutes"] = wait
    legs[0]["ride_minutes"] = ride
    legs[0]["wait_estimated"] = bool(values.get("wait_estimated", legs[0]["wait_estimated"]))
    legs[0]["ride_estimated"] = bool(values.get("ride_estimated", legs[0]["ride_estimated"]))


def _dedupe(candidates):
    chosen = {}
    for candidate in candidates:
        key = tuple(
            (leg["service"][0], leg["service"][1], leg["service"][2], leg["board_node"], leg["alight_node"])
            for leg in candidate["legs"]
        )
        if key not in chosen or candidate["total"] < chosen[key]["total"]:
            chosen[key] = candidate
    return list(chosen.values())


def _route_totals(candidate):
    legs = candidate["legs"]
    total_wait = sum(leg["wait_minutes"] for leg in legs)
    total_ride = sum(leg["ride_minutes"] for leg in legs)
    total_transfer_walk = sum(leg["walk_transfer_minutes"] for leg in legs)
    total_walk = candidate["access_walk"] + total_transfer_walk + candidate["egress_walk"]
    return total_walk, total_wait, total_ride, total_walk + total_wait + total_ride


def _leg_output(graph, leg):
    mode, service_id, direction = leg["service"]
    output = {
        "mode": mode,
        "service_id": service_id,
        "direction": direction,
        "board_node_id": leg["board_node"],
        "board_name": graph.names.get(leg["board_node"], leg["board_node"]),
        "alight_node_id": leg["alight_node"],
        "alight_name": graph.names.get(leg["alight_node"], leg["alight_node"]),
        "wait_minutes": round(leg["wait_minutes"], 1),
        "wait_estimated": leg["wait_estimated"],
        "ride_minutes": round(leg["ride_minutes"], 1),
        "ride_estimated": leg["ride_estimated"],
        "walk_transfer_minutes": round(leg["walk_transfer_minutes"], 2),
    }
    meta = graph.service_meta.get(leg["service"], {})
    if mode == "bus":
        output.update({
            "route_id": service_id,
            "route_no": meta.get("route_no") or service_id,
            "route_type": meta.get("route_type"),
        })
    else:
        output.update({
            "line_id": service_id,
            "line_name_ko": meta.get("name_ko"),
            "line_name_en": meta.get("name_en"),
        })
    return output


def recommend_transit_routes(
    from_place: str,
    to_place: str,
    max_legs: int = 3,
    max_results: int = 3,
    departure_at: datetime | None = None,
) -> dict:
    origin = resolve_place(from_place)
    if origin is None:
        return {"error": "place_not_found", "query": from_place}
    destination = resolve_place(to_place)
    if destination is None:
        return {"error": "place_not_found", "query": to_place}

    max_legs = min(3, max(1, int(max_legs)))
    max_results = max(0, int(max_results))
    departure_at = departure_at or datetime.now()
    calculated_at = datetime.now().isoformat(timespec="seconds")
    departure_text = departure_at.isoformat(timespec="seconds")
    place_from = {key: origin[key] for key in ("name", "lat", "lng")}
    place_to = {key: destination[key] for key in ("name", "lat", "lng")}
    graph = get_transit_graph()
    candidates = _dedupe(_search(graph, origin, destination, max_legs, departure_at))
    candidates.sort(key=lambda candidate: (
        candidate["total"], len(candidate["legs"]),
        candidate["access_walk"] + candidate["egress_walk"],
    ))

    shortlist = candidates[:max(15, max_results * 5)]
    for candidate in shortlist:
        _refine_first_bus(graph, candidate)
    for candidate in shortlist:
        total_walk, total_wait, total_ride, total = _route_totals(candidate)
        candidate["total_walk"] = total_walk
        candidate["total_wait"] = total_wait
        candidate["total_ride"] = total_ride
        candidate["total"] = total
    shortlist.sort(key=lambda candidate: (
        candidate["total"], len(candidate["legs"]) - 1, candidate["total_walk"]
    ))

    routes = []
    for candidate in shortlist[:max_results]:
        routes.append({
            "total_minutes": round(candidate["total"], 1),
            "transfer_count": len(candidate["legs"]) - 1,
            "total_walk_minutes": round(candidate["total_walk"], 1),
            "total_wait_minutes": round(candidate["total_wait"], 1),
            "total_ride_minutes": round(candidate["total_ride"], 1),
            "access_walk_minutes": round(candidate["access_walk"], 1),
            "egress_walk_minutes": round(candidate["egress_walk"], 1),
            "legs": [_leg_output(graph, leg) for leg in candidate["legs"]],
        })

    result = {
        "from_place": place_from,
        "to_place": place_to,
        "calculated_at": calculated_at,
        "departure_at": departure_text,
        "routes": routes,
    }
    if not routes:
        result["reason"] = "no_route_found"
    return result
