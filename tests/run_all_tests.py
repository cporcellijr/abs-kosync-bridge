#!/usr/bin/env python3
"""
Test runner for all dependency injection based unit tests.
"""

import subprocess
import sys
from pathlib import Path

def run_test_file(test_file, description):
    """Run a single test file."""
    print(f"\n🧪 Running {description}")
    print("=" * 60)

    try:
        # Run from the project root directory (parent of tests/)
        project_root = Path(__file__).parent.parent

        result = subprocess.run([
            sys.executable, test_file
        ], cwd=project_root, env={'PYTHONPATH': str(project_root)},
        capture_output=True, text=True)

        if result.returncode == 0:
            print(result.stderr)  # unittest output goes to stderr
            print(f"✅ {description} PASSED")
            return True
        else:
            print(f"❌ {description} FAILED")
            print("STDERR:")
            print(result.stderr)
            if result.stdout:
                print("STDOUT:")
                print(result.stdout)
            return False

    except Exception as e:
        print(f"❌ {description} FAILED with exception: {e}")
        return False

def main():
    """Run all dependency injection unit tests."""
    print("🎯 Dependency Injection Unit Test Suite")
    print("=" * 60)
    print("Testing all sync scenarios using dependency injection")

    # Automatically discover all test files starting with "test_" in current directory
    tests_dir = Path(__file__).parent  # tests/ directory
    test_files = []

    if tests_dir.exists():
        for test_file in sorted(tests_dir.glob("test_*.py")):
            # Generate description from filename
            description = test_file.stem.replace("test_", "").replace("_", " ").title()
            if description.endswith("Unittest"):
                description = description.replace("Unittest", "")
            description = description.strip() + " Scenario"
            # Use relative path from project root
            relative_path = f"tests/{test_file.name}"
            test_files.append((relative_path, description))

    if not test_files:
        print("⚠️  No test files found in tests/ directory starting with 'test_'")
        return False

    print(f"📁 Found {len(test_files)} test file(s):")
    for test_file, desc in test_files:
        print(f"   • {test_file} - {desc}")
    print()

    results = {}
    passed = 0
    total = len(test_files)

    for test_file, description in test_files:
        # Check if file exists relative to project root
        project_root = Path(__file__).parent.parent
        full_path = project_root / test_file
        if full_path.exists():
            success = run_test_file(test_file, description)
            results[description] = success
            if success:
                passed += 1
        else:
            print(f"⚠️  {test_file} not found, skipping {description}")
            results[description] = None

    print("\n" + "=" * 60)
    print("🎯 FINAL TEST SUMMARY")
    print("=" * 60)

    for description, result in results.items():
        if result is True:
            print(f"✅ {description}")
        elif result is False:
            print(f"❌ {description}")
        else:
            print(f"⚠️  {description} (SKIPPED)")

    print(f"\n📊 Results: {passed}/{total} tests passed")

    if passed == total:
        print("\n🎉 ALL TESTS PASSED! 🎉")
        return True
    else:
        print(f"\n❌ {total - passed} test(s) failed. Please review the failures above.")
        return False

if __name__ == '__main__':
    success = main()
    sys.exit(0 if success else 1)
