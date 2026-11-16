"""Run an explicit Entity correctness contract using the existing driver."""

import sys

try:
    from . import entity_bench
except ImportError:
    import entity_bench


def main():
    if "--scenarios" not in sys.argv or "--expect" not in sys.argv:
        print("INFRA ERROR: explicit --scenarios and --expect required", file=sys.stderr)
        return 2
    sys.argv.insert(1, "verify")
    return entity_bench.main()


if __name__ == "__main__":
    sys.exit(main())
