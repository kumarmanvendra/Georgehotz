import sys, math, string, argparse, difflib
from examples.whisper import make_initial_prompt, transcribe_wav, WHISPER_MODELS, load_whisper_model
from extra.datasets.librispeech import load_dataset, BASEDIR
from examples.mlperf.metrics import word_error_rate
import numpy as np

WER = {}
predictions = {}

def eval_whisper(model, start, end, verbose, dataset):
  diff = difflib.Differ()
  for c in dataset[start:end]:
    fn = BASEDIR / c["fname"]
    predicted = "".join(transcribe_wav(fn, model, make_initial_prompt(model))).translate(str.maketrans("", "", string.punctuation)).lower()
    transcript = c["transcript"].translate(str.maketrans("", "", string.punctuation))
    current_wer = word_error_rate([predicted], [transcript])[0]
    WER[model.name] = np.append(WER[model.name], current_wer)
    predictions[model.name].append(predicted)
    
    if (verbose > 1 and predicted != transcript) or (verbose > 2):
      print("-" * 128, f"{fn.stem}\n", sep="\n")
      sys.stdout.writelines(list(diff.compare([predicted + "\n"], [transcript + "\n"])))
      print(f"\nSample word error rate: {(current_wer*100):.2f}")

if __name__ == "__main__":
  parser = argparse.ArgumentParser(description='Evaluate whisper on librispeech', formatter_class=argparse.ArgumentDefaultsHelpFormatter)
  parser.add_argument('--models', type=str, default=None, nargs="+", help="Which model to evaluate, if not specified, will use eval all available models")
  parser.add_argument('--verbose', type=int, default=1, help="Verbosity level, 0: only print final WER, 1: print WER for each model eval 2: print failed samples, 3: print all samples")
  parser.add_argument("--num-samples", type=int, default=None, help="Number of samples to run on")
  parser.add_argument("--step-size", type=int, default=None, help="Each step it runs all models on all samples in a step, ")
  parser.add_argument("--dataset", type=str, default="test-clean", help="Which dataset to use")
  args = parser.parse_args()

  models = WHISPER_MODELS if args.models is None else {x:WHISPER_MODELS[x] for x in args.models if x in WHISPER_MODELS}
  # large-v2 and large are the same model
  if "large" in models:
    models["large-v2"] = models["large"]
    del models["large"]
  dataset = load_dataset(args.dataset)
  num_samples = len(dataset) if args.num_samples is None else min(args.num_samples, len(dataset))
  step_size = num_samples if args.step_size is None else min(args.step_size, num_samples)
  WER = {j:np.array([]) for j in models}
  predictions = {j:[] for j in models}

  print("Running eval on  the following models:", list(models.keys()))
  for i in range(0, num_samples, step_size):
    for j in models:
      print(f"evaluating {j} on {step_size} sample(s)")
      model = load_whisper_model(j)
      eval_whisper(model, i, max(i+step_size, num_samples), verbose=args.verbose, dataset=dataset)
      print("-"*128)
      if args.verbose > 0:
        print(f"Results of {j} after {i+step_size} samples: {np.average(WER[j]*100):.2f}")
        print("-"*128)
      del model
  print("Results of a run:")
  gt = [i["transcript"] for i in dataset[:num_samples]]
  for (k,v) in predictions.items():
    print(f"{k}: WER is {(word_error_rate(gt,v)[0]*100):.2f}")
    print(f"{k}: {np.count_nonzero(v)} out of {len(v)} samples have mistakes, {(np.count_nonzero(v)/len(v)*100):.2f}%")