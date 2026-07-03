# Security Policy

InferLens is pre-alpha. There are no supported released versions yet; security
fixes land on `main`. Once versioned releases begin, this policy will name the
supported range.

## Reporting a vulnerability

Please report suspected vulnerabilities privately — **do not open a public
issue** for anything exploitable.

- Preferred: [open a private security advisory](https://github.com/rabrooks/inferlens/security/advisories/new)
  via GitHub's private vulnerability reporting.
- Alternatively, email **<aaronbrooks322@gmail.com>** with `[inferlens security]`
  in the subject.

Please include a description, affected component, and reproduction steps.
As a solo-maintained project, expect an initial acknowledgement within about a
week. Coordinated disclosure is appreciated: give us a reasonable window to
ship a fix before publishing details.

## Design guarantees relevant to security

Two properties are intended to keep InferLens low-risk to install alongside a
production inference engine. A regression in either is treated as a security
bug:

- **Inert by default.** Merely installing InferLens must never change an
  engine's behavior. The vLLM stat-logger plugin does nothing unless
  `INFERLENS_TRACE_PATH` is set.
- **No engine monkeypatching.** Collectors observe through supported engine
  extension points (plugins, event streams); they do not patch engine
  internals at runtime.

## Handling trace files

Trace files (`*.ilens*`) capture engine telemetry and may include request
metadata. Treat them as potentially sensitive artifacts: they are gitignored
by default and should not be shared without review.
