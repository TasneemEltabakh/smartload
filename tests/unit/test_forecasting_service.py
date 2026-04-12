import sys
import unittest
from pathlib import Path


SERVICE_DIR = Path(__file__).resolve().parents[2] / "services" / "forecasting"
sys.path.insert(0, str(SERVICE_DIR))

from config import load_config  # noqa: E402
from forecaster import ExponentialSmoothingForecaster  # noqa: E402


class ForecastingServiceTests(unittest.TestCase):
    def setUp(self):
        self.config = load_config()
        self.forecaster = ExponentialSmoothingForecaster(self.config)

    def test_forecast_returns_next_interval_prediction(self):
        records = self._build_sinusoidal_records()
        forecast = self.forecaster.forecast(records)
        self.assertEqual(forecast["scope"], "total_system_load")
        self.assertEqual(forecast["horizon_minutes"], 5)
        self.assertGreater(forecast["predicted_request_rate"], 0)

    def test_sinusoidal_history_meets_mape_threshold(self):
        records = self._build_sinusoidal_records()
        evaluation = self.forecaster.evaluate(records)
        self.assertTrue(evaluation["meets_threshold"])
        self.assertLessEqual(evaluation["mape"], self.config.acceptable_mape)

    def test_upward_trend_forecast_tracks_recent_history(self):
        records = []
        for minute in range(20):
            records.append({
                "timestamp": f"2026-04-12T18:{minute:02d}:00Z",
                "request_count": 100 + (minute * 4)
            })

        forecast = self.forecaster.forecast(records)
        self.assertGreater(forecast["predicted_request_rate"], 140)

    def _build_sinusoidal_records(self):
        values = [
            120, 128, 137, 146, 154, 160, 164, 166, 164, 160,
            154, 146, 137, 128, 120, 112, 103, 94, 86, 80,
            76, 74, 76, 80, 86, 94, 103, 112, 120, 128,
            137, 146, 154, 160, 164, 166, 164, 160, 154, 146
        ]
        return [
            {
                "timestamp": f"2026-04-12T18:{index:02d}:00Z",
                "request_count": value
            }
            for index, value in enumerate(values)
        ]


if __name__ == "__main__":
    unittest.main()
