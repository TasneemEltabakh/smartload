# SmartLoad

SmartLoad is a middleware system designed to manage workload distribution across backend services.
The system combines traditional load balancing with data-driven decision components such as telemetry collection, anomaly detection, forecasting, and reinforcement learning.

The goal of the project is to study how intelligent decision systems can improve request routing, system stability, and resource utilization in distributed environments.

---

# System Overview

SmartLoad sits between incoming client requests and backend services.
It receives traffic, observes system behavior through telemetry, and applies routing and scaling decisions.

The system begins with a **baseline routing mechanism** (round-robin load balancing).
More advanced components such as anomaly detection, forecasting, and reinforcement learning are added on top of this baseline.

At a high level, the system performs the following tasks:

* receive incoming client requests
* distribute traffic across backend services
* collect telemetry about system performance
* detect abnormal behavior in backend nodes
* predict future workload trends
* adapt routing and scaling decisions

---

# High-Level Architecture

```
Clients
   |
   v
NGINX (Ingress / Load Balancer)
   |
   v
SmartLoad Middleware
   |
   |---- Telemetry Service
   |---- Anomaly Detection
   |---- Forecasting Module
   |---- Reinforcement Learning Engine
   |---- Autoscaler
   |
   v
Backend Services
```

### Main Components

**Ingress Layer**

Handles incoming HTTP requests and forwards them to backend services.
In the current prototype this is implemented using **NGINX**.

**Telemetry Layer**

Collects system metrics such as request latency, response times, and server utilization.

**Decision Layer**

Processes telemetry data to support routing and scaling decisions.
This layer includes:

* anomaly detection
* workload forecasting
* reinforcement learning routing

**Control Layer**

Applies decisions to the system by updating routing behavior or scaling backend instances.

---


# Repository Structure

```
smartload/
│
├── .github/
│   └── workflows/
│       └── ci.yml
│       CI/CD pipeline definitions for automated builds, tests, and linting.
│
├── datasets/
│   Public and synthetic datasets used for training and evaluation
│   of SmartLoad models (forecasting, anomaly detection, RL).
│
├── docs/
│   Architecture documentation, design decisions, diagrams,
│   and technical specifications.
│
├── infrastructure/
│
│   ├── docker-compose.yml
│   │   Local development environment for running SmartLoad services.
│   │
│   ├── nginx/
│   │   NGINX configuration acting as the ingress load balancer.
│   │
│   └── k8s/
│       Kubernetes deployment manifests for production environments.
│
├── servers/
│   Dummy backend services used to simulate application servers
│   for testing routing and load balancing behavior.
│
│   ├── app.js
│   │   Simple HTTP server used for testing load balancing.
│   │
│   ├── Dockerfile
│   │   Container definition for the test backend service.
│   │
│   ├── package.json
│   └── package-lock.json
│
├── services/
│   Core SmartLoad intelligent services.
│
│   ├── anomaly-detector/
│   │   Detects abnormal system behavior such as latency spikes
│   │   or failing backend nodes.
│   │
│   ├── autoscaler/
│   │   Handles automatic scaling decisions based on load
│   │   and forecasting predictions.
│   │
│   ├── forecasting/
│   │   Predicts future traffic patterns using time-series models.
│   │
│   ├── policy-manager/
│   │   Manages system policies such as routing modes,
│   │   safety constraints, and scaling limits.
│   │
│   ├── rl-engine/
│   │   Reinforcement Learning service that learns optimal
│   │   routing strategies from telemetry data.
│   │
│   └── traffic-simulator/
│       Generates synthetic workloads to test SmartLoad behavior.
│
├── telemetry/
│   Telemetry and monitoring pipeline responsible for collecting
│   system metrics such as latency, CPU usage, queue length,
│   and request throughput.
│
├── tests/
│   Unit tests, integration tests, and system-level tests
│   validating SmartLoad behavior.
│
├── .dockerignore
│   Docker build exclusions.
│
├── .gitignore
│   Git ignored files.
│
├── LICENSE
│   Project license.
│
└── README.md
    Main project documentation.
```

---

# Data Sources

The system uses publicly available workload datasets for training and evaluation.

Examples include:

* Google Borg cluster traces
* Alibaba cluster traces
* Numenta Anomaly Benchmark
* Yahoo Service Machine Dataset

These datasets are used to train forecasting and anomaly detection models.


---

# Documentation

Additional design documents are available in the `docs/` directory.

* system architecture
* API design
* implementation plans
* evaluation methodology

---

# License

This project is developed as part of an academic research project.

