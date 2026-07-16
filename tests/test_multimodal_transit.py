import io
import json
import runpy
import sys
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest.mock import patch


APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))

import multimodal_transit
import transit
from bus_graph import BusGraph
from transit_graph import TransitGraph


def make_graph(services, coords, transfers=(), schedules=None):
    service_sequences = {}
    adjacent_minutes = {}
    node_services = {}
    service_meta = {}
    bus_by_route = {}
    bus_by_stop = {}
    bus_coords = {}
    bus_names = {}
    bus_route_meta = {}
    bus_edge_minutes = {}

    for service, nodes, edge_minutes, meta in services:
        service_sequences[service] = nodes
        service_meta[service] = meta
        for index, node in enumerate(nodes):
            node_services.setdefault(node, []).append((service, index))
        for index, minutes in enumerate(edge_minutes):
            adjacent_minutes[(service, index, index + 1)] = minutes

        if service[0] == "bus":
            route_id, direction = service[1], int(service[2])
            raw_nodes = [node.removeprefix("bus:") for node in nodes]
            bus_by_route[(route_id, direction)] = list(enumerate(raw_nodes, 1))
            bus_route_meta[route_id] = (meta["route_no"], meta.get("route_type"))
            for order, stop_id in enumerate(raw_nodes, 1):
                bus_by_stop.setdefault(stop_id, []).append((route_id, direction, order))
                bus_coords[stop_id] = coords[f"bus:{stop_id}"]
                bus_names[stop_id] = stop_id
            for order, minutes in enumerate(edge_minutes, 1):
                bus_edge_minutes[(route_id, direction, order, order + 1)] = minutes

    names = {node: node.split(":", 1)[1] for node in coords}
    transfer_adjacency = {}
    for left, right, distance_m, walking_minutes in transfers:
        transfer_adjacency.setdefault(left, []).append(
            {"node": right, "distance_m": distance_m, "walking_minutes": walking_minutes}
        )
        transfer_adjacency.setdefault(right, []).append(
            {"node": left, "distance_m": distance_m, "walking_minutes": walking_minutes}
        )

    bus_graph = BusGraph(
        bus_by_route, bus_by_stop, bus_coords, bus_names, bus_route_meta, bus_edge_minutes
    )
    return TransitGraph(
        service_sequences,
        adjacent_minutes,
        node_services,
        coords,
        names,
        service_meta,
        transfer_adjacency,
        schedules or {},
        bus_graph,
    )


SUBWAY_META = {"name_ko": "대전 1호선", "name_en": "Daejeon Line 1"}
BUS_META = {"route_no": "101", "route_type": "간선버스"}


