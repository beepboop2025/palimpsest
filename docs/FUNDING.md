# Funding and the public-good model

Palimpsest is built to be **funded by grants and to stay free.** It is not a commercial
product. It does not, and will not, monetize the people or the topics it observes — that
would both corrupt the incentives of a transparency tool and endanger the people it exists
to serve. The output (the index, the dataset, the measurement layer) is openly licensed so
that journalists, researchers, and other anti-censorship projects can build on it at no cost.

## Why grant-funded, not commercial

The value Palimpsest creates is a **public good**: a continuous, quantified early-warning
signal of what an authoritarian state is suppressing. Public goods are chronically
under-produced by markets, because the people who benefit most — journalists covering China,
human rights defenders, diaspora activists — cannot and should not pay for it. Internet-freedom
funders exist precisely to fund this category. Palimpsest is designed to fit it cleanly:
open-source, auditable, safety-first, and non-extractive.

## The fundable core: closing the velocity gap

Two of the three DDTI legs — **selectivity** and **novelty** — run today from open
infrastructure. The third, **velocity** (minute-resolution deletion speed), is blocked from
outside China because the relevant feeds are walled to foreign traffic. This is the honest gap,
and closing it is the heart of the funded work:

1. **In-country measurement capacity** — a safe, infrastructure-only egress path (never a
   person) so the velocity leg can observe deletions at minute resolution.
2. **UNDERTEXT: many-vantage differential observation** — instead of trusting one vantage point,
   observe the same public content from several and treat *disagreement between them* as the
   censorship signal itself. Divergence is the measurement. (Design invented for Palimpsest;
   the open research question is the scoring function.)
3. **Selectivity → true rate** — join the deletion numerator against a topic-volume denominator
   (trending streams) to upgrade attention-allocation into a genuine deletion *rate*.
4. **Open dataset + API hardening** — publish the index and underlying observations as a
   reusable, openly licensed feed for the wider community.

## Deliverables a funder can verify

Everything is structured to be checkable by an external code audit (a standard OTF step):

- A working, runnable prototype today — `python3 demo/palimpsest_demo.py` produces a real
  result with no install.
- Pure, offline, **tested** cores for the scoring, discovery, and governance logic
  (`tests/`, plus the 34-test deletion-detector suite in `censorwatch/tests/`).
- Safety enforced in code, not just documented (`core/governance.py`): kill switch, rate
  ceiling, tamper-evident audit log.
- Open licensing (MIT now; AGPL-3.0 available if a funder prefers stronger copyleft).

## Who benefits

- **Journalists covering China** get an early, quantified read on which topics are being
  contained — often before the story is otherwise visible.
- **Human rights and research organizations** get an openly licensed measurement layer they
  can cite and extend.
- **The anti-censorship ecosystem** (OONI, GreatFire, Citizen Lab, CDT) gets a complementary
  content-layer signal and a dataset designed to be shared back.

## Status

Prepared as the open-source core for an Open Technology Fund, Internet Freedom Fund application
(IFF-2026-06). Budget and milestone detail live in the application itself. The maintainer
commits to an open code audit and open licensing as conditions of funding.

*If you are a funder or a potential collaborator, the best starting point is to run the demo,
read [METHODOLOGY.md](METHODOLOGY.md) and [ETHICS.md](ETHICS.md), and open an issue or reach out.*
