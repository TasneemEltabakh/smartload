import json
import logging

try:
    import redis
except ImportError:  # pragma: no cover
    redis = None


class RedisRoutingPublisher:
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

    def publish(self, payload):
        message = json.dumps(payload)
        try:
            client = self._client_or_connect()
            if client is None:
                logging.warning(json.dumps({
                    "event": "redis_unavailable",
                    "channel": self.config.redis_channel,
                    "error": "redis package is not installed; skipping publish"
                }))
                return
            client.publish(self.config.redis_channel, message)
        except Exception as exc:
            logging.warning(json.dumps({
                "event": "redis_publish_failed",
                "channel": self.config.redis_channel,
                "error": str(exc)
            }))
