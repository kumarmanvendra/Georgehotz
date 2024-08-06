#!/bin/bash

export PYTHONPATH="."
export MODEL="bert"
export DEFAULT_FLOAT="HALF" GPUS=6 BS=66 EVAL_BS=6

export BEAM=4
export BASEDIR="/raid/datasets/wiki"

echo "TODO: DISABLING DROPOUT - UNSET FOR REAL SUBMISSION RUN"
export DISABLE_DROPOUT=1 # TODO: Unset flag for real submission run.

export BENCHMARK=10 DEBUG=2

python3 examples/mlperf/model_train.py
