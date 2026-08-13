"""Scale check: how does this behave with a real intake, not four fixtures?

    python tests/loadtest.py 1000 /tmp/load-scratch

Generates N synthetic applicants (no packets — this measures the app, not
Tesseract), runs the pipeline, then times every page an advisor or professor
actually opens and reports the size of what came back. Page weight matters as
much as latency here: a route that returns 40 MB of HTML is "fast" on the
server and unusable in a browser.

Deliberately outside pytest — it takes minutes and writes a lot of junk.
"""

import os
import random
import sys
import time
import tracemalloc

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

AREAS = ["power systems", "VLSI", "machine learning", "robotics", "photonics",
         "signal processing", "controls", "computer architecture"]
FIRST = ["Aarav", "Priya", "Chen", "Maria", "Joonha", "Sofia", "Ravi", "Wei",
         "Ana", "Yusuf", "Nina", "Tomas", "Lena", "Omar", "Grace"]
LAST = ["Sharma", "Patel", "Liu", "Lopez", "Jun", "Rossi", "Mehta", "Chen",
        "Silva", "Khan", "Novak", "Muller", "Costa", "Haddad", "Okafor"]


def make_roster(path, n, seed=7):
    rng = random.Random(seed)
    cols = ["cas_id", "first_name", "last_name", "email", "college_name_0",
            "gpas_by_transcript_gpa_0", "designation_0", "local_status_0",
            "toefl_ibt_result_0", "gre_verbal_scaled_0", "gre_quantitative_scaled_0"]
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(",".join(cols) + "\n")
        for i in range(n):
            first, last = rng.choice(FIRST), rng.choice(LAST)
            fh.write(",".join([
                str(1000000000 + i),
                first, last, f"{first.lower()}.{last.lower()}{i}@example.edu",
                rng.choice(["State University", "Institute of Technology",
                            "National University"]),
                f"{rng.uniform(2.5, 4.0):.2f}",
                "Fall 2027 M.S. Electrical Engineering (ECEN)",
                rng.choice(["Complete", "In Review", "Awaiting Documents"]),
                str(rng.randint(90, 118)), str(rng.randint(140, 170)),
                str(rng.randint(140, 170)),
            ]) + "\n")
    return path


def attach_documents(outdir, fraction=0.35, seed=11):
    """Give some applicants an SOP and résumé, so the summary path is exercised
    (and the near-duplicate SOP check gets something to chew on)."""
    from src.documents import build_section
    from src.merge import upsert_source
    from src.store import Store, db_path_for
    rng = random.Random(seed)
    store = Store(db_path_for(outdir))
    ids = [e["cas_id"] for e in store.index()]
    picked = ids[:int(len(ids) * fraction)]
    for sid in picked:
        area = rng.choice(AREAS)
        sop = (f"My goal is to research {area} at Texas A&M University. "
               f"During my undergraduate work I built and measured systems in {area}, "
               "and I want to continue that in graduate school. The faculty there "
               f"publish the {area} work I want to do. " * 3)
        resume = ("Education\nState University\nBachelor of Science in Electrical "
                  "Engineering\nSkills: Python, Matlab, C++\nResearch Experience\n"
                  f"Built and characterised a {area} testbed over two semesters.\n"
                  "Course Project\nDesigned a controller and verified it in simulation.\n")
        def _apply(u, sop=sop, resume=resume):
            upsert_source(u, "sop", build_section("sop", sop, "s.txt"), file="s.txt")
            upsert_source(u, "resume", build_section("resume", resume, "r.txt"), file="r.txt")
            return u
        store.update(sid, _apply)
    return len(picked)


def timed(label, fn):
    t0 = time.perf_counter()
    out = fn()
    secs = time.perf_counter() - t0
    return label, secs, out


def main(n=500, scratch="/tmp/load-scratch"):
    from src import app as appmod
    from src import users as usersmod
    from src.run import reindex, run
    from src.store import Store, db_path_for

    os.makedirs(os.path.join(scratch, "raw"), exist_ok=True)
    usersmod.USERS_YAML = os.path.join(scratch, "users.yaml")   # no login for the run
    appmod.DATA = scratch
    appmod.app.config.update(TESTING=True)
    client = appmod.app.test_client()
    field_map = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             "config", "csv_field_map.yaml")

    roster = make_roster(os.path.join(scratch, "raw", "load.csv"), n)
    print(f"\n{n} applicants → {scratch}\n" + "=" * 64)

    tracemalloc.start()
    _, secs, summary = timed("ingest", lambda: run(roster, scratch, field_map))
    print(f"{'ingest':<28}{secs:7.2f}s   {summary['valid']} valid, "
          f"{summary['quarantined']} quarantined")

    _, secs, count = timed("documents", lambda: attach_documents(scratch))
    print(f"{'attach docs (35%)':<28}{secs:7.2f}s   {count} applicants")

    _, secs, _ = timed("reindex", lambda: reindex(scratch))
    print(f"{'reindex (re-render all)':<28}{secs:7.2f}s")

    store = Store(db_path_for(scratch))
    entries = store.index()
    ids = [e["cas_id"] for e in entries]

    # a realistic mid-review state: most marked, some swiped, some assigned
    rng = random.Random(3)
    for i, sid in enumerate(ids):
        status = "tbd" if i % 3 else rng.choice(["accept", "reject"])
        patch = {"review_status": status}
        if status == "tbd" and i % 5 == 0:
            patch["triage"] = "shortlist"
            patch["assigned_to"] = ["gratz-paul", "fan-zhiwen"][i % 2]
            if i % 10 == 0:
                patch["professor_status"] = "accept"
        store.update(sid, lambda u, p=patch: {**u, **p})

    current, peak = tracemalloc.get_traced_memory()
    print(f"{'peak memory (ingest+setup)':<28}{peak / 1e6:7.1f} MB")
    tracemalloc.stop()

    print("-" * 64)
    print(f"{'page':<28}{'time':>8}   {'size':>9}")
    print("-" * 64)
    pages = [("/ (dashboard)", "/"), ("/step/1", "/step/1"), ("/step/2", "/step/2"),
             ("/applicants", "/applicants"), ("/triage", "/triage"),
             ("/summaries", "/summaries"), ("/triage/shortlist", "/triage/shortlist"),
             ("/professors", "/professors"), ("/prof?as=gratz-paul", "/prof?as=gratz-paul"),
             ("/download/summaries", "/download/summaries"),
             ("/download/status.xlsx", "/download/status.xlsx"),
             ("/review-sheet", "/review-sheet")]
    worst = []
    for label, url in pages:
        tracemalloc.start()
        t0 = time.perf_counter()
        resp = client.get(url)
        secs = time.perf_counter() - t0
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        size = len(resp.data)
        flag = "  <-- heavy" if size > 3_000_000 or secs > 3 else ""
        print(f"{label:<28}{secs:7.2f}s   {size/1e6:7.2f} MB   {peak/1e6:6.0f} MB peak{flag}")
        worst.append((label, secs, size))

    print("-" * 64)
    slow = [w for w in worst if w[1] > 3]
    fat = [w for w in worst if w[2] > 3_000_000]
    if slow:
        print("SLOW (>3s):", ", ".join(f"{l} {s:.1f}s" for l, s, _ in slow))
    if fat:
        print("HEAVY (>3MB):", ", ".join(f"{l} {b/1e6:.1f}MB" for l, _, b in fat))
    if not slow and not fat:
        print("All pages under 3s and 3 MB.")
    return 0


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 500
    scratch = sys.argv[2] if len(sys.argv) > 2 else "/tmp/load-scratch"
    sys.exit(main(n, scratch))
