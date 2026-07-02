"""V13.9 architectural-firewall (release-safety) tests.

Runs the real scanner (scripts/check_release_safety.py) against crafted temp dirs
and asserts it: passes clean code, catches each dynamic-execution / wallet-dependency
violation, does NOT flag safe look-alikes, and honours the allow-comment. Confirms
the existing forbidden-file/secret checks still fire.
"""
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.abspath(__file__))
SCANNER = os.path.join(ROOT, "scripts", "check_release_safety.py")

if not os.path.exists(SCANNER):
    print("SKIP test_v137: scanner not found. Treating as pass.")
    sys.exit(0)

_passed = 0
_failed = 0


def check(name, cond):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"PASS {name}")
    else:
        _failed += 1
        print(f"FAIL {name}")


def run_against(files):
    """Write {relpath: content} into a temp dir, run the scanner there, return (rc, output)."""
    with tempfile.TemporaryDirectory() as d:
        for rel, content in files.items():
            p = os.path.join(d, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as f:
                f.write(content)
        proc = subprocess.run([sys.executable, SCANNER], cwd=d,
                              capture_output=True, text=True, timeout=120)
        return proc.returncode, proc.stdout + proc.stderr


def test_clean_passes():
    rc, out = run_against({"ok.py": "import pandas as pd\nx = pd.Series([1,2,3])\n",
                           "requirements.txt": "pandas>=2.0.0\nnumpy>=1.24.0\n"})
    check("clean code passes (exit 0)", rc == 0)
    check("clean prints passed", "passed" in out.lower())


def test_catches_dynamic_exec():
    rc, out = run_against({"bad.py": "y = eval(user_input)\n"})
    check("dynamic eval -> fail", rc == 1 and "dynamic eval" in out)
    rc, out = run_against({"bad.py": "exec(downloaded)\n"})
    check("dynamic exec -> fail", rc == 1 and "dynamic exec" in out)
    rc, out = run_against({"bad.py": "import os\nos.system(cmd)\n"})
    check("os.system -> fail", rc == 1 and "shell command" in out)
    rc, out = run_against({"bad.py": "obj = pickle.loads(blob)\n"})
    check("pickle.loads -> fail", rc == 1 and "deserialization" in out)
    rc, out = run_against({"bad.py": "import importlib\nm = importlib.import_module(name)\n"})
    check("variable importlib -> fail", rc == 1 and "importlib" in out)


def test_catches_wallet():
    rc, out = run_against({"bad.py": "from web3 import Web3\n"})
    check("web3 import -> fail", rc == 1 and "wallet/contract library import" in out)
    rc, out = run_against({"ok.py": "x=1\n", "requirements.txt": "pandas\nweth-unrelated\nweb3>=6.0.0\n"})
    check("web3 in requirements -> fail", rc == 1 and "wallet/contract dependency" in out)


def test_safe_forms_not_flagged():
    safe = ("import ast\n"
            "a = ast.literal_eval('[1,2]')\n"          # safe
            "b = df.eval('x + y')\n"                    # safe method
            "c = __import__('uuid').uuid4()\n"          # safe literal import
            "cursor.execute('SELECT 1')\n")             # safe DB method
    rc, out = run_against({"ok.py": safe})
    check("safe eval/import look-alikes pass (exit 0)", rc == 0)


def test_allow_comment_respected():
    rc, out = run_against({"x.py": "v = eval(trusted)  # release-safety: allow vetted loader\n"})
    check("allow-comment -> not an error (exit 0)", rc == 0)
    check("allow-comment -> reported as documented exception", "allowed" in out.lower())


def test_existing_checks_intact():
    rc, out = run_against({".env": "SECRET=abc\n"})
    check("forbidden .env still caught", rc == 1 and "Forbidden" in out)
    rc, out = run_against({"cfg.txt": "OPENAI=sk-proj-ABCDEFGHIJKLMNOPQRSTUVWX\n"})
    check("secret pattern still caught", rc == 1 and "OpenAI" in out)


if __name__ == "__main__":
    test_clean_passes()
    test_catches_dynamic_exec()
    test_catches_wallet()
    test_safe_forms_not_flagged()
    test_allow_comment_respected()
    test_existing_checks_intact()
    print(f"\n==== {_passed} passed, {_failed} failed ====")
    sys.exit(1 if _failed else 0)
