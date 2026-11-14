"""Unit tests for objective model, decision algorithm, hysteresis, and anti-ping-pong."""

import copy
import inspect
import json
import math
import unittest

try:
    from decide import (
        DEFAULT_HYSTERESIS_THRESHOLD,
        PingPongTracker,
        count_ping_pong,
        decide_proposed,
        simulate_decisions,
    )
    from interfaces import (
        Action,
        DecisionResult,
        NetworkMetrics,
        StrategyType,
        decision_function_protocol,
        get_mock_green_forecast,
        get_mock_network_metrics,
    )
    from objective import (
        compute_break_even_workload_mb,
        compute_green_gain_kw,
        compute_network_cost,
        compute_objective,
        estimate_transmission_delay_s,
        merge_objective_params,
        min_max_normalize,
        normalize_green_gain,
    )
except ImportError:
    from src.decide import (
        DEFAULT_HYSTERESIS_THRESHOLD,
        PingPongTracker,
        count_ping_pong,
        decide_proposed,
        simulate_decisions,
    )
    from src.interfaces import (
        Action,
        DecisionResult,
        NetworkMetrics,
        StrategyType,
        decision_function_protocol,
        get_mock_green_forecast,
        get_mock_network_metrics,
    )
    from src.objective import (
        compute_break_even_workload_mb,
        compute_green_gain_kw,
        compute_network_cost,
        compute_objective,
        estimate_transmission_delay_s,
        merge_objective_params,
        min_max_normalize,
        normalize_green_gain,
    )


def make_good_metrics(dcs=("dc1", "dc2", "dc3")):
    """Tạo bộ NetworkMetrics điều kiện mạng tốt đồng nhất cho danh sách DC."""
    return {
        dc: NetworkMetrics(
            target_dc=dc,
            rtt_ms=1.0,
            throughput_mbps=1000.0,
            packet_loss_rate=0.0,
            queue_delay_ms=0.0,
        )
        for dc in dcs
    }


