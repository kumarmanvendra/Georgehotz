# -*- coding: utf-8 -*-
#
# TARGET arch is: ['-I/home/nimlgen/cuda_ioctl_sniffer/open-gpu-kernel-modules/src/common/sdk/nvidia/inc/']
# WORD_SIZE is: 8
# POINTER_SIZE is: 8
# LONGDOUBLE_SIZE is: 16
#
import ctypes


class AsDictMixin:
    @classmethod
    def as_dict(cls, self):
        result = {}
        if not isinstance(self, AsDictMixin):
            # not a structure, assume it's already a python object
            return self
        if not hasattr(cls, "_fields_"):
            return result
        # sys.version_info >= (3, 5)
        # for (field, *_) in cls._fields_:  # noqa
        for field_tuple in cls._fields_:  # noqa
            field = field_tuple[0]
            if field.startswith('PADDING_'):
                continue
            value = getattr(self, field)
            type_ = type(value)
            if hasattr(value, "_length_") and hasattr(value, "_type_"):
                # array
                if not hasattr(type_, "as_dict"):
                    value = [v for v in value]
                else:
                    type_ = type_._type_
                    value = [type_.as_dict(v) for v in value]
            elif hasattr(value, "contents") and hasattr(value, "_type_"):
                # pointer
                try:
                    if not hasattr(type_, "as_dict"):
                        value = value.contents
                    else:
                        type_ = type_._type_
                        value = type_.as_dict(value.contents)
                except ValueError:
                    # nullptr
                    value = None
            elif isinstance(value, AsDictMixin):
                # other structure
                value = type_.as_dict(value)
            result[field] = value
        return result


class Structure(ctypes.Structure, AsDictMixin):

    def __init__(self, *args, **kwds):
        # We don't want to use positional arguments fill PADDING_* fields

        args = dict(zip(self.__class__._field_names_(), args))
        args.update(kwds)
        super(Structure, self).__init__(**args)

    @classmethod
    def _field_names_(cls):
        if hasattr(cls, '_fields_'):
            return (f[0] for f in cls._fields_ if not f[0].startswith('PADDING'))
        else:
            return ()

    @classmethod
    def get_type(cls, field):
        for f in cls._fields_:
            if f[0] == field:
                return f[1]
        return None

    @classmethod
    def bind(cls, bound_fields):
        fields = {}
        for name, type_ in cls._fields_:
            if hasattr(type_, "restype"):
                if name in bound_fields:
                    if bound_fields[name] is None:
                        fields[name] = type_()
                    else:
                        # use a closure to capture the callback from the loop scope
                        fields[name] = (
                            type_((lambda callback: lambda *args: callback(*args))(
                                bound_fields[name]))
                        )
                    del bound_fields[name]
                else:
                    # default callback implementation (does nothing)
                    try:
                        default_ = type_(0).restype().value
                    except TypeError:
                        default_ = None
                    fields[name] = type_((
                        lambda default_: lambda *args: default_)(default_))
            else:
                # not a callback function, use default initialization
                if name in bound_fields:
                    fields[name] = bound_fields[name]
                    del bound_fields[name]
                else:
                    fields[name] = type_()
        if len(bound_fields) != 0:
            raise ValueError(
                "Cannot bind the following unknown callback(s) {}.{}".format(
                    cls.__name__, bound_fields.keys()
            ))
        return cls(**fields)


class Union(ctypes.Union, AsDictMixin):
    pass





NV01_DEVICE_0 = (0x00000080) # macro
# NV0080_MAX_DEVICES = NV_MAX_DEVICES # macro
NV0080_ALLOC_PARAMETERS_MESSAGE_ID = (0x0080) # macro
class struct_NV0080_ALLOC_PARAMETERS(Structure):
    pass

struct_NV0080_ALLOC_PARAMETERS._pack_ = 1 # source:False
struct_NV0080_ALLOC_PARAMETERS._fields_ = [
    ('deviceId', ctypes.c_uint32),
    ('hClientShare', ctypes.c_uint32),
    ('hTargetClient', ctypes.c_uint32),
    ('hTargetDevice', ctypes.c_uint32),
    ('flags', ctypes.c_uint32),
    ('PADDING_0', ctypes.c_ubyte * 4),
    ('vaSpaceSize', ctypes.c_uint64),
    ('vaStartInternal', ctypes.c_uint64),
    ('vaLimitInternal', ctypes.c_uint64),
    ('vaMode', ctypes.c_uint32),
    ('PADDING_1', ctypes.c_ubyte * 4),
]

