import time, random, unittest, sys
from tqdm import tqdm
from unittest.mock import patch
from io import StringIO
from collections import namedtuple
from tinygrad.helpers import tinytqdm

random.seed(1337)
class TestProgressBarOutput(unittest.TestCase):
  def _compare_bars(self, bar1, bar2, cmp_prog=False):
    prefix1, prog1, suffix1 = bar1.split("|")
    prefix2, prog2, suffix2 = bar2.split("|")
    self.assertEqual(prefix1, prefix2)
    self.assertEqual(suffix1, suffix2)

    diff = sum([1 for c1, c2 in zip(prog1, prog2) if c1 == c2]) # allow 1 char diff (due to tqdm special chars)
    self.assertTrue(not cmp_prog or diff <= 1)

  @patch('sys.stdout', new_callable=StringIO)
  @patch('shutil.get_terminal_size')
  def test_tqdm_output_iter_e2e(self, mock_terminal_size, mock_stdout):
    for _ in range(10):
      total, ncols = random.randint(5, 30), random.randint(80, 240)
      mock_terminal_size.return_value = namedtuple(field_names='columns', typename='terminal_size')(ncols)
      mock_stdout.truncate(0)

      # compare bars at each iteration (only when tinytqdm bar has been updated)
      for n in (bar := tinytqdm(range(total), desc="Test: ")):
        time.sleep(0.01)
        if bar.cnt % bar.skip != 0: continue
        tinytqdm_output = mock_stdout.getvalue().split("\r")[-1].rstrip()
        iters_per_sec = float(tinytqdm_output.split("it/s")[-2].split(" ")[-1]) if n>0 else 0
        elapsed = n/iters_per_sec if n>0 else 0
        tqdm_output = tqdm.format_meter(n=n, total=total, elapsed=elapsed, ncols=ncols, prefix="Test")
        self._compare_bars(tinytqdm_output, tqdm_output)

      # compare final bars
      tinytqdm_output = mock_stdout.getvalue().split("\r")[-1].rstrip()
      iters_per_sec = float(tinytqdm_output.split("it/s")[-2].split(" ")[-1]) if n>0 else 0
      elapsed = total/iters_per_sec if n>0 else 0
      tqdm_output = tqdm.format_meter(n=total, total=total, elapsed=elapsed, ncols=ncols, prefix="Test")
      self._compare_bars(tinytqdm_output, tqdm_output)

  @patch('sys.stdout', new_callable=StringIO)
  @patch('shutil.get_terminal_size')
  def test_tqdm_output_custom_e2e(self, mock_terminal_size, mock_stdout):
    for _ in range(10):
      total, ncols = random.randint(10000, 100000), random.randint(80, 120)
      mock_terminal_size.return_value = namedtuple(field_names='columns', typename='terminal_size')(ncols)
      mock_stdout.truncate(0)

      # compare bars at each iteration (only when tinytqdm bar has been updated)
      bar = tinytqdm(total=total, desc="Test: ")
      n = 0
      while n < total:
        time.sleep(0.01)
        incr = (total // 10) + random.randint(0, 100)
        if n + incr > total: incr = total - n
        bar.update(incr, close=n+incr==total)
        n += incr
        if bar.cnt % bar.skip != 0: continue

        tinytqdm_output = mock_stdout.getvalue().split("\r")[-1].rstrip()
        iters_per_sec = float(tinytqdm_output.split("it/s")[-2].split(" ")[-1]) if n>0 else 0
        elapsed = n/iters_per_sec if n>0 else 0
        tqdm_output = tqdm.format_meter(n=n, total=total, elapsed=elapsed, ncols=ncols, prefix="Test")
        sys.stderr.write(f"{tinytqdm_output}\n{tqdm_output}\n\n")

      # compare final bars
      tinytqdm_output = mock_stdout.getvalue().split("\r")[-1].rstrip()
      iters_per_sec = float(tinytqdm_output.split("it/s")[-2].split(" ")[-1]) if n>0 else 0
      elapsed = total/iters_per_sec if n>0 else 0
      tqdm_output = tqdm.format_meter(n=total, total=total, elapsed=elapsed, ncols=ncols, prefix="Test")
      self._compare_bars(tinytqdm_output, tqdm_output)

def test_tqdm_perf():
  st = time.perf_counter()
  for _ in tqdm(range(100)): time.sleep(0.01)

  tqdm_time = time.perf_counter() - st

  st = time.perf_counter()
  for _ in tinytqdm(range(100)): time.sleep(0.01)
  tinytqdm_time = time.perf_counter() - st

  assert tinytqdm_time < 1.1 * tqdm_time

def test_tqdm_perf_high_iter():
  st = time.perf_counter()
  for _ in tqdm(range(10^7)): pass
  tqdm_time = time.perf_counter() - st

  st = time.perf_counter()
  for _ in tinytqdm(range(10^7)): pass
  tinytqdm_time = time.perf_counter() - st

  assert tinytqdm_time < 4 * tqdm_time

if __name__ == '__main__':
  unittest.main()
