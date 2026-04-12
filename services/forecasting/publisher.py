import json
import logging

try:
    import redis
except ImportError:  # pragma: no cover - exercised in local environments without dependencies
    redis = None


class RedisForecastPublisher:
    def __init__(self, config):
        self.config = config
        self._client = None

    def _client_or_connect(self):
        if redis is None:
            return None
        if self._client is None:
            self._client = redis.Redis(
                host=self.config.redis_host,
                port=self.config.redis_port,
                decode_responses=True
            )
        return self._client

    def publish(self, forecast_message):
        payload = json.dumps(forecast_message)
        try:
            client = self._client_or_connect()
            if client is None:
                logging.warning(json.dumps({
                    "event": "redis_unavailable",
                    "channel": self.config.redis_channel,
                    "error": "redis package is not installed; skipping publish"
                }))
                return
            client.publish(self.config.redis_channel, payload)
        except Exception as exc:
            logging.warning(json.dumps({
                "event": "redis_publish_failed",
                "channel": self.config.redis_channel,
                "error": str(exc)
            }))