class MultimodalTransitTest(unittest.TestCase):
    def recommend(self, graph, origin=(36.35, 127.38), destination=(36.35, 127.39), **kwargs):
        places = {
            "출발": {"name": "출발", "lat": origin[0], "lng": origin[1]},
            "도착": {"name": "도착", "lat": destination[0], "lng": destination[1]},
        }
        with patch.object(multimodal_transit, "resolve_place", side_effect=places.get), patch.object(
            multimodal_transit, "get_transit_graph", return_value=graph
        ):
            return multimodal_transit.recommend_transit_routes(
                "출발", "도착", departure_at=datetime(2026, 7, 16, 8, 0), **kwargs
            )

    def test_subway_only_uses_official_edges_and_five_minute_wait(self):
        graph = make_graph(
            [(('subway', 'L1', 'up'), ['subway:A', 'subway:B'], [4.0], SUBWAY_META)],
            {'subway:A': (36.35, 127.38), 'subway:B': (36.35, 127.39)},
        )

        result = self.recommend(graph)

        route = result["routes"][0]
        self.assertEqual(9.0, route["total_minutes"])
        self.assertEqual(5.0, route["total_wait_minutes"])
        self.assertEqual(4.0, route["total_ride_minutes"])
        self.assertEqual("subway", route["legs"][0]["mode"])

    def test_faster_subway_first_route_ranks_before_bus(self):
        graph = make_graph(
            [
                (('bus', 'R1', '0'), ['bus:X', 'bus:Y'], [8.0], BUS_META),
                (('subway', 'L1', 'up'), ['subway:A', 'subway:B'], [2.0], SUBWAY_META),
            ],
            {
                'bus:X': (36.35, 127.38), 'bus:Y': (36.35, 127.39),
                'subway:A': (36.35, 127.38), 'subway:B': (36.35, 127.39),
            },
        )

        with patch.object(transit, "get_arrival_info", return_value=None), patch.object(
            transit, "get_route_vehicle_locations", return_value=[]
        ):
            result = self.recommend(graph, max_results=2)

        self.assertEqual("subway", result["routes"][0]["legs"][0]["mode"])
        self.assertEqual("bus", result["routes"][1]["legs"][0]["mode"])

    def test_three_legs_use_two_stored_transfers_and_never_expand_a_fourth(self):
        graph = make_graph(
            [
                (('subway', 'L1', 'up'), ['subway:A', 'subway:B'], [1.0], SUBWAY_META),
                (('bus', 'R1', '0'), ['bus:C', 'bus:D'], [1.0], BUS_META),
                (('subway', 'L2', 'up'), ['subway:E', 'subway:F'], [1.0], SUBWAY_META),
                (('bus', 'R2', '0'), ['bus:G', 'bus:H'], [1.0], BUS_META),
            ],
            {
                'subway:A': (36.35, 127.380), 'subway:B': (36.35, 127.395),
                'bus:C': (36.35, 127.395), 'bus:D': (36.35, 127.410),
                'subway:E': (36.35, 127.410), 'subway:F': (36.35, 127.425),
                'bus:G': (36.35, 127.425), 'bus:H': (36.35, 127.440),
            },
            transfers=[
                ('subway:B', 'bus:C', 100, 2.25),
                ('bus:D', 'subway:E', 150, 3.5),
                ('subway:F', 'bus:G', 100, 2.0),
            ],
        )

        result = self.recommend(graph, destination=(36.35, 127.425))

        route = result["routes"][0]
        self.assertEqual(3, len(route["legs"]))
        self.assertEqual(2, route["transfer_count"])
        self.assertEqual([0.0, 2.25, 3.5], [leg["walk_transfer_minutes"] for leg in route["legs"]])
        no_fourth = self.recommend(graph, destination=(36.35, 127.440))
        self.assertEqual([], no_fourth["routes"])

    def test_deduplicates_identical_itineraries(self):
        graph = make_graph(
            [(('subway', 'L1', 'up'), ['subway:A', 'subway:B'], [2.0], SUBWAY_META)],
            {'subway:A': (36.35, 127.38), 'subway:B': (36.35, 127.39)},
        )
        graph.node_services['subway:A'].append((('subway', 'L1', 'up'), 0))

        result = self.recommend(graph, max_results=3)

        self.assertEqual(1, len(result["routes"]))

    def test_output_fields_and_no_route_contract(self):
        graph = make_graph(
            [(('subway', 'L1', 'up'), ['subway:A', 'subway:B'], [2.0], SUBWAY_META)],
            {'subway:A': (36.35, 127.38), 'subway:B': (36.35, 127.39)},
        )
        result = self.recommend(graph)
        route = result["routes"][0]
        leg = route["legs"][0]

        self.assertTrue({"from_place", "to_place", "calculated_at", "departure_at", "routes"} <= result.keys())
        self.assertTrue({
            "total_minutes", "transfer_count", "total_walk_minutes", "total_wait_minutes",
            "total_ride_minutes", "access_walk_minutes", "egress_walk_minutes", "legs",
        } <= route.keys())
        self.assertTrue({
            "mode", "service_id", "direction", "board_node_id", "board_name",
            "alight_node_id", "alight_name", "wait_minutes", "wait_estimated",
            "ride_minutes", "ride_estimated", "walk_transfer_minutes", "line_id",
            "line_name_ko", "line_name_en",
        } <= leg.keys())

        disconnected = make_graph(
            [(('subway', 'L1', 'up'), ['subway:A', 'subway:B'], [2.0], SUBWAY_META)],
            {'subway:A': (36.35, 127.38), 'subway:B': (36.35, 127.395), 'subway:Z': (36.35, 127.410)},
        )
        no_route = self.recommend(disconnected, destination=(36.35, 127.410))
        self.assertEqual([], no_route["routes"])
        self.assertEqual("no_route_found", no_route["reason"])

    def test_first_bus_leg_alone_uses_legacy_realtime_values_in_total(self):
        graph = make_graph(
            [
                (('bus', 'R1', '0'), ['bus:A', 'bus:B'], [4.0], BUS_META),
                (('subway', 'L1', 'up'), ['subway:C', 'subway:D'], [3.0], SUBWAY_META),
            ],
            {
                'bus:A': (36.35, 127.380), 'bus:B': (36.35, 127.395),
                'subway:C': (36.35, 127.395), 'subway:D': (36.35, 127.410),
            },
            transfers=[('bus:B', 'subway:C', 75, 1.0)],
        )

        def refined(_by_route, _coords, legs, graph=None):
            self.assertEqual(1, len(legs))
            self.assertEqual("R1", legs[0]["route_id"])
            return [{**legs[0], "wait_minutes": 1.5, "wait_estimated": False,
                     "ride_minutes": 2.5, "ride_estimated": True}]

        with patch.object(transit, "_refine_legs_realtime", side_effect=refined) as refine:
            result = self.recommend(graph, destination=(36.35, 127.410))

        route = result["routes"][0]
        self.assertEqual(1, refine.call_count)
        self.assertEqual(1.5, route["legs"][0]["wait_minutes"])
        self.assertEqual(2.5, route["legs"][0]["ride_minutes"])
        self.assertEqual(5.0, route["legs"][1]["wait_minutes"])
        self.assertEqual(13.0, route["total_minutes"])

    def test_wrapper_is_lazy_and_cli_calls_multimodal_recommendation(self):
        with patch.object(multimodal_transit, "recommend_transit_routes", return_value={"routes": []}) as recommend:
            self.assertEqual({"routes": []}, transit.recommend_transit_routes("A", "B"))
        recommend.assert_called_once_with("A", "B", max_legs=3, max_results=3, departure_at=None)

        transit_path = APP_DIR / "transit.py"
        with patch.object(multimodal_transit, "recommend_transit_routes", return_value={"routes": []}) as recommend, \
             patch.object(sys, "argv", [str(transit_path), "A", "B"]), redirect_stdout(io.StringIO()) as output:
            runpy.run_path(str(transit_path), run_name="__main__")
        recommend.assert_called_once()
        self.assertEqual({"routes": []}, json.loads(output.getvalue()))


if __name__ == "__main__":
    unittest.main()
