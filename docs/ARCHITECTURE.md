# Architecture: Heterogeneous Macro Evaluation in GEM

This document gives the block-level view that the prose in
[`TECHNICAL_REPORT.md`](./TECHNICAL_REPORT.md) walks through in depth: where
each stage of the pipeline lives, and which GPU memory tier (Global / Shared /
Registers) each of the three intercepted primitives (DSP48E2, CARRY4,
SRLC32E) actually uses. Every box below corresponds to a concrete symbol in
the codebase — see the "Source" column on each table.

---

## 1. End-to-end pipeline

```
 ┌────────────────────┐   ┌─────────────────────────┐   ┌───────────────────────────┐
 │   Yosys frontend    │   │   Rust host compiler     │   │   CUDA device (per cycle)  │
 │  (scripts/*.ys)     │   │   (src/*.rs)             │   │   (csrc/kernel_v1*.cu*)    │
 │                      │   │                          │   │                            │
 │ read_verilog -lib    │   │ aigpdk.rs   – cell typing│   │ Global memory:             │
 │   macros_bb.v        │──▶│ aig.rs      – DAG build  │──▶│  script + macro input      │
 │  (blackbox DSP48E2,  │   │ macros.rs   – pin/width  │   │  planes + persistent       │
 │   CARRY4, SRLC32E    │   │   single source of truth │   │  macro state (PREG, SRL32) │
 │   declared BEFORE    │   │ pe.rs       – boomerang  │   │                            │
 │   techmap/abc run,   │   │   scheduler + macro      │   │ Shared memory:             │
 │   so they are never  │   │   phases + warp grouping │   │  8192-bit boomerang tree   │
 │   flattened to AIG)  │   │ flatten.rs  – memory     │   │  (hier[] overlay) +        │
 │                      │   │   formatter, 64-bit      │   │  per-block metadata        │
 │                      │   │   aligned SoA layout     │   │                            │
 │                      │   │                          │   │ Registers:                 │
 │                      │   │                          │   │  per-thread macro eval +   │
 │                      │   │                          │   │  __shfl_up_sync carry scan │
 └────────────────────┘   └─────────────────────────┘   └───────────────────────────┘
```

The frontend's only job is to stop `techmap`/`abc` from ever seeing the
macros. Everything about *how* a macro is represented in memory is decided
once, in `src/macros.rs`, and consumed by both the host compiler and (via
generated constants) the CUDA kernel — so the two cannot drift apart.

---

## 2. GPU memory hierarchy: where each primitive actually lives

GEM's execution model already has three tiers, and the macro work slots into
each of them rather than adding a fourth:

| Tier | Lifetime | What stock GEM already keeps there | What the macro fork adds |
|---|---|---|---|
| **Global memory (VRAM)** | Persists across all simulated cycles | The compiled script, primary I/O state, SRAM contents | Macro **input planes** (structure-of-arrays, 64-bit aligned) and macro **persistent state** (DSP `PREG`, SRLC32E 32-bit shift register) — allocated immediately after the SRAM region (`macro_state_origin = sum_srams_start` in `flatten.rs`) |
| **Shared memory** | One thread block, one cycle | The 8,192-bit boomerang tree scratchpad (`hier[]`) used for the 13-level AND-reduction | Macro **outputs consumed within the same cycle** are written into unused `hier[1]` slots (the "zero-cost overlay", `flatten.rs` §2.3) instead of a new buffer; a small `shared_metadata[256]` block also carries the offset into global memory where each partition's macro state begins (`dsp_state_base`) |
| **Registers** | One thread, one instruction (or one warp, for a shuffle) | Per-thread AND/inverter evaluation | Per-lane macro input/output values during evaluation, and the **CARRY4 Kogge-Stone carry scan**, which never touches memory at all — carries move lane-to-lane via `__shfl_up_sync` |

### Per-primitive placement

