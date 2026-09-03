#!/usr/bin/env python3
"""Generate an input VCD for test_data/macrobench/macro_bench.v.

Why this file exists: TECHNICAL_REPORT.md's "Reproducing Our Results"
appendix runs

    bash scripts/bench.sh macro build/macrobench.gv build/macrobench.gemparts stim.vcd 40

but no generator for `stim.vcd` shipped in the repo. The only stimulus
script present, gen_stimulus.py, drives the *MIPS* `Computer` module's
ports (reset/ins_addr/ins/...) -- a completely different port list from
macro_bench's (clk/rst/a_in/b_in/d_in/srl_*/add_*). This script closes that
gap so the macro-rich micro-benchmark -- the design that actually produces
the report's headline 1.59x / 220x-less-DRAM numbers -- can be driven
end-to-end without hand-authoring a VCD.

Stimulus is boundary-biased on purpose, matching TECHNICAL_REPORT.md's
verification methodology (SS4.1): signed extremes, 1<<(w-1), and all-ones,
because that is where the 27-bit pre-adder wrap and 48-bit accumulator wrap
live. The SRLC32E read address (srl_a) is swept across all 32 values so the
combinational Q = state[A] read is exercised at every address at least once.

Usage:
  python3 scripts/gen_macrobench_stimulus.py [out.vcd] [extra_random_cycles]

Pair with:
  cargo run -r --features cuda --bin cuda_test -- \\
      build/macrobench.gv build/macrobench.gemparts \\
      build/macrobench_stim.vcd build/macrobench_out.vcd 40 \\
      --input-vcd-scope top --output-vcd-scope top
"""
import random
import sys

# macro_bench's top-level ports, with widths. Order does not matter for VCD,
# only that every port macro_bench declares as an input is driven.
INPUTS = [
    ("rst", 1),
    ("a_in", 27), ("b_in", 18), ("d_in", 27),
    ("srl_d", 1), ("srl_ce", 1), ("srl_a", 5),
    ("add_a", 32), ("add_b", 32), ("add_cin", 1),
]


def to_unsigned(v, w):
    """Two's-complement bit pattern of a possibly-negative int in width w."""
    return v & ((1 << w) - 1)


def bits(v, w):
    return "".join("1" if (v >> i) & 1 else "0" for i in range(w - 1, -1, -1))


def boundary_vectors(w):
    """(0, max-positive, min-negative, all-ones) as unsigned w-bit patterns."""
    return [
        0,
        to_unsigned((1 << (w - 1)) - 1, w),   # max positive
        to_unsigned(-(1 << (w - 1)), w),      # min negative
        to_unsigned(-1, w),                   # all-ones
    ]


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else "build/macrobench_stim.vcd"
    extra_random_cycles = int(sys.argv[2]) if len(sys.argv) > 2 else 24

    ids, nxt = {}, 34  # '!' is reserved for clk, matching gen_stimulus.py's convention
    lines = ["$timescale 1ns $end", "$scope module top $end",
             "$var wire 1 ! clk $end"]
    for nm, w in INPUTS:
        ids[nm] = chr(nxt); nxt += 1
        lines.append(f"$var wire {w} {ids[nm]} {nm} $end")
    lines += ["$upscope $end", "$enddefinitions $end"]

    st = {nm: 0 for nm, _ in INPUTS}
    st["rst"] = 1
    t = 0

    def emit():
        lines.append(f"#{t}")
        for nm, w in INPUTS:
            v = st[nm]
            lines.append(f"b{bits(v, w)} {ids[nm]}" if w > 1 else f"{v}{ids[nm]}")

    def edge():
        nonlocal t
        emit(); t += 10
        lines.append(f"#{t}"); lines.append("1!"); t += 10
        lines.append(f"#{t}"); lines.append("0!"); t += 10

    lines.append(f"#{t}"); lines.append("0!")
    emit(); t += 10

    # Hold reset for 2 cycles (all lane registers, and the SRL/carry state,
    # start from a known zero -- matches the PS's "Initialization Constraint").
    for _ in range(2):
        edge()
    st["rst"] = 0

    # --- Boundary-biased sweep -------------------------------------------
    # Cross the DSP pre-adder/multiplier/accumulator boundaries and sweep the
    # SRL read address across all 32 slots. srl_ce is held high throughout so
    # every cycle both shifts the SRL and produces a fresh accumulate.
    a_vecs = boundary_vectors(27)
    d_vecs = boundary_vectors(27)
    b_vecs = boundary_vectors(18)
    add_vecs = boundary_vectors(32)

    n = max(len(a_vecs), len(d_vecs), len(b_vecs), len(add_vecs), 32)
    for i in range(n):
        st["a_in"] = a_vecs[i % len(a_vecs)]
        st["d_in"] = d_vecs[i % len(d_vecs)]
        st["b_in"] = b_vecs[i % len(b_vecs)]
        st["add_a"] = add_vecs[i % len(add_vecs)]
        st["add_b"] = add_vecs[(i + 1) % len(add_vecs)]
        st["add_cin"] = i & 1
        st["srl_d"] = (i ^ (i >> 1)) & 1  # gray-code-ish toggle pattern
        st["srl_ce"] = 1
        st["srl_a"] = i % 32
        edge()

    # --- Deterministic pseudo-random tail ----------------------------------
    # Fills out the run length (report's headline numbers used 41 simulated
    # cycles; the sweep above already exceeds that, this just adds margin)
    # and exercises combinations the boundary sweep alone would not hit.
    rng = random.Random(0x600D6E5)
    for _ in range(extra_random_cycles):
        st["a_in"] = rng.getrandbits(27)
        st["d_in"] = rng.getrandbits(27)
        st["b_in"] = rng.getrandbits(18)
        st["add_a"] = rng.getrandbits(32)
        st["add_b"] = rng.getrandbits(32)
        st["add_cin"] = rng.getrandbits(1)
        st["srl_d"] = rng.getrandbits(1)
        st["srl_ce"] = 1
        st["srl_a"] = rng.getrandbits(5)
        edge()

    with open(out, "w") as f:
        f.write("\n".join(lines) + "\n")
    total_cycles = 2 + n + extra_random_cycles
    print(f"wrote {out}: {total_cycles} cycles "
          f"(2 reset + {n} boundary-sweep + {extra_random_cycles} random)")


if __name__ == "__main__":
    main()
