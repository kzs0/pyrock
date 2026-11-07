# pyrock

pyrock is the Python port of [Bedrock](https://github.com/kzs0/bedrock): one
opinionated, zero-dependency observability library for CPython 3.11+ that
covers traces (OTLP HTTP/gRPC), Prometheus metrics, continuous profiles (OTLP
Profiles) and structured, trace-correlated logs, all built around *operations*
(named units of work), with the same metric names, span attributes, log shape
and `BEDROCK_*` environment variables as the Go library. It is under active
development; see [DESIGN.md](DESIGN.md) for the design and API.
