"""V12.5 regression tests — locks in the two Major review fixes.

1. EmergencyController.trigger() must honor a later flatten=True even if the
   kill switch is already active (previously it early-returned).
2. ProcessLock.release() must only remove a lockfile this process owns.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

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


class _MockBroker:
    def __init__(self):
        self.cancels = 0
        self.closes = 0

    def cancel_all_orders(self):
        self.cancels += 1

    def close_all_positions(self):
        self.closes += 1


def test_emergency_repeat_flatten():
    from risk.emergency import EmergencyController
    b = _MockBroker()
    state = tempfile.mktemp(suffix=".json")
    ec = EmergencyController(broker=b, state_file=state)
    ec.trigger("kill only", flatten=False)
    check("#1 first kill cancels orders, no flatten", b.cancels == 1 and b.closes == 0)
    ec.trigger("kill + flatten", flatten=True)
    check("#1 later flatten honored while already active", b.closes == 1)
    check("#1 cancel runs on every trigger", b.cancels == 2)


def test_process_lock_release_ownership():
    from ktrade_runtime.process_lock import ProcessLock
    lp = tempfile.mktemp(suffix=".lock")

    lock = ProcessLock(lp)
    check("#3 acquire succeeds", lock.acquire() is True)
    # foreign owner takes over the file; our release must NOT delete it
    with open(lp, "w") as fh:
        fh.write("424242")
    lock.release()
    check("#3 release leaves a foreign-owned lock intact", os.path.exists(lp))
    if os.path.exists(lp):
        os.remove(lp)

    # our own lock is removed normally
    lock2 = ProcessLock(lp)
    lock2.acquire()
    lock2.release()
    check("#3 release removes our own lock", not os.path.exists(lp))

    # a stale (dead-PID) lock is reclaimed on acquire
    with open(lp, "w") as fh:
        fh.write("999999")
    lock3 = ProcessLock(lp)
    check("#3 stale dead-PID lock is reclaimed", lock3.acquire() is True)
    lock3.release()


if __name__ == "__main__":
    test_emergency_repeat_flatten()
    test_process_lock_release_ownership()
    print(f"\n==== {_passed} passed, {_failed} failed ====")
    sys.exit(1 if _failed else 0)