NV0080_ALLOC_PARAMETERS = struct_NV0080_ALLOC_PARAMETERS
NV01_ROOT = (0x00000000) # macro
NV1_ROOT = (0x00000000) # macro
NV01_NULL_OBJECT = (0x00000000) # macro
NV1_NULL_OBJECT = (0x00000000) # macro
NV01_ROOT_NON_PRIV = (0x00000001) # macro
NV1_ROOT_NON_PRIV = (0x00000001) # macro
NV01_ROOT_CLIENT = (0x00000041) # macro
FABRIC_MANAGER_SESSION = (0x0000000f) # macro
NV0020_GPU_MANAGEMENT = (0x00000020) # macro
NV20_SUBDEVICE_0 = (0x00002080) # macro
NV2081_BINAPI = (0x00002081) # macro
NV2082_BINAPI_PRIVILEGED = (0x00002082) # macro
NV20_SUBDEVICE_DIAG = (0x0000208f) # macro
NV01_CONTEXT_DMA = (0x00000002) # macro
NV01_MEMORY_SYSTEM = (0x0000003e) # macro
NV1_MEMORY_SYSTEM = (0x0000003e) # macro
NV01_MEMORY_LOCAL_PRIVILEGED = (0x0000003f) # macro
NV1_MEMORY_LOCAL_PRIVILEGED = (0x0000003f) # macro
NV01_MEMORY_PRIVILEGED = (0x0000003f) # macro
NV1_MEMORY_PRIVILEGED = (0x0000003f) # macro
NV01_MEMORY_LOCAL_USER = (0x00000040) # macro
NV1_MEMORY_LOCAL_USER = (0x00000040) # macro
NV01_MEMORY_USER = (0x00000040) # macro
NV1_MEMORY_USER = (0x00000040) # macro
NV_MEMORY_EXTENDED_USER = (0x00000042) # macro
NV01_MEMORY_VIRTUAL = (0x00000070) # macro
NV01_MEMORY_SYSTEM_DYNAMIC = (0x00000070) # macro
NV1_MEMORY_SYSTEM_DYNAMIC = (0x00000070) # macro
NV_MEMORY_MAPPER = (0x000000fe) # macro
NV01_MEMORY_LOCAL_PHYSICAL = (0x000000c2) # macro
NV01_MEMORY_SYSTEM_OS_DESCRIPTOR = (0x00000071) # macro
NV01_MEMORY_DEVICELESS = (0x000090ce) # macro
NV01_MEMORY_FRAMEBUFFER_CONSOLE = (0x00000076) # macro
NV01_MEMORY_HW_RESOURCES = (0x000000b1) # macro
NV01_MEMORY_LIST_SYSTEM = (0x00000081) # macro
NV01_MEMORY_LIST_FBMEM = (0x00000082) # macro
NV01_MEMORY_LIST_OBJECT = (0x00000083) # macro
NV_IMEX_SESSION = (0x000000f1) # macro
NV01_MEMORY_FLA = (0x000000f3) # macro
NV_MEMORY_EXPORT = (0x000000e0) # macro
NV_CE_UTILS = (0x00000050) # macro
NV_MEMORY_FABRIC = (0x000000f8) # macro
NV_MEMORY_FABRIC_IMPORT_V2 = (0x000000f9) # macro
NV_MEMORY_FABRIC_IMPORTED_REF = (0x000000fb) # macro
FABRIC_VASPACE_A = (0x000000fc) # macro
NV_MEMORY_MULTICAST_FABRIC = (0x000000fd) # macro
IO_VASPACE_A = (0x000000f2) # macro
NV01_NULL = (0x00000030) # macro
NV1_NULL = (0x00000030) # macro
NV01_EVENT = (0x00000005) # macro
NV1_EVENT = (0x00000005) # macro
NV01_EVENT_KERNEL_CALLBACK = (0x00000078) # macro
NV1_EVENT_KERNEL_CALLBACK = (0x00000078) # macro
NV01_EVENT_OS_EVENT = (0x00000079) # macro
NV1_EVENT_OS_EVENT = (0x00000079) # macro
NV01_EVENT_WIN32_EVENT = (0x00000079) # macro
NV1_EVENT_WIN32_EVENT = (0x00000079) # macro
NV01_EVENT_KERNEL_CALLBACK_EX = (0x0000007e) # macro
NV1_EVENT_KERNEL_CALLBACK_EX = (0x0000007e) # macro
NV01_TIMER = (0x00000004) # macro
NV1_TIMER = (0x00000004) # macro
KERNEL_GRAPHICS_CONTEXT = (0x00000090) # macro
NV50_CHANNEL_GPFIFO = (0x0000506f) # macro
GF100_CHANNEL_GPFIFO = (0x0000906f) # macro
KEPLER_CHANNEL_GPFIFO_A = (0x0000a06f) # macro
UVM_CHANNEL_RETAINER = (0x0000c574) # macro
KEPLER_CHANNEL_GPFIFO_B = (0x0000a16f) # macro
MAXWELL_CHANNEL_GPFIFO_A = (0x0000b06f) # macro
PASCAL_CHANNEL_GPFIFO_A = (0x0000c06f) # macro
VOLTA_CHANNEL_GPFIFO_A = (0x0000c36f) # macro
TURING_CHANNEL_GPFIFO_A = (0x0000c46f) # macro
AMPERE_CHANNEL_GPFIFO_A = (0x0000c56f) # macro
HOPPER_CHANNEL_GPFIFO_A = (0x0000c86f) # macro
NV04_SOFTWARE_TEST = (0x0000007d) # macro
NV4_SOFTWARE_TEST = (0x0000007d) # macro
NV30_GSYNC = (0x000030f1) # macro
VOLTA_USERMODE_A = (0x0000c361) # macro
TURING_USERMODE_A = (0x0000c461) # macro
AMPERE_USERMODE_A = (0x0000c561) # macro
HOPPER_USERMODE_A = (0x0000c661) # macro
NVC371_DISP_SF_USER = (0x0000c371) # macro
NVC372_DISPLAY_SW = (0x0000c372) # macro
NVC573_DISP_CAPABILITIES = (0x0000c573) # macro
NVC673_DISP_CAPABILITIES = (0x0000c673) # macro
NVC773_DISP_CAPABILITIES = (0x0000c773) # macro
NV04_DISPLAY_COMMON = (0x00000073) # macro
NV50_DEFERRED_API_CLASS = (0x00005080) # macro
MPS_COMPUTE = (0x0000900e) # macro
NVC570_DISPLAY = (0x0000c570) # macro
NVC57A_CURSOR_IMM_CHANNEL_PIO = (0x0000c57a) # macro
NVC57B_WINDOW_IMM_CHANNEL_DMA = (0x0000c57b) # macro
NVC57D_CORE_CHANNEL_DMA = (0x0000c57d) # macro
NVC57E_WINDOW_CHANNEL_DMA = (0x0000c57e) # macro
NVC670_DISPLAY = (0x0000c670) # macro
NVC671_DISP_SF_USER = (0x0000c671) # macro
NVC67A_CURSOR_IMM_CHANNEL_PIO = (0x0000c67a) # macro
NVC67B_WINDOW_IMM_CHANNEL_DMA = (0x0000c67b) # macro
NVC67D_CORE_CHANNEL_DMA = (0x0000c67d) # macro
NVC67E_WINDOW_CHANNEL_DMA = (0x0000c67e) # macro
NVC77F_ANY_CHANNEL_DMA = (0x0000c77f) # macro
NVC770_DISPLAY = (0x0000c770) # macro
NVC771_DISP_SF_USER = (0x0000c771) # macro
NVC77D_CORE_CHANNEL_DMA = (0x0000c77d) # macro
NV9010_VBLANK_CALLBACK = (0x00009010) # macro
GF100_PROFILER = (0x000090cc) # macro
MAXWELL_PROFILER = (0x0000b0cc) # macro
MAXWELL_PROFILER_DEVICE = (0x0000b2cc) # macro
GF100_SUBDEVICE_MASTER = (0x000090e6) # macro
GF100_SUBDEVICE_INFOROM = (0x000090e7) # macro
GF100_ZBC_CLEAR = (0x00009096) # macro
GF100_DISP_SW = (0x00009072) # macro
GF100_TIMED_SEMAPHORE_SW = (0x00009074) # macro
G84_PERFBUFFER = (0x0000844c) # macro
NV50_MEMORY_VIRTUAL = (0x000050a0) # macro
NV50_P2P = (0x0000503b) # macro
NV50_THIRD_PARTY_P2P = (0x0000503c) # macro
FERMI_TWOD_A = (0x0000902d) # macro
FERMI_VASPACE_A = (0x000090f1) # macro
HOPPER_SEC2_WORK_LAUNCH_A = (0x0000cba2) # macro
GF100_HDACODEC = (0x000090ec) # macro
NVB8B0_VIDEO_DECODER = (0x0000b8b0) # macro
NVC4B0_VIDEO_DECODER = (0x0000c4b0) # macro
NVC6B0_VIDEO_DECODER = (0x0000c6b0) # macro
NVC7B0_VIDEO_DECODER = (0x0000c7b0) # macro
NVC9B0_VIDEO_DECODER = (0x0000c9b0) # macro
NVC4B7_VIDEO_ENCODER = (0x0000c4b7) # macro
NVB4B7_VIDEO_ENCODER = (0x0000b4b7) # macro
NVC7B7_VIDEO_ENCODER = (0x0000c7b7) # macro
NVC9B7_VIDEO_ENCODER = (0x0000c9b7) # macro
NVB8D1_VIDEO_NVJPG = (0x0000b8d1) # macro
NVC4D1_VIDEO_NVJPG = (0x0000c4d1) # macro
NVC9D1_VIDEO_NVJPG = (0x0000c9d1) # macro
NVB8FA_VIDEO_OFA = (0x0000b8fa) # macro
NVC6FA_VIDEO_OFA = (0x0000c6fa) # macro
NVC7FA_VIDEO_OFA = (0x0000c7fa) # macro
NVC9FA_VIDEO_OFA = (0x0000c9fa) # macro
KEPLER_INLINE_TO_MEMORY_B = (0x0000a140) # macro
FERMI_CONTEXT_SHARE_A = (0x00009067) # macro
KEPLER_CHANNEL_GROUP_A = (0x0000a06c) # macro
PASCAL_DMA_COPY_A = (0x0000c0b5) # macro
TURING_DMA_COPY_A = (0x0000c5b5) # macro
AMPERE_DMA_COPY_A = (0x0000c6b5) # macro
AMPERE_DMA_COPY_B = (0x0000c7b5) # macro
HOPPER_DMA_COPY_A = (0x0000c8b5) # macro
MAXWELL_DMA_COPY_A = (0x0000b0b5) # macro
ACCESS_COUNTER_NOTIFY_BUFFER = (0x0000c365) # macro
MMU_FAULT_BUFFER = (0x0000c369) # macro
MMU_VIDMEM_ACCESS_BIT_BUFFER = (0x0000c763) # macro
TURING_A = (0x0000c597) # macro
TURING_COMPUTE_A = (0x0000c5c0) # macro
AMPERE_A = (0x0000c697) # macro
AMPERE_COMPUTE_A = (0x0000c6c0) # macro
AMPERE_B = (0x0000c797) # macro
AMPERE_COMPUTE_B = (0x0000c7c0) # macro
ADA_A = (0x0000c997) # macro
ADA_COMPUTE_A = (0x0000c9c0) # macro
AMPERE_SMC_PARTITION_REF = (0x0000c637) # macro
AMPERE_SMC_EXEC_PARTITION_REF = (0x0000c638) # macro
AMPERE_SMC_CONFIG_SESSION = (0x0000c639) # macro
NV0092_RG_LINE_CALLBACK = (0x00000092) # macro
AMPERE_SMC_MONITOR_SESSION = (0x0000c640) # macro
HOPPER_A = (0x0000cb97) # macro
HOPPER_COMPUTE_A = (0x0000cbc0) # macro
NV40_DEBUG_BUFFER = (0x000000db) # macro
RM_USER_SHARED_DATA = (0x000000de) # macro
GT200_DEBUGGER = (0x000083de) # macro
NV40_I2C = (0x0000402c) # macro
KEPLER_DEVICE_VGPU = (0x0000a080) # macro
NVA081_VGPU_CONFIG = (0x0000a081) # macro
NVA084_KERNEL_HOST_VGPU_DEVICE = (0x0000a084) # macro
NV0060_SYNC_GPU_BOOST = (0x00000060) # macro
GP100_UVM_SW = (0x0000c076) # macro
NVENC_SW_SESSION = (0x0000a0bc) # macro
NV_EVENT_BUFFER = (0x000090cd) # macro
NVFBC_SW_SESSION = (0x0000a0bd) # macro
NV_CONFIDENTIAL_COMPUTE = (0x0000cb33) # macro
NV_COUNTER_COLLECTION_UNIT = (0x0000cbca) # macro
NV_SEMAPHORE_SURFACE = (0x000000da) # macro
__all__ = \
    ['ACCESS_COUNTER_NOTIFY_BUFFER', 'ADA_A', 'ADA_COMPUTE_A',
    'AMPERE_A', 'AMPERE_B', 'AMPERE_CHANNEL_GPFIFO_A',
    'AMPERE_COMPUTE_A', 'AMPERE_COMPUTE_B', 'AMPERE_DMA_COPY_A',
    'AMPERE_DMA_COPY_B', 'AMPERE_SMC_CONFIG_SESSION',
    'AMPERE_SMC_EXEC_PARTITION_REF', 'AMPERE_SMC_MONITOR_SESSION',
    'AMPERE_SMC_PARTITION_REF', 'AMPERE_USERMODE_A',
    'FABRIC_MANAGER_SESSION', 'FABRIC_VASPACE_A',
    'FERMI_CONTEXT_SHARE_A', 'FERMI_TWOD_A', 'FERMI_VASPACE_A',
    'G84_PERFBUFFER', 'GF100_CHANNEL_GPFIFO', 'GF100_DISP_SW',
    'GF100_HDACODEC', 'GF100_PROFILER', 'GF100_SUBDEVICE_INFOROM',
    'GF100_SUBDEVICE_MASTER', 'GF100_TIMED_SEMAPHORE_SW',
    'GF100_ZBC_CLEAR', 'GP100_UVM_SW', 'GT200_DEBUGGER', 'HOPPER_A',
    'HOPPER_CHANNEL_GPFIFO_A', 'HOPPER_COMPUTE_A',
    'HOPPER_DMA_COPY_A', 'HOPPER_SEC2_WORK_LAUNCH_A',
    'HOPPER_USERMODE_A', 'IO_VASPACE_A', 'KEPLER_CHANNEL_GPFIFO_A',
    'KEPLER_CHANNEL_GPFIFO_B', 'KEPLER_CHANNEL_GROUP_A',
    'KEPLER_DEVICE_VGPU', 'KEPLER_INLINE_TO_MEMORY_B',
    'KERNEL_GRAPHICS_CONTEXT', 'MAXWELL_CHANNEL_GPFIFO_A',
    'MAXWELL_DMA_COPY_A', 'MAXWELL_PROFILER',
    'MAXWELL_PROFILER_DEVICE', 'MMU_FAULT_BUFFER',
    'MMU_VIDMEM_ACCESS_BIT_BUFFER', 'MPS_COMPUTE',
    'NV0020_GPU_MANAGEMENT', 'NV0060_SYNC_GPU_BOOST',
    'NV0080_ALLOC_PARAMETERS', 'NV0080_ALLOC_PARAMETERS_MESSAGE_ID',
    'NV0092_RG_LINE_CALLBACK', 'NV01_CONTEXT_DMA', 'NV01_DEVICE_0',
    'NV01_EVENT', 'NV01_EVENT_KERNEL_CALLBACK',
    'NV01_EVENT_KERNEL_CALLBACK_EX', 'NV01_EVENT_OS_EVENT',
    'NV01_EVENT_WIN32_EVENT', 'NV01_MEMORY_DEVICELESS',
    'NV01_MEMORY_FLA', 'NV01_MEMORY_FRAMEBUFFER_CONSOLE',
    'NV01_MEMORY_HW_RESOURCES', 'NV01_MEMORY_LIST_FBMEM',
    'NV01_MEMORY_LIST_OBJECT', 'NV01_MEMORY_LIST_SYSTEM',
    'NV01_MEMORY_LOCAL_PHYSICAL', 'NV01_MEMORY_LOCAL_PRIVILEGED',
    'NV01_MEMORY_LOCAL_USER', 'NV01_MEMORY_PRIVILEGED',
    'NV01_MEMORY_SYSTEM', 'NV01_MEMORY_SYSTEM_DYNAMIC',
    'NV01_MEMORY_SYSTEM_OS_DESCRIPTOR', 'NV01_MEMORY_USER',
    'NV01_MEMORY_VIRTUAL', 'NV01_NULL', 'NV01_NULL_OBJECT',
    'NV01_ROOT', 'NV01_ROOT_CLIENT', 'NV01_ROOT_NON_PRIV',
    'NV01_TIMER', 'NV04_DISPLAY_COMMON', 'NV04_SOFTWARE_TEST',
    'NV1_EVENT', 'NV1_EVENT_KERNEL_CALLBACK',
    'NV1_EVENT_KERNEL_CALLBACK_EX', 'NV1_EVENT_OS_EVENT',
    'NV1_EVENT_WIN32_EVENT', 'NV1_MEMORY_LOCAL_PRIVILEGED',
    'NV1_MEMORY_LOCAL_USER', 'NV1_MEMORY_PRIVILEGED',
    'NV1_MEMORY_SYSTEM', 'NV1_MEMORY_SYSTEM_DYNAMIC',
    'NV1_MEMORY_USER', 'NV1_NULL', 'NV1_NULL_OBJECT', 'NV1_ROOT',
    'NV1_ROOT_NON_PRIV', 'NV1_TIMER', 'NV2081_BINAPI',
    'NV2082_BINAPI_PRIVILEGED', 'NV20_SUBDEVICE_0',
    'NV20_SUBDEVICE_DIAG', 'NV30_GSYNC', 'NV40_DEBUG_BUFFER',
    'NV40_I2C', 'NV4_SOFTWARE_TEST', 'NV50_CHANNEL_GPFIFO',
    'NV50_DEFERRED_API_CLASS', 'NV50_MEMORY_VIRTUAL', 'NV50_P2P',
    'NV50_THIRD_PARTY_P2P', 'NV9010_VBLANK_CALLBACK',
    'NVA081_VGPU_CONFIG', 'NVA084_KERNEL_HOST_VGPU_DEVICE',
    'NVB4B7_VIDEO_ENCODER', 'NVB8B0_VIDEO_DECODER',
    'NVB8D1_VIDEO_NVJPG', 'NVB8FA_VIDEO_OFA', 'NVC371_DISP_SF_USER',
    'NVC372_DISPLAY_SW', 'NVC4B0_VIDEO_DECODER',
    'NVC4B7_VIDEO_ENCODER', 'NVC4D1_VIDEO_NVJPG', 'NVC570_DISPLAY',
    'NVC573_DISP_CAPABILITIES', 'NVC57A_CURSOR_IMM_CHANNEL_PIO',
    'NVC57B_WINDOW_IMM_CHANNEL_DMA', 'NVC57D_CORE_CHANNEL_DMA',
    'NVC57E_WINDOW_CHANNEL_DMA', 'NVC670_DISPLAY',
    'NVC671_DISP_SF_USER', 'NVC673_DISP_CAPABILITIES',
    'NVC67A_CURSOR_IMM_CHANNEL_PIO', 'NVC67B_WINDOW_IMM_CHANNEL_DMA',
    'NVC67D_CORE_CHANNEL_DMA', 'NVC67E_WINDOW_CHANNEL_DMA',
    'NVC6B0_VIDEO_DECODER', 'NVC6FA_VIDEO_OFA', 'NVC770_DISPLAY',
    'NVC771_DISP_SF_USER', 'NVC773_DISP_CAPABILITIES',
    'NVC77D_CORE_CHANNEL_DMA', 'NVC77F_ANY_CHANNEL_DMA',
    'NVC7B0_VIDEO_DECODER', 'NVC7B7_VIDEO_ENCODER',
    'NVC7FA_VIDEO_OFA', 'NVC9B0_VIDEO_DECODER',
    'NVC9B7_VIDEO_ENCODER', 'NVC9D1_VIDEO_NVJPG', 'NVC9FA_VIDEO_OFA',
    'NVENC_SW_SESSION', 'NVFBC_SW_SESSION', 'NV_CE_UTILS',
    'NV_CONFIDENTIAL_COMPUTE', 'NV_COUNTER_COLLECTION_UNIT',
    'NV_EVENT_BUFFER', 'NV_IMEX_SESSION', 'NV_MEMORY_EXPORT',
    'NV_MEMORY_EXTENDED_USER', 'NV_MEMORY_FABRIC',
    'NV_MEMORY_FABRIC_IMPORTED_REF', 'NV_MEMORY_FABRIC_IMPORT_V2',
    'NV_MEMORY_MAPPER', 'NV_MEMORY_MULTICAST_FABRIC',
    'NV_SEMAPHORE_SURFACE', 'PASCAL_CHANNEL_GPFIFO_A',
    'PASCAL_DMA_COPY_A', 'RM_USER_SHARED_DATA', 'TURING_A',
    'TURING_CHANNEL_GPFIFO_A', 'TURING_COMPUTE_A',
    'TURING_DMA_COPY_A', 'TURING_USERMODE_A', 'UVM_CHANNEL_RETAINER',
    'VOLTA_CHANNEL_GPFIFO_A', 'VOLTA_USERMODE_A',
    'struct_NV0080_ALLOC_PARAMETERS']
