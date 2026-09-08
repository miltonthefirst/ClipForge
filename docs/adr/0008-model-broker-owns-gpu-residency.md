# ADR-0008 — One broker owns GPU residency, and it measures rather than tracks

- **Status:** Accepted
- **Date:** 2026-09-08
- **Phase:** 2

## Context

The binding constraint on this architecture is 6144 MiB of VRAM, of which roughly
5.4 GB is usable once the Windows desktop and a browser have taken their share.
Whisper `large-v3-turbo` needs about 1.6 GB and a 4B LLM about 3.4 GB. They fit
together only on paper, and not at all once either grows.

So the pipeline must load exactly one model at a time and evict it before loading
the next. The question is who enforces that, and how it knows whether there is
room.

Phase 0 answered the second half unexpectedly. A `doctor` run reported **1.3 GB
free on a 6 GB card**: an unrelated interactive Ollama session was holding 4.8 GB
at 17% GPU offload. Nothing about that was ClipForge's doing, and no amount of
internal bookkeeping would have revealed it.

There is also no allocator to interrogate. `faster-whisper` runs on CTranslate2,
which frees on refcount drop, and the LLM runs out-of-process in Ollama, which
frees on its own schedule. There is no `torch.cuda.memory_allocated()` to call —
see [ADR-0002](0002-ctranslate2-without-pytorch.md).

## Decision

A single `ModelBroker` owns GPU residency. Every GPU stage acquires a lease from
it before loading anything, and **the broker's lock is the GPU lane**. There is
no separate GPU queue: modelling the lane as a lock rather than a second pool
means a job never has to be handed between pools mid-flight, and the invariant
"at most one model resident" is enforced by the same mechanism that expresses it.

Jobs run in a thread pool sized by `CLIPFORGE_CPU_LANE_DEPTH`, so a download or a
render proceeds while another job transcribes. Only GPU work serialises.

Three properties follow from the Phase 0 finding:

**The broker measures actual free VRAM through NVML, rather than tracking its own
allocations.** Tracking would have reported an empty card in the situation
actually observed.

**A shortfall names the process holding the memory.** "CUDA out of memory" tells
an operator nothing they can act on. "needs 1600 MiB, 600 MiB available,
ollama.exe (pid 8412) is holding 4800 MiB" tells them exactly what to close.

**Release is verified, not assumed.** The broker re-probes after a lease ends and
logs a warning if the memory did not come back. It logs rather than raising: the
stage has already finished, and failing a completed job because the *next* one
might not fit would be the wrong trade. The next acquire's budget check is what
actually enforces it.

The broker takes an **injected probe**. That is what makes the constrained-card
behaviour — the case that matters and the one that cannot be arranged on demand —
an ordinary unit test rather than something only reproducible by opening a chat
window on the right machine.

## Consequences

- A VRAM shortfall is classified **retryable**: whatever is holding the card will
  very likely let go, so the job requeues rather than dying.
- The broker can be configured to wait for capacity with backoff before refusing.
  The refusal path releases the lock *before* raising, so a foreign process
  cannot wedge the entire worker.
- The lock is deliberately **not reentrant**. A stage that acquires the broker
  while already holding it has a design error; a reentrant lock would hide it,
  where a plain one deadlocks immediately and visibly in a test.
- On a machine with no NVIDIA device the broker allows everything through. CI
  runs on one, and refusing there would make the whole harness unrunnable — a
  stage that genuinely needed a device will fail clearly on its own.
- Per-stage peak VRAM is sampled through the lease and written to the job
  document, so the model ladder in `docs/PLAN.md` §2.1 becomes an empirical
  question rather than an estimated one.

## Alternatives considered

- **A semaphore of depth 1 with no measurement.** Enforces exclusivity between
  *our* stages and is blind to everything else — which is precisely the failure
  Phase 0 hit.
- **A separate GPU worker process.** Clean isolation, and it makes the free-VRAM
  problem worse rather than better: two processes each measuring a shared card,
  with an IPC protocol to invent. The lock is in-process because the work is.
- **Trusting a static VRAM table per model.** Estimates drift with quantisation,
  context length and driver version, and they say nothing about what else is
  resident. Measuring costs one NVML call.
- **Failing the whole worker when VRAM is short.** Tempting for a single-user
  deployment, and wrong: the CPU lane can still make progress on downloads and
  renders while the card is busy.