| Primitive | Global memory (VRAM) | Shared memory (per block) | Registers (per thread/warp) |
|---|---|---|---|
| **DSP48E2** (MAC) — register-only, no mid-cycle work | `in_ad[i]` (`A[26:0]`\|`D[26:0]`), `in_c[i]` (`C[47:0]`), `in_bctl[i]` (`B[17:0]`\|`OPMODE`\|`PREADD`); `state_p[i]` = `PREG`, persists cycle-to-cycle | Not visited mid-partition — `PREG` is read like a flip-flop's `Q`, at cycle start | 48-bit ALU computed in-register at the endpoint phase, then written straight back to the global `PREG` slot |
| **CARRY4** — purely combinational, no persistent state | Chain inputs `S`/`DI` arrive via the normal boomerang script, same as any other gate — no separate global buffer needed | Mid-partition phase reads `S`/`DI`/`CI` from `hier[]` tree results | Kogge-Stone `(generate, propagate)` pair per lane, combined over 5 `__shfl_up_sync` steps; `O[3:0]`/`CO[3:0]` computed and scattered back to `hier[1]` |
| **SRLC32E** — combinational read + clocked shift | 32-bit shift state persists in global memory (`state_words() > 0`, same region as DSP `PREG`) | Mid-partition phase: combinational read `Q = state[A]` scattered into `hier[1]`; state commit writes the shifted value back | Read-then-shift computed per-lane in registers before the state-buffer write-back |

### Why this split, not some other one

- **Global memory holds anything that must survive to the next cycle.**
  `PREG` and the SRL's 32-bit register are architecturally flip-flops; GEM
  already re-reads all flip-flop state from global memory every cycle, so
  putting macro state there is the only place consistent with the rest of
  the simulator (and it is what makes the 64-bit aligned, structure-of-arrays
  layout in the problem statement's "Heterogeneous Memory Allocator"
  requirement meaningful — it is a *global*-memory coalescing concern).
- **Shared memory is reused, not expanded**, because GEM's occupancy is
  already register-bound (2 blocks/SM, see `TECHNICAL_REPORT.md` §2.5); any
  new `__shared__` allocation would have cost occupancy everywhere, not just
  on macro-containing designs.
- **Registers carry the CARRY4 chain** specifically because a carry chain is
  a genuine sequential dependency (`CO[3] → CIN`) that would otherwise force
  one boomerang phase per link; a warp shuffle resolves an 8-link chain in
  5 steps with zero shared-memory or global-memory traffic.

---

## 3. CARRY4 warp-level carry scan (register tier detail)

```
lane:        0        1        2        3        4   ...   31
           ┌────┐  ┌────┐  ┌────┐  ┌────┐  ┌────┐      ┌────┐
S[3:0]/DI ▶│CARRY4├▶│CARRY4├▶│CARRY4├▶│CARRY4├▶│CARRY4├ ... ▶│CARRY4│
           │block│  │block│  │block│  │block│  │block│      │block│
           └──┬─┘  └──┬─┘  └──┬─┘  └──┬─┘  └──┬─┘      └──┬─┘
              │ (g,p)  │ (g,p)  │ (g,p)  │ (g,p)  │ (g,p)      │ (g,p)
              ▼        ▼        ▼        ▼        ▼            ▼
        ───────────────────────────────────────────────────────────
         5 rounds of __shfl_up_sync (Kogge-Stone prefix scan)
         segmented: a chain head clears `propagate`, so no carry
         crosses into an unrelated chain sharing the same warp
        ───────────────────────────────────────────────────────────
              │        │        │        │        │            │
              ▼        ▼        ▼        ▼        ▼            ▼
          true CIN recovered per lane → each lane computes its own
          4-bit O[3:0] / CO[3:0] independently, in registers
```

An 8-link 32-bit adder resolves in `log2(32) = 5` shuffle rounds instead of
8 sequential boomerang phases — each of which would otherwise have cost a
full script re-read from global memory.

---

## 4. Source map

| Diagram element | File | Symbol |
|---|---|---|
| Blackbox interception | `scripts/macros_bb.v`, `scripts/macro.ys` | `(* blackbox *) module CARRY4/DSP48E2/SRLC32E` |
| Pin/width single source of truth | `src/macros.rs` | `MacroInst`, slot tables |
| DAG construction, per-output fan-in | `src/aig.rs` | `Macro` driver/endpoint kinds |
| Boomerang scheduler, warp grouping | `src/pe.rs` | mid-partition macro phases, chain grouping |
| Global-memory SoA layout, state allocation | `src/flatten.rs` | `macro_state_off`, `macro_state_origin`, `state_words()` |
| `hier[1]` shared-memory overlay | `src/flatten.rs`, `csrc/kernel_v1_impl.cuh` | spare `hier[1]` slot reuse |
| CUDA macro evaluation phase | `csrc/kernel_v1_impl.cuh` | `shared_metadata`, `dsp_state_base`, `GEM_KIND_SRLC32E` |
| Kogge-Stone carry scan | `csrc/kernel_v1_impl.cuh` | `__shfl_up_sync` calls in the CARRY4 path |

For the numerical results this architecture produces (throughput, DRAM
utilisation, register pressure), see `TECHNICAL_REPORT.md` §4.
