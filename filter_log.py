
import sys

log_file = "unittest.log"
try:
    with open(log_file, "r", encoding="utf-16") as f:
        lines = f.readlines()
except UnicodeError:
    try:
        with open(log_file, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
    except Exception:
        with open(log_file, "r", encoding="latin-1") as f:
             lines = f.readlines()

for i, line in enumerate(lines):
    if "Error" in line or "Traceback" in line or "FAIL" in line or "NameError" in line:
        print(f"--- Line {i} ---")
        # Print 5 lines before and 20 after
        start = max(0, i - 5)
        end = min(len(lines), i + 20)
        for j in range(start, end):
            print(lines[j].rstrip())
        print("-" * 20)
