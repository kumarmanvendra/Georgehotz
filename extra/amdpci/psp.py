import os, ctypes, time
from tinygrad.runtime.autogen import libpciaccess, amdgpu_2, amdgpu_mp_13_0_0_offset, amdgpu_psp_gfx_if
from tinygrad.helpers import to_mv, mv_address, colored
from extra.amdpci.firmware import Firmware

class PSP_IP:
  SOS_PATH = "/lib/firmware/amdgpu/psp_13_0_0_sos.bin"
  TA_PATH = "/lib/firmware/amdgpu/psp_13_0_0_ta.bin"
  SMU_PATH = "/lib/firmware/amdgpu/smu_13_0_0.bin"
  PFP_PATH = "/lib/firmware/amdgpu/gc_11_0_0_pfp.bin"
  ME_PATH = "/lib/firmware/amdgpu/gc_11_0_0_me.bin"
  RLC_PATH = "/lib/firmware/amdgpu/gc_11_0_0_rlc.bin"
  MEC_PATH = "/lib/firmware/amdgpu/gc_11_0_0_mec.bin"
  MES_2_PATH = "/lib/firmware/amdgpu/gc_11_0_0_mes_2.bin"
  MES1_PATH = "/lib/firmware/amdgpu/gc_11_0_0_mes1.bin" # KIQ
  IMU_PATH = "/lib/firmware/amdgpu/gc_11_0_0_imu.bin"

  def __init__(self, adev):
    self.adev = adev
    self.prep_fw()
    self.init_sos()
    self.init_ta()
    self.load_fw()

  def prep_fw(self):
    self.smu_fw = Firmware(self.adev, self.SMU_PATH, amdgpu_2.struct_smc_firmware_header_v1_0)
    self.smu_psp_desc = self.smu_fw.smu_psp_desc()

    self.pfp_fw = Firmware(self.adev, self.PFP_PATH, amdgpu_2.struct_gfx_firmware_header_v2_0)
    self.me_fw = Firmware(self.adev, self.ME_PATH, amdgpu_2.struct_gfx_firmware_header_v2_0)
    self.mec_fw = Firmware(self.adev, self.MEC_PATH, amdgpu_2.struct_gfx_firmware_header_v2_0)
    self.mes_fw = Firmware(self.adev, self.MES_2_PATH, amdgpu_2.struct_mes_firmware_header_v1_0)
    self.mes_kiq_fw = Firmware(self.adev, self.MES1_PATH, amdgpu_2.struct_mes_firmware_header_v1_0)

    self.fw_list = [
      self.pfp_fw.cpv2_code_psp_desc(amdgpu_psp_gfx_if.GFX_FW_TYPE_RS64_PFP),
      self.me_fw.cpv2_code_psp_desc(amdgpu_psp_gfx_if.GFX_FW_TYPE_RS64_ME),
      self.mec_fw.cpv2_code_psp_desc(amdgpu_psp_gfx_if.GFX_FW_TYPE_RS64_MEC),

      self.pfp_fw.cpv2_data_psp_desc(amdgpu_psp_gfx_if.GFX_FW_TYPE_RS64_PFP_P0_STACK),
      self.pfp_fw.cpv2_data_psp_desc(amdgpu_psp_gfx_if.GFX_FW_TYPE_RS64_PFP_P1_STACK),

      self.me_fw.cpv2_data_psp_desc(amdgpu_psp_gfx_if.GFX_FW_TYPE_RS64_ME_P0_STACK),
      self.me_fw.cpv2_data_psp_desc(amdgpu_psp_gfx_if.GFX_FW_TYPE_RS64_ME_P1_STACK),

      self.mec_fw.cpv2_data_psp_desc(amdgpu_psp_gfx_if.GFX_FW_TYPE_RS64_MEC_P0_STACK),
      self.mec_fw.cpv2_data_psp_desc(amdgpu_psp_gfx_if.GFX_FW_TYPE_RS64_MEC_P1_STACK),
      self.mec_fw.cpv2_data_psp_desc(amdgpu_psp_gfx_if.GFX_FW_TYPE_RS64_MEC_P2_STACK),
      self.mec_fw.cpv2_data_psp_desc(amdgpu_psp_gfx_if.GFX_FW_TYPE_RS64_MEC_P3_STACK),

      self.mes_fw.mes_code_psp_desc(amdgpu_psp_gfx_if.GFX_FW_TYPE_CP_MES),
      self.mes_fw.mes_data_psp_desc(amdgpu_psp_gfx_if.GFX_FW_TYPE_MES_STACK),

      self.mes_kiq_fw.mes_code_psp_desc(amdgpu_psp_gfx_if.GFX_FW_TYPE_CP_MES_KIQ),
      self.mes_kiq_fw.mes_data_psp_desc(amdgpu_psp_gfx_if.GFX_FW_TYPE_MES_KIQ_STACK),
    ]

  def init_sos(self):
    sos_fw = memoryview(bytearray(open(self.SOS_PATH, "rb").read()))
    sos_hdr = amdgpu_2.struct_psp_firmware_header_v1_0.from_address(mv_address(sos_fw))

    assert sos_hdr.header.header_version_major == 2
    sos_hdr = amdgpu_2.struct_psp_firmware_header_v2_0.from_address(mv_address(sos_fw))

    assert sos_hdr.header.header_version_minor == 0
    fw_bin = sos_hdr.psp_fw_bin

    self.sos_fw_infos = []
    for fw_i in range(sos_hdr.psp_fw_bin_count):
      fw_bin_desc = amdgpu_2.struct_psp_fw_bin_desc.from_address(ctypes.addressof(fw_bin) + fw_i * ctypes.sizeof(amdgpu_2.struct_psp_fw_bin_desc))
      ucode_start_offset = fw_bin_desc.offset_bytes + sos_hdr.header.ucode_array_offset_bytes
      self.sos_fw_infos.append((fw_bin_desc.fw_type, ucode_start_offset))

    self.sos_fw = sos_fw
    self.sos_hdr = sos_hdr

  def init_ta(self):
    ta_fw = memoryview(bytearray(open(self.TA_PATH, "rb").read()))
    ta_hdr = amdgpu_2.struct_common_firmware_header.from_address(mv_address(ta_fw))
    assert ta_hdr.header_version_major == 2

    ta_hdr = amdgpu_2.struct_ta_firmware_header_v2_0.from_address(mv_address(ta_fw))

    fw_bin = ta_hdr.ta_fw_bin
    self.ta_fw_infos = []
    for fw_i in range(ta_hdr.ta_fw_bin_count):
      fw_bin_desc = amdgpu_2.struct_psp_fw_bin_desc.from_address(ctypes.addressof(fw_bin) + fw_i * ctypes.sizeof(amdgpu_2.struct_psp_fw_bin_desc))
      ucode_start_offset = fw_bin_desc.offset_bytes + ta_hdr.header.ucode_array_offset_bytes
      self.ta_fw_infos.append((fw_bin_desc.fw_type, ucode_start_offset))

    self.ta_fw = ta_fw
    self.ta_hdr = ta_hdr

  def load_fw(self):
    self.fence_buf = self.adev.vmm.alloc_vram(0x1000, "psp_fence_buf")
    self.cmd_buf = self.adev.vmm.alloc_vram(0x1000, "psp_cmd_buf")
    self.ring_mem = self.adev.vmm.alloc_vram(0x10000, "psp_ring_mem") # a bit bigger, no wrap around for this ring

    ctypes.memset(self.adev.vmm.vram_to_cpu_addr(self.fence_buf, 0x1000), 0, 0x1000)

    self.hw_start()

  def is_sos_alive(self):
    sol = self.adev.rreg_ip("MP0", 0, amdgpu_mp_13_0_0_offset.regMP0_SMN_C2PMSG_81, amdgpu_mp_13_0_0_offset.regMP0_SMN_C2PMSG_81_BASE_IDX)
    return sol != 0x0

  def init_sos_version(self):
    self.sos_fw_version = self.adev.rreg_ip("MP0", 0, amdgpu_mp_13_0_0_offset.regMP0_SMN_C2PMSG_58, amdgpu_mp_13_0_0_offset.regMP0_SMN_C2PMSG_58_BASE_IDX)
    
  def ring_create(self):
    # Remove all rings, will setup our new rings...
    self.adev.wreg_ip("MP0", 0, amdgpu_mp_13_0_0_offset.regMP0_SMN_C2PMSG_64, amdgpu_mp_13_0_0_offset.regMP0_SMN_C2PMSG_64_BASE_IDX, amdgpu_psp_gfx_if.GFX_CTRL_CMD_ID_DESTROY_RINGS)
    time.sleep(100 / 1000) # 20 ms orignally

    reg = 0
    while reg & 0x8000FFFF != 0x80000000:
      reg = self.adev.rreg_ip("MP0", 0, amdgpu_mp_13_0_0_offset.regMP0_SMN_C2PMSG_64, amdgpu_mp_13_0_0_offset.regMP0_SMN_C2PMSG_64_BASE_IDX)
      print(reg)

    # Wait till the ring is ready
    reg = 0
    while reg & 0x80000000 != 0x80000000:
      reg = self.adev.rreg_ip("MP0", 0, amdgpu_mp_13_0_0_offset.regMP0_SMN_C2PMSG_64, amdgpu_mp_13_0_0_offset.regMP0_SMN_C2PMSG_64_BASE_IDX)

    self.adev.wreg_ip("MP0", 0, amdgpu_mp_13_0_0_offset.regMP0_SMN_C2PMSG_69, amdgpu_mp_13_0_0_offset.regMP0_SMN_C2PMSG_69_BASE_IDX, self.ring_mem & 0xffffffff)
    self.adev.wreg_ip("MP0", 0, amdgpu_mp_13_0_0_offset.regMP0_SMN_C2PMSG_70, amdgpu_mp_13_0_0_offset.regMP0_SMN_C2PMSG_70_BASE_IDX, self.ring_mem >> 32)
    self.adev.wreg_ip("MP0", 0, amdgpu_mp_13_0_0_offset.regMP0_SMN_C2PMSG_71, amdgpu_mp_13_0_0_offset.regMP0_SMN_C2PMSG_71_BASE_IDX, 0x1000)

    ring_type = 2 << 16 # PSP_RING_TYPE__KM = 2. Kernel mode ring
    self.adev.wreg_ip("MP0", 0, amdgpu_mp_13_0_0_offset.regMP0_SMN_C2PMSG_64, amdgpu_mp_13_0_0_offset.regMP0_SMN_C2PMSG_64_BASE_IDX, ring_type)
    print(self.adev.rreg_ip("MP0", 0, amdgpu_mp_13_0_0_offset.regMP0_SMN_C2PMSG_64, amdgpu_mp_13_0_0_offset.regMP0_SMN_C2PMSG_64_BASE_IDX))

    # there might be handshake issue with hardware which needs delay
    time.sleep(100 / 1000) # 20 ms orignally

    # Wait for response flag
    reg = 0
    while reg & 0x8000FFFF != 0x80000000: # last 16 bits are status, should be 0
      reg = self.adev.rreg_ip("MP0", 0, amdgpu_mp_13_0_0_offset.regMP0_SMN_C2PMSG_64, amdgpu_mp_13_0_0_offset.regMP0_SMN_C2PMSG_64_BASE_IDX)

    print("sOS ring created")

  def prep_load_ip_fw_cmd_buf(self, psp_desc):
    fw_type, phys_addr, phys_size = psp_desc
    print('PSP: issue load ip fw:', fw_type, hex(phys_addr), hex(phys_size))

    assert ctypes.sizeof(amdgpu_psp_gfx_if.struct_psp_gfx_cmd_resp) == 1024
    ctypes.memset(self.adev.vmm.vram_to_cpu_addr(self.cmd_buf, 0x1000), 0, 0x1000)
    cmd = amdgpu_psp_gfx_if.struct_psp_gfx_cmd_resp.from_address(self.adev.vmm.vram_to_cpu_addr(self.cmd_buf))
    cmd.cmd_id = amdgpu_psp_gfx_if.GFX_CMD_ID_LOAD_IP_FW
    cmd.cmd.cmd_load_ip_fw.fw_phy_addr_lo = phys_addr & 0xffffffff
    cmd.cmd.cmd_load_ip_fw.fw_phy_addr_hi = phys_addr >> 32
    cmd.cmd.cmd_load_ip_fw.fw_size = phys_size
    cmd.cmd.cmd_load_ip_fw.fw_type = fw_type

  def prep_tmr_cmd_buf(self):
    assert ctypes.sizeof(amdgpu_psp_gfx_if.struct_psp_gfx_cmd_resp) == 1024
    ctypes.memset(self.adev.vmm.vram_to_cpu_addr(self.cmd_buf, 0x1000), 0, 0x1000)

    cmd = amdgpu_psp_gfx_if.struct_psp_gfx_cmd_resp.from_address(self.adev.vmm.vram_to_cpu_addr(self.cmd_buf))
    cmd.cmd_id = amdgpu_psp_gfx_if.GFX_CMD_ID_SETUP_TMR
    cmd.cmd.cmd_setup_tmr.buf_phy_addr_lo = self.tmr_gpu_addr & 0xffffffff
    cmd.cmd.cmd_setup_tmr.buf_phy_addr_hi = self.tmr_gpu_addr >> 32
    cmd.cmd.cmd_setup_tmr.system_phy_addr_lo = self.tmr_gpu_addr & 0xffffffff # the same for our mappings
    cmd.cmd.cmd_setup_tmr.system_phy_addr_hi = self.tmr_gpu_addr >> 32
    cmd.cmd.cmd_setup_tmr.bitfield.virt_phy_addr = 1
    cmd.cmd.cmd_setup_tmr.buf_size = self.tmr_size

  def prep_boot_config_get(self):
    assert ctypes.sizeof(amdgpu_psp_gfx_if.struct_psp_gfx_cmd_resp) == 1024
    ctypes.memset(self.adev.vmm.vram_to_cpu_addr(self.cmd_buf, 0x1000), 0, 0x1000)
    cmd = amdgpu_psp_gfx_if.struct_psp_gfx_cmd_resp.from_address(self.adev.vmm.vram_to_cpu_addr(self.cmd_buf))
    cmd.cmd_id = amdgpu_psp_gfx_if.GFX_CMD_ID_BOOT_CFG
    cmd.cmd.boot_cfg.sub_cmd = amdgpu_psp_gfx_if.BOOTCFG_CMD_GET

  def ring_get_wptr(self):
    return self.adev.rreg_ip("MP0", 0, amdgpu_mp_13_0_0_offset.regMP0_SMN_C2PMSG_67, amdgpu_mp_13_0_0_offset.regMP0_SMN_C2PMSG_67_BASE_IDX)
  
  def ring_set_wptr(self, wptr):
    self.adev.wreg_ip("MP0", 0, amdgpu_mp_13_0_0_offset.regMP0_SMN_C2PMSG_67, amdgpu_mp_13_0_0_offset.regMP0_SMN_C2PMSG_67_BASE_IDX, wptr)

  def cmd_submit_buf(self):
    prev_wptr = self.ring_get_wptr()
    ring_entry_addr = self.adev.vmm.vram_to_cpu_addr(self.ring_mem + prev_wptr * 4)

    ctypes.memset(ring_entry_addr, 0, ctypes.sizeof(amdgpu_psp_gfx_if.struct_psp_gfx_rb_frame))
    write_loc = amdgpu_psp_gfx_if.struct_psp_gfx_rb_frame.from_address(ring_entry_addr)
    write_loc.cmd_buf_addr_hi = self.cmd_buf >> 32
    write_loc.cmd_buf_addr_lo = self.cmd_buf & 0xffffffff
    write_loc.fence_addr_hi = self.fence_buf >> 32
    write_loc.fence_addr_lo = self.fence_buf & 0xffffffff
    write_loc.fence_value = prev_wptr

    print(prev_wptr, hex(self.fence_buf))
    self.ring_set_wptr(prev_wptr + ctypes.sizeof(amdgpu_psp_gfx_if.struct_psp_gfx_rb_frame) // 4)
    
    fence_view = to_mv(self.adev.vmm.vram_to_cpu_addr(self.fence_buf), 4).cast('I')

    smth = fence_view[0]
    while smth != prev_wptr:
      self.adev.wreg_ip("HDP", 0, 0x00d1, 0x0, 1)
      smth = fence_view[0]

    resp = amdgpu_psp_gfx_if.struct_psp_gfx_cmd_resp.from_address(self.adev.vmm.vram_to_cpu_addr(self.cmd_buf))
    if resp.resp.status != 0:
      print(colored(f"PSP command failed {resp.cmd_id} {resp.resp.status}", "red"))

  def load_smu_fw(self):
    self.prep_load_ip_fw_cmd_buf(self.smu_psp_desc)
    self.cmd_submit_buf()

  def load_tmr(self):
    self.prep_tmr_cmd_buf()
    self.cmd_submit_buf()

  def rlc_autoload_start(self):
    assert ctypes.sizeof(amdgpu_psp_gfx_if.struct_psp_gfx_cmd_resp) == 1024
    ctypes.memset(self.adev.vmm.vram_to_cpu_addr(self.cmd_buf, 0x1000), 0, 0x1000)

    cmd = amdgpu_psp_gfx_if.struct_psp_gfx_cmd_resp.from_address(self.adev.vmm.vram_to_cpu_addr(self.cmd_buf))
    cmd.cmd_id = amdgpu_psp_gfx_if.GFX_CMD_ID_AUTOLOAD_RLC
    self.cmd_submit_buf()

  def hw_start(self):
    self.bootloader_load_sos()
    self.ring_create()

    # TMR
    # TODO: 0x1300000 should be parsed from TOC...
    self.tmr_size = 0x1300000
    self.tmr_gpu_addr = self.adev.vmm.alloc_vram(self.tmr_size, "psp_tmr", align=0x100000) # psp tmr

    # For ASICs with DF Cstate management centralized to PMFW, TMR setup should be performed after PMFW loaded and before other non-psp firmware loaded.
    self.load_smu_fw()
    self.load_tmr()

    for fw in self.fw_list:
      self.prep_load_ip_fw_cmd_buf(fw)
      self.cmd_submit_buf()

  def bootloader_load_sos(self):
    if (self.is_sos_alive()):
      self.init_sos_version()
      print(f"sOS alive, version {self.sos_fw_version}")
      return 0

    assert False, "TODO: Init from bootloader"
