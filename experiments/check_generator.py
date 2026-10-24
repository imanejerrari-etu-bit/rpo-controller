"""
Check that the workload generator really delivers the requested rate.

Runs the TokenBucket of experiments/workload.py in isolation (no database) at three rates, with a
simulated 2 ms of per-operation client work, and prints achieved/requested.  The corrected
(deadline-scheduling) limiter gives 1.00x; the old accumulate-tokens limiter overshoots by 1.4-2.0x.

Usage (repo root):  python -m experiments.check_generator        (copy this file to experiments/)
"""
import time
from experiments.workload import TokenBucket


def achieved(rate, secs=8.0, work_ms=2.0):
    b = TokenBucket(rate)
    n, t0 = 0, time.perf_counter()
    while time.perf_counter() - t0 < secs:
        b.acquire()
        t = time.perf_counter()
        while time.perf_counter() - t < work_ms / 1000:
            pass
        n += 1
    return n / (time.perf_counter() - t0)


if __name__ == "__main__":
    ok = True
    print("requested/s  achieved/s  ratio")
    for rate in (25, 100, 250):
        a = achieved(rate)
        ok &= abs(a / rate - 1) < 0.03
        print(f"{rate:>10}  {a:>10.1f}  {a / rate:>5.2f}x")
    print("\nOK: the generator delivers the requested rate" if ok else
          "\nFAIL: the generator overshoots its target. Replace experiments/workload.py by the corrected version "
          "(code_snapshots/workload.py) before any measurement.")
