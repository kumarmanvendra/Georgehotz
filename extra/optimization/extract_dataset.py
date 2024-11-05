#!/usr/bin/env python3
# extract asts from process replay artifacts
import os
from test.external.process_replay.process_replay import _pmap

LOGOPS = os.getenv("LOGOPS", "/tmp/sops")

def extract_ast(*args) -> bool:
  open(LOGOPS, "a").write(str(args[0]).replace("\n", "").replace(" ", "")+"\n")
  return args[-1]

if __name__ == "__main__":
  _pmap("kernel", extract_ast)
