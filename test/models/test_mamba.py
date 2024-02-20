import unittest
import pathlib
from tinygrad.helpers import CI
from tinygrad import Device
from examples.mamba import Mamba, generate
from transformers import AutoTokenizer

PROMPT = 'Why is gravity '
TOKENIZER = AutoTokenizer.from_pretrained("EleutherAI/gpt-neox-20b")
class TestMamba(unittest.TestCase):
  def test_mamba_130M(self):
    print("test_mamba_130M")
    OUT_130M = '''Why is gravity \nnot a good idea?\n\nA:'''
    model = Mamba.from_pretrained('130m')
    tinyoutput = generate(model, TOKENIZER, PROMPT, n_tokens_to_gen=10)
    self.assertEqual(OUT_130M, tinyoutput)
    del model
  def test_mamba_370M(self):
    OUT_370M = '''Why is gravity \nso important?\nBecause it's the only'''
    model = Mamba.from_pretrained('370m')
    tinyoutput = generate(model, TOKENIZER, PROMPT, n_tokens_to_gen=10)
    self.assertEqual(OUT_370M, tinyoutput)
    del model
if __name__ == '__main__':
  unittest.main()