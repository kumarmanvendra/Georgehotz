#!/usr/bin/env python3
# compare kernels created by HEAD against master
from collections import defaultdict
import difflib, pickle, multiprocessing, os, logging, sqlite3, requests, io, zipfile, glob, re
from tabulate import tabulate
from typing import DefaultDict, List, Dict
from tinygrad.codegen.kernel import Kernel
from tinygrad.device import Device
from tinygrad.helpers import Context, ContextVar, colored, db_connection, VERSION, getenv, tqdm
from tinygrad.ops import LazyOp

# *** process replay settings
PAGE_SIZE = 100
REF = os.getenv("GITHUB_REF_NAME", "")
MAX_DIFF_PCT = getenv("PROCESS_REPLAY_MAX_DIFF_PCT", 20)
RUN_ID = os.getenv("GITHUB_RUN_ID", "HEAD")
TABLE_NAME = f"process_replay_{RUN_ID}_{VERSION}"
REF_TABLE_NAME = f"process_replay_master_{VERSION}"
ASSERT_DIFF = getenv("ASSERT_PROCESS_REPLAY", int((k:="[run_process_replay]") in os.getenv("COMMIT_MESSAGE", k) or k in os.getenv("PR_TITLE", k)))
if REF == "master": ASSERT_DIFF = False
COMPARE_SCHEDULE = getenv("COMPARE_SCHEDULE", int((k:="[compare_schedule]") in os.getenv("COMMIT_MESSAGE", "") or k in os.getenv("PR_TITLE", "")))
SKIP_PROCESS_REPLAY = (k:="[skip_process_replay]") in os.getenv("COMMIT_MESSAGE", "") or k in os.getenv("PR_TITLE", "")
early_stop = multiprocessing.Event()
logging.basicConfig(level=logging.INFO, format='%(message)s')
# *** github settings
BASE_URL = f"https://api.github.com/repos/{os.getenv('GITHUB_REPOSITORY', 'tinygrad/tinygrad')}"
GH_HEADERS = {"Authorization": f"Bearer {os.getenv('GH_TOKEN', '')}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}

def process_replay(offset:int, ref_schedule:List[LazyOp]):
  if early_stop.is_set(): return
  conn = db_connection()
  cur = conn.cursor()
  cur.execute(f"SELECT val FROM '{TABLE_NAME}' LIMIT ? OFFSET ?", (PAGE_SIZE, offset))
  changed = 0
  for row in cur.fetchall():
    ast, applied_opts = None, None
    # try unpickle and linearize
    try:
      ast, opts, applied_opts, name, compare_src, ctx = pickle.loads(row[0])
      with Context(**{k:v for k,v in ctx.items() if k in ContextVar._cache and k != "DEBUG"}):
        k = Kernel(ast, opts=opts)
        for opt in applied_opts: k.apply_opt(opt)
        good_src = k.opts.render(name, k.linearize().uops)
    except Exception as e:
      logging.warning("FAILED TO RECREATE KERNEL")
      logging.info(ast)
      logging.info(applied_opts)
      logging.info(e)
      if ASSERT_DIFF: raise e
      continue
    # try compare
    if COMPARE_SCHEDULE and ast not in ref_schedule:
      with Context(**{k:v for k,v in ctx.items() if k in ContextVar._cache and k != "DEBUG"}):
        print(opts.render(name, Kernel(ast, opts=opts).linearize().uops))
      continue
    try: assert compare_src == good_src
    except AssertionError as e:
      changed += 1
      logging.info("PROCESS REPLAY DETECTED CHANGE")
      logging.info(ast)
      logging.info(applied_opts)
      diff = list(difflib.unified_diff(good_src.splitlines(), compare_src.splitlines()))
      for line in diff:
        logging.info(colored(line, "red" if line.startswith("-") else "green" if line.startswith("+") else None))
      if ASSERT_DIFF: raise e
      if changed > MAX_DIFF_PCT:
        logging.warning(f"detected changes in over {MAX_DIFF_PCT}% of kernels. skipping further diff generation.")
        early_stop.set()
        break
  conn.commit()
  cur.close()

def get_ref_schedule(offset:int, ref_schedule):
  conn = sqlite3.connect("/tmp/process_replay/process_replay.db")
  cur = conn.cursor()
  cur.execute(f"SELECT val FROM '{REF_TABLE_NAME}' LIMIT ? OFFSET ?", (PAGE_SIZE, offset))
  for row in cur.fetchall(): ref_schedule.append(pickle.loads(row[0])[0])
  conn.commit()
  cur.close()

def download_artifact(run_id:str, name:str, dest:str):
  res = requests.get(f"{BASE_URL}/actions/runs/{run_id}/artifacts?name={name}", headers=GH_HEADERS)
  assert res.status_code == 200, f"download failed {res.status_code} {res.json()}"
  download_url = res.json()["artifacts"][0]["archive_download_url"]
  res = requests.get(download_url, headers=GH_HEADERS)
  assert res.status_code == 200, f"download failed {res.status_code}"
  with io.BytesIO(res.content) as zip_content:
    with zipfile.ZipFile(zip_content, "r") as zip_ref: zip_ref.extractall(dest)

def parse_benchmark(fp:str):
  ret: DefaultDict[str, List] = defaultdict(list)
  with open(fp) as f:
    for line in f.read().splitlines():
      for v,k in dict(re.findall(r'(\d+\.\d+|\d+)\s*([a-zA-Z\s]+?)(?:,|$)', line)).items(): ret[k.strip()].append(float(v))
  return ret

if __name__ == "__main__":
  if SKIP_PROCESS_REPLAY:
    logging.info("skipping process replay.")
    exit(0)

  # *** speed diff (for benchmarks)
  if REF == "update_benchmark":
    name = {"testmacbenchmark": "Mac", "testnvidiabenchmark": "NVIDIA", "testmorenvidiabenchmark": "NVIDIA Training",
            "testamdbenchmark": "AMD", "testmoreamdbenchmark": "AMD Training"}[os.environ["GITHUB_JOB"]]
    res = requests.get(f"{BASE_URL}/actions/workflows/benchmark.yml/runs?per_page=1&branch=master", headers=GH_HEADERS)
    ref_run_id = res.json()["workflow_runs"][0]["id"]
    print(f"comparing speed for {RUN_ID} against {ref_run_id}")
    download_artifact(ref_run_id, f"Speed ({name})", "/tmp/timing_ref")
    download_artifact(RUN_ID, f"Speed ({name})", "/tmp/timing_compare")
    for fp in glob.glob("/tmp/timing_ref/*.txt"):
      print(fp.split('/')[-1].split('.')[0])
      ref = parse_benchmark(fp)
      compare = parse_benchmark(fp.replace("timing_ref", "timing_compare"))
      diff: Dict[str, List[str]] = {}
      avg_diff: Dict[str, float] = {}
      for key, ref_vals in ref.items():
        vals = [(comp - ref) / ref * 100 for ref,comp in zip(ref_vals, compare[key]) if ref != 0]
        if not vals or key == "epochs": continue
        avg_diff[key] = sum(vals) / len(vals)
        diff[key] = [colored(f"{x:7.2f}%", 'yellow' if x == 0 else 'red' if x < 0.75 else 'green' if x > 1.15 else 'yellow') for x in vals[:5]]
      print(tabulate(diff, headers='keys'))

  # *** schedule diff
  ref_schedule = multiprocessing.Manager().list()
  if COMPARE_SCHEDULE:
    logging.info("fetching process replay reference")
    # TODO: make this run_id dynamic
    download_artifact("10093148840", f"process_replay_{Device.DEFAULT.lower()}.db", "/tmp/process_replay")
    ref_conn = sqlite3.connect("/tmp/process_replay/process_replay.db")
    row_count = ref_conn.execute(f"select count(*) from '{REF_TABLE_NAME}'").fetchone()[0]
    processes = []
    for i in tqdm(range(0, row_count, PAGE_SIZE)):
      processes.append(p:=multiprocessing.Process(target=get_ref_schedule, args=(i, ref_schedule)))
      p.start()
    for p in processes: p.join()
    ref_conn.close()
  conn = db_connection()
  cur = conn.cursor()

  # *** kernel diff
  try: row_count = cur.execute(f"select count(*) from '{TABLE_NAME}'").fetchone()[0]
  except sqlite3.OperationalError:
    logging.warning(f"{TABLE_NAME} isn't accessible in master, did DB_VERSION change?")
    exit(0)
  conn.commit()
  cur.close()
  processes = []
  for i in tqdm(range(0, row_count, PAGE_SIZE)):
    processes.append(p:=multiprocessing.Process(target=process_replay, args=(i, ref_schedule)))
    p.start()
  for p in processes: p.join()
