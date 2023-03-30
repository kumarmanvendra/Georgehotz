import struct
from models.efficientnet import EfficientNet
from tinygrad.tensor import Tensor
from extra.utils import fetch
import ast

def compile_net(run, special_names):
  # functions that run the net
  functions = {}
  bufs = {}
  bufnum = 0
  statements = []
  bufs_to_save = {}
  for fxn,args in run.jit_cache:
    functions[fxn.name] = fxn.prg   # NOTE: this assumes all with the same name are the same
    cargs = []
    for i,arg in enumerate(args):
      key = id(arg)
      if key not in bufs:
        if key in special_names:
          bufs[key] = (special_names[key], len(arg._buf))
        else:
          bufs[key] = (f"buf_{bufnum}", len(arg._buf))
          bufnum += 1
          if i > 0: bufs_to_save[bufs[key][0]] = arg   # if first usage of a buffer is not an output, and it's not a special name
      if bufs[key][0] in bufs_to_save:
        cargs.append(f"&{bufs[key][0]}")
      else:
        cargs.append(f"&mut {bufs[key][0]}")
    statements.append(f"{fxn.name}({', '.join(cargs)});")

  return functions, statements, bufs, bufs_to_save

if __name__ == "__main__":
  model = EfficientNet(0)
  model.load_from_pretrained()

  from tinygrad.jit import TinyJit
  @TinyJit
  def run(x): return model.forward(x).realize()

  # twice to run the JIT
  the_input = Tensor.randn(1,3,224,224)
  the_output = run(the_input)
  the_output = run(the_input)

  # hack to put the inputs back
  assert len(run.input_replace) == 1, f"didn't get one input to replace {run.input_replace}"
  for (j,i),idx in run.input_replace.items():
    run.jit_cache[j][1][i] = the_input.lazydata.realized

  # TODO: fetch this from the jit in self.input_replace and self.ret (hint: use get_parameters on self.ret)
  special_names = {id(the_input.lazydata.realized): "input", id(the_output.lazydata.realized): "outputs"}

  functions, statements, bufs, bufs_to_save = compile_net(run, special_names)

  # c header
  # cprog = ["#include <stdio.h>", "#include <math.h>", "#define max(x,y) ((x>y)?x:y)"]
  cprog = []

  # save the weights
  for name,cl in bufs_to_save.items():
    b = bytes(cl._buf)
    num = len(b) // 4
    weights = ",".join([str(f) for f in struct.unpack(str(num)+'f', b)])
    cprog.append(f"static {name} : [f32; {num}] = [{weights}];")

  # image library!
  # cprog += ["#define STB_IMAGE_IMPLEMENTATION", fetch("https://raw.githubusercontent.com/nothings/stb/master/stb_image.h").decode('utf-8')]

  # imagenet labels, move to datasets?
  lbls = fetch("https://gist.githubusercontent.com/yrevar/942d3a0ac09ec9e5eb3a/raw/238f720ff059c1f82f368259d1ca4ffa5dd8f9f5/imagenet1000_clsidx_to_labels.txt")
  lbls = ast.literal_eval(lbls.decode('utf-8'))
  lbls = ['"'+lbls[i]+'"' for i in range(1000)]
  cprog.append(f"static lbls : [&'static str; 1000] = [{','.join(lbls)}];")

  # empty buffers
  # TODO move all, but the input buffer into the net function to help the compiler?
  cprog += [f"static mut {name} : &'static mut [f32; {len}] = &mut[0.0; {len}];" for name,len in bufs.values() if name not in bufs_to_save]

  # the functions
  cprog += list(functions.values())

  # the net
  # TODO reduce unsafe scope to just indexing ops
  cprog += ["unsafe fn net() {"] + statements + ["}"]

# TODO add extern c declarations here so that we can call the rust lib from c, with the same names
# (extern c blocks https://doc.rust-lang.org/std/keyword.extern.html)
# or setup a cargo workspace with the image-rs crate

#   cprog += ["""
# int main(int argc, char* argv[]) {
#   int X=0, Y=0, chan=0;
#   stbi_uc *image = (argc > 1) ? stbi_load(argv[1], &X, &Y, &chan, 3) : stbi_load_from_file(stdin, &X, &Y, &chan, 3);
#   assert(image != NULL);
#   assert(chan == 3);
#   // resize to input[1,3,224,224] and rescale
#   for (int y = 0; y < 224; y++) {
#     for (int x = 0; x < 224; x++) {
#       // get sample position
#       int tx = (x/224.)*X;
#       int ty = (y/224.)*Y;
#       for (int c = 0; c < 3; c++) {
#         input[c*224*224 + y*224 + x] = (image[ty*X*chan + tx*chan + c] / 255.0 - 0.45) / 0.225;
#       }
#     }
#   }
#   net();
#   double best = -100000.0;
#   int best_idx = -1;
#   for (int i = 0; i < 1000; i++) {
#     // printf("%f\\n", (double)outputs[i]);
#     if (outputs[i] > best) {
#       best = outputs[i];
#       best_idx = i;
#     }
#   }
#   if (best_idx == -1 || best_idx >= 1000) {
#     printf("did not find any match, idx is %i\\n", best_idx);
#   } else {
#     printf("%s\\n", lbls[best_idx]);
#   }
# }"""]

  cprog += ["""
fn main() {
  unsafe {
    dbg!(net());
  }
}"""]

  # CLANG=1 python3 examples/compile_efficientnet.py | clang -O2 -lm -x c - -o recognize && DEBUG=1 time ./recognize docs/stable_diffusion_by_tinygrad.jpg
  # category : 281 (tabby, tabby cat) with 9.452788
  print('\n'.join(cprog))
