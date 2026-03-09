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
├── services/
│   ├── load-balancer/
│   ├── telemetry/
│   ├── anomaly-detector/
│   ├── forecasting/
│   ├── rl-engine/
│   ├── autoscaler/
│   └── policy-manager/
│
├── infrastructure/
│   └── nginx/
│
├── datasets/
│
├── docs/
│   ├── architecture.md
│   ├── api-design.md
│   └── system-design.md
│
├── docker-compose.yml
└── README.md
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

