"""Envoy adapter — stub. Real implementation pending."""

from ..base import LoadBalancerAdapter, AdapterState


class EnvoyAdapter(LoadBalancerAdapter):
    def __init__(self, *args, **kwargs):
        raise NotImplementedError(
            "EnvoyAdapter not implemented. Open a feature request if needed."
        )

    def set_upstream_weights(self, backend_weights):  # pragma: no cover
        raise NotImplementedError

    def exclude_backend(self, backend_id):  # pragma: no cover
        raise NotImplementedError

    def include_backend(self, backend_id):  # pragma: no cover
        raise NotImplementedError

    def current_state(self):  # pragma: no cover
        raise NotImplementedError
