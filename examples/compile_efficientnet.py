from pathlib import Path
from extra.models.efficientnet import EfficientNet
from tinygrad.tensor import Tensor
from tinygrad.nn.state import safe_save
from extra.export_model import export_model
from tinygrad.helpers import getenv, fetch
import ast
from tinygrad.runtime.ops_clang import ClangCompiler, ClangProgram
from ctypes import c_char_p

if __name__ == "__main__":
  model = EfficientNet(0)
  model.load_from_pretrained()
  mode = "clang" if getenv("CLANG", "") != "" else "webgpu" if getenv("WEBGPU", "") != "" else "webgl" if getenv("WEBGL", "") != "" else ""
  prg, inp_sizes, out_sizes, state = export_model(model, mode, Tensor.randn(1,3,224,224))
  dirname = Path(__file__).parent
  if getenv("CLANG", "") == "":
    safe_save(state, (dirname / "net.safetensors").as_posix())
    ext = "js" if getenv("WEBGPU", "") != "" or getenv("WEBGL", "") != "" else "json"
    with open(dirname / f"net.{ext}", "w") as text_file:
      text_file.write(prg)
  else:
    cprog = [prg]
    # image library!
    cprog += ["#define STB_IMAGE_IMPLEMENTATION", fetch("https://raw.githubusercontent.com/nothings/stb/master/stb_image.h").read_text().replace("half", "_half")]

    # imagenet labels, move to datasets?
    lbls = ast.literal_eval(fetch("https://gist.githubusercontent.com/yrevar/942d3a0ac09ec9e5eb3a/raw/238f720ff059c1f82f368259d1ca4ffa5dd8f9f5/imagenet1000_clsidx_to_labels.txt").read_text())
    lbls = ['"'+lbls[i]+'"' for i in range(1000)]
    inputs = "\n".join([f"{dtype.name} {inp}[{sz}];" for inp,(sz,dtype,_) in inp_sizes.items()])
    outputs = "\n".join([f"{dtype.name} {out}[{sz}];" for out,(sz,dtype,_) in out_sizes.items()])
    cprog.append(f"char *lbls[] = {{{','.join(lbls)}}};")
    cprog.append(inputs)
    cprog.append(outputs)

    # buffers (empty + weights)
    cprog.append("""
  int main(int argc, char* argv[]) {
    //int DEBUG = getenv("DEBUG") != NULL ? atoi(getenv("DEBUG")) : 0;
    int X=0, Y=0, chan=0;
    stbi_uc *image = (argc > 1) ? stbi_load(argv[1], &X, &Y, &chan, 3) : stbi_load_from_file(stdin, &X, &Y, &chan, 3);
    assert(image != NULL);
    //if (DEBUG) printf("loaded image %dx%d channels %d\\n", X, Y, chan);
    assert(chan == 3);
    // resize to input[1,3,224,224] and rescale
    for (int y = 0; y < 224; y++) {
      for (int x = 0; x < 224; x++) {
        // get sample position
        int tx = (x/224.)*X;
        int ty = (y/224.)*Y;
        for (int c = 0; c < 3; c++) {
          input0[c*224*224 + y*224 + x] = (image[ty*X*chan + tx*chan + c] / 255.0 - 0.45) / 0.225;
        }
      }
    }
    net(input0, output0);
    float best = -INFINITY;
    int best_idx = -1;
    for (int i = 0; i < 1000; i++) {
      if (output0[i] > best) {
        best = output0[i];
        best_idx = i;
      }
    }
    //if (DEBUG) printf("category : %d (%s) with %f\\n", best_idx, lbls[best_idx], best);
    //else printf("%s\\n", lbls[best_idx]);
    printf("category : %d (%s) with %f\\n", best_idx, lbls[best_idx], best);
  }""")

    # CLANG=1 python3 examples/compile_efficientnet.py | clang -O2 -lm -x c - -o recognize && DEBUG=1 time ./recognize docs/showcase/stable_diffusion_by_tinygrad.jpg
    # category : 281 (tabby, tabby cat) with 9.452788
    # print('\n'.join(cprog))
    # above method includes any stdin

    src = '\n'.join(cprog)
    p = ClangProgram("main", ClangCompiler().compile(src))
    p(2, (c_char_p * 2)(b'', b'docs/showcase/stable_diffusion_by_tinygrad.jpg'))
