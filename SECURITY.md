# Security Policy

## Supported versions

pyrock is a pre-1.0 Python package. The latest release is the supported
version; `main` is development code, not a release. Older versions are not
guaranteed to receive backports. Upgrade to the newest release before
reporting a problem that may already be fixed.

## Reporting a vulnerability

Do not open a public issue for a suspected vulnerability. Use GitHub's
[private vulnerability reporting form](https://github.com/kzs0/pyrock/security/advisories/new)
instead. If that form is unavailable, contact the repository owner through the
contact information on their [GitHub profile](https://github.com/kzs0).

Include enough information to reproduce and assess the report:

- the affected pyrock version or commit, and the Python version and build
  (including whether it is free-threaded);
- the deployment and relevant configuration (`BEDROCK_*` variables, privacy
  policy, server address);
- the impact and a minimal proof of concept;
- known workarounds; and
- whether the issue or proof of concept has been disclosed elsewhere.

There is no fixed response SLA. The maintainers investigate reports as
capacity allows and coordinate a disclosure date when a fix is required.
Please keep details private until a release or coordinated disclosure is
ready.

## Scope and deployment responsibility

This policy covers the source code in this repository. Report vulnerabilities
in CPython, an operating system, a collector, a proxy, a web framework or
another third-party service to that project as well. The copy-in recipes in
`examples/recipes/` are examples; review them like your own code.

Observability data and endpoints can expose operational details. Deployers
should:

- authenticate or isolate collectors, and keep credentials out of telemetry
  (the default privacy policy redacts common secret keys, but cannot know your
  data);
- keep the observability server on loopback (the default) or another intended
  interface; binding to `0.0.0.0` is an explicit choice;
- enable `/debug/pprof` only where its cost and exposure are acceptable; it
  serves stacks with full source paths;
- treat `allow_error_details`, `allow_source_paths` and broad
  `attribute_keys` / `resource_keys` allowlists as decisions to review; and
- use `insecure=True` / `allow_http=True` only for local development, because
  API keys and headers are then sent unencrypted.

Review configuration when upgrading; security-sensitive defaults and
validation may become stricter.
