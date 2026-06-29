# Security and source-safety reporting

Palimpsest measures censorship next to people who can be harmed. If you believe any
part of this project could endanger a person or a source, report it **privately and
before** using or extending the code. Source safety overrides every other consideration,
including completeness of measurement.

## How to report privately

- **Preferred:** GitHub private vulnerability reporting — open the repository's
  **Security** tab and choose **Report a vulnerability**. This reaches the maintainer
  privately, with no public trace.
- **Do not** open a public issue for anything that could expose a person, a source, or a
  live collection seam.

## In scope

- Anything that could deanonymize or profile a poster, contributor, or source.
- Any path that places a person inside the censoring jurisdiction at risk.
- Exposure of collection infrastructure (proxy / egress) that could be turned against users.
- Standard software vulnerabilities (injection, secret exposure, dependency CVEs).

## Out of scope

The project is OSINT-only by design (see [docs/ETHICS.md](docs/ETHICS.md)). Requests to add
intrusion, deanonymization, or offensive capability are declined as out of scope, not
triaged as features.

We aim to acknowledge a good-faith report within a few days.