class TestObjectiveFunction(unittest.TestCase):
    def test_transmission_delay_formula(self):
        self.assertAlmostEqual(estimate_transmission_delay_s(100.0, 100.0), 8.0, places=6)
        self.assertAlmostEqual(estimate_transmission_delay_s(1.0, 8.0), 1.0, places=6)

    def test_transmission_delay_zero_throughput(self):
        self.assertEqual(estimate_transmission_delay_s(100.0, 0.0), math.inf)

    def test_transmission_delay_negative_workload(self):
        with self.assertRaises(ValueError):
            estimate_transmission_delay_s(-1.0, 100.0)

    def test_min_max_normalize_and_clip(self):
        self.assertAlmostEqual(min_max_normalize(50.0, 0.0, 100.0), 0.5, places=6)
        self.assertAlmostEqual(min_max_normalize(-5.0, 0.0, 100.0), 0.0, places=6)
        self.assertAlmostEqual(min_max_normalize(150.0, 0.0, 100.0), 1.0, places=6)
        with self.assertRaises(ValueError):
            min_max_normalize(1.0, 1.0, 1.0)

    def test_green_gain_sign_and_norm(self):
        green = get_mock_green_forecast()
        gg_dc2 = compute_green_gain_kw("dc1", "dc2", green)
        gg_dc3 = compute_green_gain_kw("dc1", "dc3", green)
        self.assertAlmostEqual(gg_dc2, 50.0, places=6)
        self.assertAlmostEqual(normalize_green_gain(gg_dc2), 0.5, places=6)
        self.assertAlmostEqual(gg_dc3, -15.0, places=6)
        self.assertAlmostEqual(normalize_green_gain(gg_dc3), -0.15, places=6)

    def test_network_cost_mock_values(self):
        net = get_mock_network_metrics()
        nc_dc2 = compute_network_cost(100.0, net["dc2"])
        nc_dc3 = compute_network_cost(100.0, net["dc3"])
        self.assertTrue(nc_dc2["feasible"])
        self.assertTrue(nc_dc3["feasible"])
        self.assertAlmostEqual(nc_dc2["network_cost_norm"], 0.1018167, places=6)
        self.assertAlmostEqual(nc_dc3["network_cost_norm"], 0.0303833, places=6)

    def test_network_cost_monotonic(self):
        base = NetworkMetrics(
            target_dc="dc2",
            rtt_ms=20.0,
            throughput_mbps=100.0,
            packet_loss_rate=0.5,
            queue_delay_ms=5.0,
        )
        base_nc = compute_network_cost(100.0, base)["network_cost_norm"]

        higher_rtt = NetworkMetrics("dc2", rtt_ms=80.0, throughput_mbps=100.0, packet_loss_rate=0.5, queue_delay_ms=5.0)
        higher_loss = NetworkMetrics("dc2", rtt_ms=20.0, throughput_mbps=100.0, packet_loss_rate=3.0, queue_delay_ms=5.0)
        lower_thr = NetworkMetrics("dc2", rtt_ms=20.0, throughput_mbps=20.0, packet_loss_rate=0.5, queue_delay_ms=5.0)

        self.assertGreaterEqual(compute_network_cost(100.0, higher_rtt)["network_cost_norm"], base_nc)
        self.assertGreaterEqual(compute_network_cost(100.0, higher_loss)["network_cost_norm"], base_nc)
        self.assertGreaterEqual(compute_network_cost(100.0, lower_thr)["network_cost_norm"], base_nc)
        self.assertGreaterEqual(compute_network_cost(500.0, base)["network_cost_norm"], base_nc)

    def test_network_cost_range(self):
        extreme = NetworkMetrics(
            target_dc="dc2",
            rtt_ms=10000.0,
            throughput_mbps=0.001,
            packet_loss_rate=100.0,
            queue_delay_ms=5000.0,
        )
        nc = compute_network_cost(1000.0, extreme)["network_cost_norm"]
        self.assertGreaterEqual(nc, 0.0)
        self.assertLessEqual(nc, 1.0)

    def test_invalid_metrics_infeasible(self):
        green = get_mock_green_forecast()
        cases = [
            NetworkMetrics("dc2", rtt_ms=10.0, throughput_mbps=0.0, packet_loss_rate=0.0),
            NetworkMetrics("dc2", rtt_ms=10.0, throughput_mbps=100.0, packet_loss_rate=150.0),
            NetworkMetrics("dc2", rtt_ms=-1.0, throughput_mbps=100.0, packet_loss_rate=0.0),
        ]
        for metrics in cases:
            nc = compute_network_cost(100.0, metrics)
            self.assertFalse(nc["feasible"])
            self.assertEqual(nc["network_cost_norm"], 1.0)

            bd = compute_objective("dc1", "dc2", green, {"dc2": metrics}, 100.0)
            self.assertFalse(bd.feasible)
            self.assertEqual(bd.network_cost_norm, 1.0)
            self.assertEqual(bd.net_benefit, -math.inf)

    def test_break_even_workload(self):
        green = get_mock_green_forecast()
        net = get_mock_network_metrics()
        l_hyst = compute_break_even_workload_mb("dc1", "dc2", green, net, threshold=0.15)
        l_greedy = compute_break_even_workload_mb("dc1", "dc2", green, net, threshold=0.0)
        self.assertIsNotNone(l_hyst)
        self.assertIsNotNone(l_greedy)
        self.assertAlmostEqual(l_hyst, 468.1875, delta=0.5)
        self.assertAlmostEqual(l_greedy, 1593.1875, delta=0.5)

    def test_params_normalization_and_validation(self):
        p = merge_objective_params({"alpha": 1.0, "beta": 1.0, "foo": "ignored", "hysteresis_threshold": 0.2})
        self.assertAlmostEqual(p["alpha"], 0.5, places=6)
        self.assertAlmostEqual(p["beta"], 0.5, places=6)
        self.assertNotIn("foo", p)

        with self.assertRaises(ValueError):
            merge_objective_params({"alpha": -1.0})


