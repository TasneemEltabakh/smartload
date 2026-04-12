from datetime import datetime, timedelta, timezone


class ExponentialSmoothingForecaster:
    def __init__(self, config):
        self.config = config

    def forecast(self, records):
        series = [record["request_count"] for record in records]
        timestamps = [self._parse_timestamp(record["timestamp"]) for record in records]
        if len(series) < self.config.minimum_history_points:
            raise ValueError("not enough history to generate forecast")

        model_name = self._select_model(series)
        forecast_values = self._forecast_series(series, self.config.horizon_minutes, model_name)
        forecast_value = round(sum(forecast_values) / len(forecast_values), 2)
        last_timestamp = timestamps[-1]

        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "metric": "request_count",
            "scope": "total_system_load",
            "horizon_minutes": self.config.horizon_minutes,
            "predicted_request_rate": forecast_value,
            "forecast_sequence": [round(value, 2) for value in forecast_values],
            "prediction_window_start": last_timestamp.isoformat(),
            "prediction_window_end": (last_timestamp + timedelta(minutes=self.config.horizon_minutes)).isoformat(),
            "model": model_name,
            "alpha": self.config.smoothing_alpha
        }

    def evaluate(self, records):
        series = [record["request_count"] for record in records]
        timestamps = [record["timestamp"] for record in records]
        if len(series) < self.config.minimum_history_points + self.config.horizon_minutes:
            raise ValueError("not enough history to evaluate forecast accuracy")

        forecasts = []
        actuals = []
        horizon = self.config.horizon_minutes
        start_index = self.config.minimum_history_points
        model_name = self._select_model(series[:start_index + horizon])
        for index in range(start_index, len(series) - horizon + 1):
            history = series[:index]
            predicted_values = self._forecast_series(history, horizon, model_name)
            predicted = sum(predicted_values) / len(predicted_values)
            actual_window = series[index:index + horizon]
            actual = sum(actual_window) / len(actual_window)
            forecasts.append(predicted)
            actuals.append(actual)

        mape = self._mape(actuals, forecasts)
        latest_actual = actuals[-1]
        latest_predicted = forecasts[-1]

        return {
            "evaluated_at": datetime.now(timezone.utc).isoformat(),
            "samples": len(actuals),
            "mape": round(mape, 2),
            "acceptable_mape": self.config.acceptable_mape,
            "meets_threshold": mape <= self.config.acceptable_mape,
            "latest_actual_average_request_rate": round(latest_actual, 2),
            "latest_predicted_average_request_rate": round(latest_predicted, 2),
            "latest_actual_timestamp": timestamps[-1],
            "selected_model": model_name
        }

    def _forecast_series(self, values, horizon, model_name):
        if model_name == "persistence":
            baseline = float(values[-1]) if values else 0.0
            return [baseline] * horizon
        baseline = self._single_exponential_smoothing(values)
        return [baseline] * horizon

    def _single_exponential_smoothing(self, values):
        if not values:
            return 0.0
        baseline = float(values[0])
        for value in values[1:]:
            baseline = (self.config.smoothing_alpha * float(value)) + (
                (1 - self.config.smoothing_alpha) * baseline
            )
        return baseline

    def _select_model(self, values):
        if len(values) < self.config.minimum_history_points + self.config.horizon_minutes:
            return "single_exponential_smoothing"

        candidates = {
            "persistence": lambda hist, horizon: [float(hist[-1])] * horizon,
            "single_exponential_smoothing": lambda hist, horizon: [
                self._single_exponential_smoothing(hist)
            ] * horizon,
        }

        best_model = "single_exponential_smoothing"
        best_mape = float("inf")
        horizon = self.config.horizon_minutes
        start_index = self.config.minimum_history_points

        for model_name, predictor in candidates.items():
            actuals = []
            forecasts = []
            for index in range(start_index, len(values) - horizon + 1):
                history = values[:index]
                predicted = sum(predictor(history, horizon)) / horizon
                actual = sum(values[index:index + horizon]) / horizon
                actuals.append(actual)
                forecasts.append(predicted)
            mape = self._mape(actuals, forecasts)
            if mape < best_mape:
                best_mape = mape
                best_model = model_name
        return best_model

    def _mape(self, actuals, forecasts):
        total = 0.0
        count = 0
        for actual, forecast in zip(actuals, forecasts):
            if actual == 0:
                continue
            total += abs((actual - forecast) / actual) * 100
            count += 1
        return total / count if count else 0.0

    def _parse_timestamp(self, value):
        if value.endswith("Z"):
            value = value.replace("Z", "+00:00")
        return datetime.fromisoformat(value).astimezone(timezone.utc)
