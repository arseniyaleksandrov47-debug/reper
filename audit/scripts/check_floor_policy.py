"""Historical entry point; fixed floor policies have been removed."""
from pathlib import Path
import runpy
import sys

if __name__ == "__main__":
    if "--live-profiles" in sys.argv:
        runpy.run_path(str(Path(__file__).with_name("check_adaptive_floor_policy.py")), run_name="__main__")
    else:
        print("Floor policies are now inferred from context. Live check: python audit/scripts/check_adaptive_floor_policy.py")