class TestDecisionAlgorithm(unittest.TestCase):
    def test_stay_decision(self):
        green = get_mock_green_forecast()
        net = make_good_metrics()
        res = decide_proposed("dc2", green, net, 100.0)
        self.assertIs(res.action, Action.STAY)
        self.assertEqual(res.target_dc, "dc2")
        self.assertFalse(res.details["hysteresis_blocked"])
        self.assertEqual(res.cost_score, 0.0)

    def test_migrate_decision(self):
        green = get_mock_green_forecast()
        net = get_mock_network_metrics()
        res = decide_proposed("dc1", green, net, 100.0)
        self.assertIs(res.action, Action.MIGRATE)
        self.assertEqual(res.target_dc, "dc2")
        self.assertAlmostEqual(res.details["best_net_benefit"], 0.1990917, places=4)
        self.assertAlmostEqual(res.cost_score, -0.1990917, places=4)
        self.assertFalse(res.details["hysteresis_blocked"])

    def test_large_workload_stays(self):
        green = get_mock_green_forecast()
        net = get_mock_network_metrics()
        res = decide_proposed("dc1", green, net, 1000.0)
        self.assertIs(res.action, Action.STAY)
        self.assertEqual(res.target_dc, "dc1")
        self.assertTrue(res.details["hysteresis_blocked"])
        self.assertAlmostEqual(res.details["best_net_benefit"], 0.0790917, places=4)

    def test_hysteresis_threshold(self):
        # Ví dụ 4: lợi ích ròng dương (~0.0888) nhưng <= 0.15 -> STAY khi bật ngưỡng, MIGRATE khi theta=0
        green_ex4 = {"dc1": 40.0, "dc2": 58.0, "dc3": 10.0}
        net = make_good_metrics()

        res_hyst = decide_proposed("dc1", green_ex4, net, 100.0, {"hysteresis_threshold": 0.15})
        self.assertIs(res_hyst.action, Action.STAY)
        self.assertEqual(res_hyst.target_dc, "dc1")
        self.assertTrue(res_hyst.details["hysteresis_blocked"])
        self.assertAlmostEqual(res_hyst.details["best_net_benefit"], 0.0888317, places=4)

        res_greedy = decide_proposed("dc1", green_ex4, net, 100.0, {"hysteresis_threshold": 0.0})
        self.assertIs(res_greedy.action, Action.MIGRATE)
        self.assertEqual(res_greedy.target_dc, "dc2")

        # Ví dụ 6: cải thiện thật sự lớn (~0.2988 > 0.15) -> Hysteresis không chặn
        green_ex6 = {"dc1": 10.0, "dc2": 20.0, "dc3": 80.0}
        res_real = decide_proposed("dc2", green_ex6, net, 100.0, {"hysteresis_threshold": 0.15})
        self.assertIs(res_real.action, Action.MIGRATE)
        self.assertEqual(res_real.target_dc, "dc3")
        self.assertAlmostEqual(res_real.details["best_net_benefit"], 0.2988317, places=4)

    def test_default_threshold_matches_config(self):
        res = decide_proposed("dc1", get_mock_green_forecast(), get_mock_network_metrics(), 100.0, params=None)
        self.assertEqual(DEFAULT_HYSTERESIS_THRESHOLD, 0.15)
        self.assertEqual(res.details["hysteresis_threshold"], 0.15)

    def test_no_feasible_candidate(self):
        green = get_mock_green_forecast()
        only_current = {"dc1": NetworkMetrics("dc1", rtt_ms=0.5, throughput_mbps=1000.0, packet_loss_rate=0.0)}
        res1 = decide_proposed("dc1", green, only_current, 100.0)
        self.assertIs(res1.action, Action.STAY)
        self.assertIn("no feasible", res1.reason)

        zero_thr = {
            "dc2": NetworkMetrics("dc2", rtt_ms=10.0, throughput_mbps=0.0, packet_loss_rate=0.0),
            "dc3": NetworkMetrics("dc3", rtt_ms=10.0, throughput_mbps=0.0, packet_loss_rate=0.0),
        }
        res2 = decide_proposed("dc1", green, zero_thr, 100.0)
        self.assertIs(res2.action, Action.STAY)
        self.assertIn("no feasible", res2.reason)

    def test_missing_data_skipped(self):
        green = {"dc1": 20.0, "dc2": 90.0, "dc3": 95.0}
        net = make_good_metrics(("dc1", "dc2"))  # thiếu dc3
        res = decide_proposed("dc1", green, net, 100.0)
        self.assertIn("dc3", res.details["skipped"])
        self.assertIs(res.action, Action.MIGRATE)
        self.assertEqual(res.target_dc, "dc2")

    def test_input_validation(self):
        green = get_mock_green_forecast()
        net = get_mock_network_metrics()
        with self.assertRaises(ValueError):
            decide_proposed("dc9", green, net, 100.0)
        with self.assertRaises(ValueError):
            decide_proposed("dc1", green, net, -5.0)
        with self.assertRaises(ValueError):
            decide_proposed("dc1", green, net, math.nan)
        with self.assertRaises(ValueError):
            decide_proposed("dc1", green, net, 100.0, {"hysteresis_threshold": -0.1})

    def test_deterministic_tie_break(self):
        green = {"dc1": 10.0, "dc2": 90.0, "dc3": 90.0}
        net = make_good_metrics()
        results = [decide_proposed("dc1", green, net, 100.0) for _ in range(5)]
        for res in results:
            self.assertIs(res.action, Action.MIGRATE)
            self.assertEqual(res.target_dc, "dc2")

    def test_current_dc_in_metrics_is_ignored(self):
        green = get_mock_green_forecast()
        net = get_mock_network_metrics()
        res = decide_proposed("dc1", green, net, 100.0)
        self.assertNotIn("dc1", res.details["candidates"])
        self.assertIs(res.action, Action.MIGRATE)
        self.assertEqual(res.target_dc, "dc2")


