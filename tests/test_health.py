"""Output timing is independent of hardware, network and source frame rate."""
import unittest

from live.health import OutputHealth


class OutputHealthTests(unittest.TestCase):
    def test_warns_for_sustained_half_speed_then_recovers(self):
        health = OutputHealth()
        for second in range(21):
            result = health.update(second, second * 0.43)
            if second < 15:
                self.assertFalse(result["warning"])
        self.assertAlmostEqual(result["realtime_speed"], 0.43)
        self.assertIn("0.43x", result["warning"])
        for second in range(21, 36):
            result = health.update(second, 20 * 0.43 + second - 20)
        self.assertFalse(result["warning"])
        self.assertAlmostEqual(result["realtime_speed"], 1)

    def test_output_freeze_warns_even_though_progress_messages_keep_arriving(self):
        health = OutputHealth()
        for second in range(31):
            result = health.update(second, min(second, 10))
        self.assertTrue(result["warning"])
        self.assertEqual(result["realtime_speed"], 0)

    def test_normal_rate_and_irregular_updates_do_not_warn(self):
        for rate in (0.98, 1.0, 1.05):
            health = OutputHealth()
            for second in (0, 1, 4, 6, 10, 15, 19, 23, 30):
                result = health.update(second, second * rate)
                self.assertFalse(result["warning"])

    def test_new_connection_and_invalid_values_do_not_inherit_old_lag(self):
        health = OutputHealth()
        for second in range(21):
            health.update(second, second * 0.4)
        reset = health.update(21, 0)
        self.assertIsNone(reset["realtime_speed"])
        self.assertFalse(reset["warning"])
        self.assertEqual(health.update(22, float("nan")), {})
        self.assertEqual(health.update(23, -1), {})
        self.assertFalse(health.update(24, 3)["warning"])
