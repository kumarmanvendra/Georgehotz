export PYTHONPATH="."
export MODEL="resnet"
export DEFAULT_FLOAT="HALF" GPUS=6 BS=1536 EVAL_BS=48 LR=7

export SPLIT_REDUCEOP=0 LAZYCACHE=0 RESET_STEP=0

export TRAIN_BEAM=2 IGNORE_JIT_FIRST_BEAM=1 BEAM_UOPS_MAX=1200 BEAM_UPCAST_MAX=64 BEAM_LOCAL_MAX=256 BEAM_MIN_PROGRESS=50

export BENCHMARK=10 DEBUG=2

python3 examples/mlperf/model_train.py