class TestPingPong(unittest.TestCase):
    def test_count_ping_pong_helper(self):
        self.assertEqual(count_ping_pong(["dc1", "dc2", "dc1", "dc2"]), 2)
        self.assertEqual(count_ping_pong(["dc1", "dc2", "dc3"]), 0)
        self.assertEqual(count_ping_pong([]), 0)
        self.assertEqual(count_ping_pong(["dc1", "dc1", "dc2", "dc2", "dc1"]), 1)

    def test_ping_pong_tracker(self):
        tracker = PingPongTracker("dc1")
        tracker.update("dc2")
        tracker.update("dc1")
        tracker.update("dc2")
        tracker.update("dc2")
        self.assertEqual(tracker.ping_pong_count, 2)
        self.assertEqual(tracker.migrations, 3)

    def test_oscillation_greedy_vs_hysteresis(self):
        # Ví dụ 5: 10 chu kỳ dao động nhẹ giữa dc2 và dc3
        net = make_good_metrics()
        snapshots = []
        for i in range(10):
            g_dc2 = 53.0 if i % 2 == 0 else 47.0
            g_dc3 = 100.0 - g_dc2
            snapshots.append(({"dc1": 10.0, "dc2": g_dc2, "dc3": g_dc3}, net))

        greedy_run = simulate_decisions(
            decide_proposed, "dc2", snapshots, 100.0, {"hysteresis_threshold": 0.0}
        )
        hyst_run = simulate_decisions(
            decide_proposed, "dc2", snapshots, 100.0, {"hysteresis_threshold": 0.15}
        )

        self.assertEqual(greedy_run["migrations"], 9)
        self.assertEqual(greedy_run["ping_pong_count"], 8)
        self.assertEqual(hyst_run["migrations"], 0)
        self.assertEqual(hyst_run["ping_pong_count"], 0)


class TestContractCompliance(unittest.TestCase):
    def test_signature_matches_protocol(self):
        proto_sig = inspect.signature(decision_function_protocol)
        impl_sig = inspect.signature(decide_proposed)

        proto_params = list(proto_sig.parameters.values())
        impl_params = list(impl_sig.parameters.values())
        self.assertEqual([p.name for p in impl_params], [p.name for p in proto_params])
        self.assertIsNone(impl_sig.parameters["params"].default)

    def test_result_types(self):
        green = get_mock_green_forecast()
        net = get_mock_network_metrics()
        cases = [
            ("dc1", decide_proposed("dc1", green, net, 100.0)),      # MIGRATE
            ("dc1", decide_proposed("dc1", green, net, 1000.0)),     # STAY (hysteresis)
            ("dc2", decide_proposed("dc2", green, make_good_metrics(), 100.0)),  # STAY
            ("dc1", decide_proposed("dc1", green, {}, 100.0)),       # STAY (no feasible)
        ]
        for current_dc, res in cases:
            self.assertIsInstance(res, DecisionResult)
            self.assertIsInstance(res.action, Action)
            self.assertIs(res.strategy, StrategyType.PROPOSED)
            if res.action is Action.STAY:
                self.assertEqual(res.target_dc, current_dc)
            else:
                self.assertNotEqual(res.target_dc, current_dc)

    def test_details_json_serializable(self):
        green = get_mock_green_forecast()
        net = get_mock_network_metrics()
        net["dc3"] = NetworkMetrics("dc3", rtt_ms=10.0, throughput_mbps=0.0, packet_loss_rate=0.0)
        res = decide_proposed("dc1", green, net, 100.0)

        serialized = json.dumps(res.details, allow_nan=False)
        self.assertIsInstance(serialized, str)

        expected_keys = {
            "strategy_note",
            "alpha",
            "beta",
            "hysteresis_threshold",
            "workload_size_mb",
            "current_dc",
            "current_green_kw",
            "candidates",
            "skipped",
            "best_candidate",
            "best_net_benefit",
            "hysteresis_blocked",
            "target_green_kw",
        }
        self.assertTrue(expected_keys.issubset(res.details.keys()))

    def test_inputs_not_mutated(self):
        green = get_mock_green_forecast()
        net = get_mock_network_metrics()
        params = {"alpha": 0.6, "beta": 0.4, "hysteresis_threshold": 0.15}

        green_before = copy.deepcopy(green)
        net_before = copy.deepcopy(net)
        params_before = copy.deepcopy(params)

        decide_proposed("dc1", green, net, 100.0, params)

        self.assertEqual(green, green_before)
        self.assertEqual(net, net_before)
        self.assertEqual(params, params_before)


if __name__ == "__main__":
    unittest.main()
