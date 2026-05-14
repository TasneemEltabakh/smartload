# Conformance tests

Interface-level tests every implementation of a SmartLoad plugin contract must pass. Same idea as Kubernetes CSI/CNI conformance suites — proves that two adapters (NGINX vs Envoy) are interchangeable from the decision plane's point of view.

## Suites

- `lb_adapter/` — `LoadBalancerAdapter` contract. Any new adapter (NGINX, Envoy, HAProxy, ALB) must pass these.

## Anti-pattern explicitly rejected

KEDA-style flat `pkg/scalers/` where every plugin is a single Go file with no shared test discipline. Conformance tests are the discipline that prevents quality drift across plugins.
