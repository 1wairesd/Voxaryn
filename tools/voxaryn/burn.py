#!/usr/bin/env python3
"""A CPU busy loop for N seconds (one core): the background load of the
frequency/SMT experiment in docs/research/p1-overhead.md. entity_bench.py's
--burners starts these itself; this is for running one by hand.

  py -3 tools/voxaryn/burn.py 60
"""
import sys, time

end = time.time() + (float(sys.argv[1]) if len(sys.argv) > 1 else 60.0)
x = 0
while time.time() < end:
    x += 1
