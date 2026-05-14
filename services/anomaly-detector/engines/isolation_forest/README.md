# isolation_forest engine

Scikit-learn IsolationForest trained on preprocessed NAB + Yahoo SMD anomaly traces. Replaces the threshold baseline when `ANOMALY_ENGINE=isolation_forest` and a model file is present.

## Status

Scaffolded only. Implementation lands when:
- the model has been trained (issue #101, Nada's work)
- preprocessed NAB / Yahoo SMD datasets exist (R0.x, R1.1)

## Planned files

- `engine.py` — `IsolationForestEngine(AnomalyEngine)` that loads the .pkl on init and falls back to threshold logic if the file is missing
- `models/isolation_forest.pkl` — trained model artifact (lives at `services/anomaly-detector/models/`)
- `test_engine.py` — fixture-based tests against a known-anomalous trace

## Why the folder exists today

To make the plugin contract visible. The factory in `engine.py` already references this module — when the implementation lands, no service-level rewiring is needed.
