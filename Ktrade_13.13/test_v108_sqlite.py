"""v10.8 SQLite pillar tests: transactional risk-state + emergency ledger, concurrency, fallback."""
import sys, os, tempfile, importlib.util, threading
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)

def load(p, n):
    spec = importlib.util.spec_from_file_location(n, os.path.join(HERE, p))
    m = importlib.util.module_from_spec(spec); sys.modules[n] = m; spec.loader.exec_module(m); return m

DB = load("data/ktrade_db.py", "ktrade_db")

P, F = [], []
def check(n, c): (P if c else F).append(n); print(("PASS " if c else "FAIL ")+n)

with tempfile.TemporaryDirectory() as d:
    store = DB.KTradeSQLiteStore(os.path.join(d, "t.db"))

    # risk_state upsert round-trip
    check("risk_state save returns True", store.save_risk_state("risk", {"approved_today": 5, "kill_active": False}))
    check("risk_state loads back", store.load_risk_state("risk").get("approved_today") == 5)
    store.save_risk_state("risk", {"approved_today": 9})
    check("risk_state upsert overwrites", store.load_risk_state("risk").get("approved_today") == 9)
    check("missing key -> empty dict", store.load_risk_state("nope") == {})

    # emergency ledger append + latest
    store.save_emergency_state(True, "VIX 55", "2026-06-23T00:00:00")
    store.save_emergency_state(False, "", "2026-06-23T00:05:00")
    latest = store.load_latest_emergency_state()
    check("emergency ledger returns most recent", latest["active"] is False)
    store.save_emergency_state(True, "flash crash", "2026-06-23T00:10:00")
    check("emergency ledger newest = active", store.load_latest_emergency_state()["active"] is True)

    # concurrency: many threads writing should not corrupt / deadlock
    errs = []
    def worker(i):
        try:
            for j in range(20):
                store.save_risk_state(f"k{i}", {"n": j})
        except Exception as e:
            errs.append(e)
    ts = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    [t.start() for t in ts]; [t.join() for t in ts]
    check("concurrent writes: no errors", not errs)
    check("concurrent writes: all keys present", all(store.load_risk_state(f"k{i}").get("n") == 19 for i in range(8)))

# Wiring: state_store + emergency use SQLite by default and round-trip across instances
os.environ["KTRADE_STATE_BACKEND"] = "sqlite"
with tempfile.TemporaryDirectory() as d:
    os.chdir(d)  # ktrade.db will be created here
    # fresh import of modules that bind get_store
    for mod in ("ktrade_db", "state_store", "emergency"):
        sys.modules.pop(mod, None)
    DB2 = load(os.path.join(HERE, "data/ktrade_db.py"), "ktrade_db") if False else load("data/ktrade_db.py", "ktrade_db")
    SS = load("risk/state_store.py", "state_store")
    EM = load("risk/emergency.py", "emergency")

    st = SS.RiskStateStore()
    st.save({"approved_today": 3})
    st2 = SS.RiskStateStore()
    check("state_store persists via SQLite across instances", st2.load().get("approved_today") == 3)

    class FB:
        def cancel_all_orders(self): pass
        def close_all_positions(self): pass
    ec = EM.EmergencyController(broker=FB())
    ec.trigger("test kill", flatten=True)
    ec2 = EM.EmergencyController(broker=FB())
    check("emergency persists via SQLite ledger across instances", ec2.active() and "test kill" in ec2.reason)
    ec2.reset()
    ec3 = EM.EmergencyController(broker=FB())
    check("emergency reset persisted via ledger", not ec3.active())

os.chdir(HERE)
print("\n==== %d passed, %d failed ====" % (len(P), len(F)))
if F: print("FAILED:", F); sys.exit(1)
