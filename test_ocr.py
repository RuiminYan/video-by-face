"""
OCR Pipeline Regression Test
Run: python test_ocr.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from ocr_timer import readTimer

VIDEO_DIR = Path(__file__).parent / "video"

GOOD = VIDEO_DIR / "good"
FAIL = VIDEO_DIR / "fail"

def run_tests():
    passed = 0
    failed = 0
    known_fail = 0

    print("=" * 50)
    print("  OCR Timer Regression Test")
    print("=" * 50)

    if GOOD.exists():
        print(f"\n[GOOD] Expected to pass ({GOOD}):")
        for v in sorted(GOOD.glob("*.[Mm][Pp]4")):
            r = readTimer(v)
            ok = r == v.stem
            status = "OK  " if ok else "FAIL"
            print(f"  [{status}] {v.name}: {r} (expect: {v.stem})")
            if ok:
                passed += 1
            else:
                failed += 1

    if FAIL.exists():
        print(f"\n[FAIL] Known failures ({FAIL}):")
        for v in sorted(FAIL.glob("*.[Mm][Pp]4")):
            r = readTimer(v)
            ok = r == v.stem
            status = "FIXED" if ok else "still FAIL"
            print(f"  [{status}] {v.name}: {r} (expect: {v.stem})")
            known_fail += 1

    print(f"\n{'=' * 50}")
    print(f"  Passed : {passed}")
    print(f"  Failed : {failed}")
    print(f"  Known  : {known_fail} (in fail/)")
    if failed == 0:
        print("  ALL GOOD ✓")
    print("=" * 50)

if __name__ == "__main__":
    run_tests()
