# alb adapter (stub)

Placeholder for an AWS Application Load Balancer implementation of `LoadBalancerAdapter`. Raises `NotImplementedError` on construction in v1.

## When to implement

When a customer or integrator requests ALB support, open a feature issue and replace this stub with the real implementation. The adapter must pass `tests/conformance/lb_adapter/` to land. Will likely depend on boto3 / aws-sdk for upstream weight adjustment via the ALB API.
