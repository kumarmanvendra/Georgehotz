#!/bin/bash -e

# setup instructions for clang2py
if [[ ! $(clang2py -V) ]]; then
  pushd .
  cd /tmp
  sudo apt-get install -y --no-install-recommends clang
  pip install --upgrade pip setuptools
  pip install clang==14.0.6
  git clone https://github.com/geohot/ctypeslib.git
  cd ctypeslib
  pip install --user .
  clang2py -V
  popd
fi

BASE=tinygrad/runtime/autogen/

fixup() {
  sed -i '1s/^/# mypy: ignore-errors\n/' $1
  sed -i 's/ *$//' $1
  grep FIXME_STUB $1 || true
}

generate_opencl_error_codes(){

  OPENCL_HEADERS_SRC=/tmp/opencl-headers
  OPENCL_COMMIT_HASH=c860bb551eeef9a47d56286a70cea903db3d6ed2
  if [ ! -d "$OPENCL_HEADERS_SRC" ]; then
    git clone https://github.com/KhronosGroup/OpenCL-Headers.git $OPENCL_HEADERS_SRC
    pushd .
    cd $OPENCL_HEADERS_SRC
    git reset --hard $OPENCL_COMMIT_HASH
    popd
  fi
  
  file=$OPENCL_HEADERS_SRC/opencl_headers_coalesce.h
  cat $OPENCL_HEADERS_SRC/CL/*.h > $file # Form a single file from all headers, to extract all errors present

  # Get all Errors 
  sed -i -n -E '/#define CL_BUILD_[SNEI][A-Z0-9_]*|[A-Z0-9_]*EXP|[A-Z0-9_]*END_INTEL/b;/#define \w*\s*0x40D[4-5]/p;/#define \w*\s*-[0-9]*/p' $file

  #Form dict
  sed -n -i '1i\opencl_status_codes = {}
  { s/#define \([^,]*\)\s*\([-][0-9].*\)/opencl_status_codes[\2] = "\1"/};
  { s/#define \([^,]*\)\s*\(0x40D[4|5].*\)/opencl_status_codes[\2] = "\1"/; p };' $file
  cat $file >> $BASE/opencl.py
}

generate_opencl() {
  clang2py /usr/include/CL/cl.h -o $BASE/opencl.py -l /usr/lib/x86_64-linux-gnu/libOpenCL.so.1 -k cdefstum
  fixup $BASE/opencl.py
  # hot patches
  sed -i "s\import ctypes\import ctypes, ctypes.util\g" $BASE/opencl.py
  sed -i "s\ctypes.CDLL('/usr/lib/x86_64-linux-gnu/libOpenCL.so.1')\ctypes.CDLL(ctypes.util.find_library('OpenCL'))\g" $BASE/opencl.py


  python3 -c "import tinygrad.runtime.autogen.opencl"
}

generate_hip() {
  clang2py /opt/rocm/include/hip/hip_ext.h /opt/rocm/include/hip/hiprtc.h \
  /opt/rocm/include/hip/hip_runtime_api.h /opt/rocm/include/hip/driver_types.h \
  --clang-args="-D__HIP_PLATFORM_AMD__ -I/opt/rocm/include -x c++" -o $BASE/hip.py -l /opt/rocm/lib/libamdhip64.so
  echo "hipDeviceProp_t = hipDeviceProp_tR0600" >> $BASE/hip.py
  echo "hipGetDeviceProperties = hipGetDevicePropertiesR0600" >> $BASE/hip.py
  fixup $BASE/hip.py
  # we can trust HIP is always at /opt/rocm/lib
  #sed -i "s\import ctypes\import ctypes, ctypes.util\g" $BASE/hip.py
  #sed -i "s\ctypes.CDLL('/opt/rocm/lib/libhiprtc.so')\ctypes.CDLL(ctypes.util.find_library('hiprtc'))\g" $BASE/hip.py
  #sed -i "s\ctypes.CDLL('/opt/rocm/lib/libamdhip64.so')\ctypes.CDLL(ctypes.util.find_library('amdhip64'))\g" $BASE/hip.py
  sed -i "s\import ctypes\import ctypes, os\g" $BASE/hip.py
  sed -i "s\'/opt/rocm/\os.getenv('ROCM_PATH', '/opt/rocm/')+'/\g" $BASE/hip.py
  python3 -c "import tinygrad.runtime.autogen.hip"
}

generate_comgr() {
  clang2py /opt/rocm/include/amd_comgr/amd_comgr.h \
  --clang-args="-D__HIP_PLATFORM_AMD__ -I/opt/rocm/include -x c++" -o $BASE/comgr.py -l /opt/rocm/lib/libamd_comgr.so
  fixup $BASE/comgr.py
  sed -i "s\import ctypes\import ctypes, ctypes.util, os\g" $BASE/comgr.py
  sed -i "s\ctypes.CDLL('/opt/rocm/lib/libamd_comgr.so')\ctypes.CDLL(os.getenv('ROCM_PATH')+'/lib/libamd_comgr.so' if os.getenv('ROCM_PATH') else ctypes.util.find_library('amd_comgr'))\g" $BASE/comgr.py
  python3 -c "import tinygrad.runtime.autogen.comgr"
}

generate_kfd() {
  clang2py /usr/include/linux/kfd_ioctl.h -o $BASE/kfd.py -k cdefstum
  fixup $BASE/kfd.py
  sed -i "s\import ctypes\import ctypes, os\g" $BASE/kfd.py
  python3 -c "import tinygrad.runtime.autogen.kfd"
}

generate_cuda() {
  clang2py /usr/include/cuda.h /usr/include/nvrtc.h -o $BASE/cuda.py -l /usr/lib/x86_64-linux-gnu/libcuda.so -l /usr/lib/x86_64-linux-gnu/libnvrtc.so
  sed -i "s\import ctypes\import ctypes, ctypes.util\g" $BASE/cuda.py
  sed -i "s\ctypes.CDLL('/usr/lib/x86_64-linux-gnu/libcuda.so')\ctypes.CDLL(ctypes.util.find_library('cuda'))\g" $BASE/cuda.py
  sed -i "s\ctypes.CDLL('/usr/lib/x86_64-linux-gnu/libnvrtc.so')\ctypes.CDLL(ctypes.util.find_library('nvrtc'))\g" $BASE/cuda.py
  fixup $BASE/cuda.py
  python3 -c "import tinygrad.runtime.autogen.cuda"
}

generate_nv() {
  NVKERN_COMMIT_HASH=d6b75a34094b0f56c2ccadf14e5d0bd515ed1ab6
  NVKERN_SRC=/tmp/open-gpu-kernel-modules-$NVKERN_COMMIT_HASH
  if [ ! -d "$NVKERN_SRC" ]; then
    git clone https://github.com/tinygrad/open-gpu-kernel-modules $NVKERN_SRC
    pushd .
    cd $NVKERN_SRC
    git reset --hard $NVKERN_COMMIT_HASH
    popd
  fi

  clang2py \
    extra/nv_gpu_driver/clc6c0qmd.h \
    $NVKERN_SRC/src/common/sdk/nvidia/inc/class/cl0080.h \
    $NVKERN_SRC/src/common/sdk/nvidia/inc/class/cl2080_notification.h \
    $NVKERN_SRC/src/common/sdk/nvidia/inc/class/clc56f.h \
    $NVKERN_SRC/src/common/sdk/nvidia/inc/class/clc56f.h \
    $NVKERN_SRC/src/common/sdk/nvidia/inc/class/clc56f.h \
    $NVKERN_SRC/src/nvidia/generated/g_allclasses.h \
    $NVKERN_SRC/src/common/sdk/nvidia/inc/class/clc6c0.h \
    $NVKERN_SRC/kernel-open/nvidia-uvm/clc6b5.h \
    $NVKERN_SRC/kernel-open/nvidia-uvm/uvm_ioctl.h \
    $NVKERN_SRC/kernel-open/nvidia-uvm/uvm_linux_ioctl.h \
    $NVKERN_SRC/src/nvidia/arch/nvalloc/unix/include/nv_escape.h \
    $NVKERN_SRC/src/nvidia/arch/nvalloc/unix/include/nv-ioctl.h \
    $NVKERN_SRC/src/nvidia/arch/nvalloc/unix/include/nv-ioctl-numbers.h \
    $NVKERN_SRC/src/nvidia/arch/nvalloc/unix/include/nv-ioctl-numa.h \
    $NVKERN_SRC/src/nvidia/arch/nvalloc/unix/include/nv-unix-nvos-params-wrappers.h \
    $NVKERN_SRC/src/common/sdk/nvidia/inc/alloc/alloc_channel.h \
    $NVKERN_SRC/src/common/sdk/nvidia/inc/nvos.h \
    $NVKERN_SRC/src/common/sdk/nvidia/inc/ctrl/ctrl0000/*.h \
    $NVKERN_SRC/src/common/sdk/nvidia/inc/ctrl/ctrl0080/*.h \
    $NVKERN_SRC/src/common/sdk/nvidia/inc/ctrl/ctrl2080/*.h \
    $NVKERN_SRC/src/common/sdk/nvidia/inc/ctrl/ctrl83de/*.h \
    $NVKERN_SRC/src/common/sdk/nvidia/inc/ctrl/ctrlc36f.h \
    $NVKERN_SRC/src/common/sdk/nvidia/inc/ctrl/ctrlcb33.h \
    $NVKERN_SRC/src/common/sdk/nvidia/inc/ctrl/ctrla06c.h \
    --clang-args="-include $NVKERN_SRC/src/common/sdk/nvidia/inc/nvtypes.h -I$NVKERN_SRC/src/common/inc -I$NVKERN_SRC/kernel-open/nvidia-uvm -I$NVKERN_SRC/kernel-open/common/inc -I$NVKERN_SRC/src/common/sdk/nvidia/inc -I$NVKERN_SRC/src/nvidia/arch/nvalloc/unix/include -I$NVKERN_SRC/src/common/sdk/nvidia/inc/ctrl" \
    -o $BASE/nv_gpu.py -k cdefstum
  fixup $BASE/nv_gpu.py
  sed -i "s\(0000000001)\1\g" $BASE/nv_gpu.py
  sed -i "s\import ctypes\import ctypes, os\g" $BASE/nv_gpu.py
  sed -i 's/#\?\s\([A-Za-z0-9_]\+\) = MW ( \([0-9]\+\) : \([0-9]\+\) )/\1 = (\2 , \3)/' $BASE/nv_gpu.py # NVC6C0_QMDV03_00 processing
  sed -i 's/#\sdef NVC6C0_QMD\([A-Za-z0-9_()]\+\):/def NVC6C0_QMD\1:/' $BASE/nv_gpu.py
  sed -i 's/#\s*return MW(\([0-9i()*+]\+\):\([0-9i()*+]\+\))/    return (\1 , \2)/' $BASE/nv_gpu.py
  sed -i 's/#\?\s*\(.*\)\s*=\s*\(NV\)\?BIT\(32\)\?\s*(\s*\([0-9]\+\)\s*)/\1 = (1 << \4)/' $BASE/nv_gpu.py # name = BIT(x) -> name = (1 << x)
  sed -i "s/UVM_\([A-Za-z0-9_]\+\) = \['i', '(', '\([0-9]\+\)', ')'\]/UVM_\1 = \2/" $BASE/nv_gpu.py # UVM_name = ['i', '(', '<num>', ')'] -> UVM_name = <num>

  # Parse status codes
  sed -n '1i\
nv_status_codes = {}
/^NV_STATUS_CODE/ { s/^NV_STATUS_CODE(\([^,]*\), *\([^,]*\), *"\([^"]*\)") *.*$/\1 = \2\nnv_status_codes[\1] = "\3"/; p }' $NVKERN_SRC/src/common/sdk/nvidia/inc/nvstatuscodes.h >> $BASE/nv_gpu.py

  python3 -c "import tinygrad.runtime.autogen.nv_gpu"
}


generate_amd() {
  # clang2py broken when pass -x c++ to prev headers
  clang2py extra/hip_gpu_driver/sdma_registers.h \
    --clang-args="-I/opt/rocm/include -x c++" \
    -o $BASE/amd_gpu.py

  sed 's/^\(.*\)\(\s*\/\*\)\(.*\)$/\1 #\2\3/; s/^\(\s*\*\)\(.*\)$/#\1\2/' extra/hip_gpu_driver/nvd.h >> $BASE/amd_gpu.py # comments
  sed 's/^\(.*\)\(\s*\/\*\)\(.*\)$/\1 #\2\3/; s/^\(\s*\*\)\(.*\)$/#\1\2/' extra/hip_gpu_driver/sdma_v6_0_0_pkt_open.h >> $BASE/amd_gpu.py # comments
  sed -i 's/#\s*define\s*\([^ \t]*\)(\([^)]*\))\s*\(.*\)/def \1(\2): return \3/' $BASE/amd_gpu.py # #define name(x) (smth) -> def name(x): return (smth)
  sed -i '/#\s*define\s\+\([^ \t]\+\)\s\+\([^ ]\+\)/s//\1 = \2/' $BASE/amd_gpu.py # #define name val -> name = val

  sed -e '/^reg/s/^\(reg[^ ]*\) [^ ]* \([^ ]*\) .*/\1 = \2/' \
    -e '/^ix/s/^\(ix[^ ]*\) [^ ]* \([^ ]*\) .*/\1 = \2/' \
    -e '/^[ \t]/d' \
    extra/hip_gpu_driver/gc_11_0_0.reg >> $BASE/amd_gpu.py

  fixup $BASE/amd_gpu.py
  sed -i "s\import ctypes\import ctypes, os\g" $BASE/amd_gpu.py
  python3 -c "import tinygrad.runtime.autogen.amd_gpu"
}

generate_hsa() {
  clang2py \
    /opt/rocm/include/hsa/hsa.h \
    /opt/rocm/include/hsa/hsa_ext_amd.h \
    /opt/rocm/include/hsa/amd_hsa_signal.h \
    /opt/rocm/include/hsa/amd_hsa_queue.h \
    /opt/rocm/include/hsa/amd_hsa_kernel_code.h \
    /opt/rocm/include/hsa/hsa_ext_finalize.h /opt/rocm/include/hsa/hsa_ext_image.h \
    /opt/rocm/include/hsa/hsa_ven_amd_aqlprofile.h \
    --clang-args="-I/opt/rocm/include" \
    -o $BASE/hsa.py -l /opt/rocm/lib/libhsa-runtime64.so

  fixup $BASE/hsa.py
  sed -i "s\import ctypes\import ctypes, ctypes.util, os\g" $BASE/hsa.py
  sed -i "s\ctypes.CDLL('/opt/rocm/lib/libhsa-runtime64.so')\ctypes.CDLL(os.getenv('ROCM_PATH')+'/lib/libhsa-runtime64.so' if os.getenv('ROCM_PATH') else ctypes.util.find_library('hsa-runtime64'))\g" $BASE/hsa.py
  python3 -c "import tinygrad.runtime.autogen.hsa"
}

if [ "$1" == "opencl" ]; then generate_opencl
elif [ "$1" == "opencl-error-code" ]; then generate_opencl_error_codes
elif [ "$1" == "hip" ]; then generate_hip
elif [ "$1" == "comgr" ]; then generate_comgr
elif [ "$1" == "cuda" ]; then generate_cuda
elif [ "$1" == "hsa" ]; then generate_hsa
elif [ "$1" == "kfd" ]; then generate_kfd
elif [ "$1" == "nv" ]; then generate_nv
elif [ "$1" == "amd" ]; then generate_amd
elif [ "$1" == "all" ]; then generate_opencl; generate_hip; generate_comgr; generate_cuda; generate_hsa; generate_kfd; generate_nv; generate_amd
else echo "usage: $0 <type>"
fi
