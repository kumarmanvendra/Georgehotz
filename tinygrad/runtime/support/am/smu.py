import os, ctypes, time
from tinygrad.runtime.autogen import libpciaccess, amdgpu_2, amdgpu_smu_v13_0_0
from tinygrad.helpers import to_mv, mv_address, colored

mmMP1_SMN_C2PMSG_82 = 0x0292
mmMP1_SMN_C2PMSG_90 = 0x029a
mmMP1_SMN_C2PMSG_75 = 0x028b
mmMP1_SMN_C2PMSG_53 = 0x0275
mmMP1_SMN_C2PMSG_54 = 0x0276
mmMP1_SMN_C2PMSG_66 = 0x0282

class SMU_IP:
  def __init__(self, adev):
    self.adev = adev

    self.driver_table_pm = self.adev.mm.palloc(0x4000)
    self.tool_table_pm = self.adev.mm.palloc(0x19000)

    self.param_reg = self.adev.ip_base("MP1", 0, 0) + mmMP1_SMN_C2PMSG_82
    self.msg_reg = self.adev.ip_base("MP1", 0, 0) + mmMP1_SMN_C2PMSG_66
    self.resp_reg = self.adev.ip_base("MP1", 0, 0) + mmMP1_SMN_C2PMSG_90

  def smu_check_reg_resp(self, val): assert val == 1
  def smu_cmn_poll_stat(self):
    # TODO: as reg_wait
    timeout = self.adev.usec_timeout * 20
    for _ in range(timeout):
      reg = self.adev.rreg(self.resp_reg)
      if (reg & 0xFFFFFFFF) != 0: break
      time.sleep(0.000001)
    return reg

  def smu_cmn_send_msg(self, msg, param=0):
    self.adev.wreg(self.resp_reg, 0)
    self.adev.wreg(self.param_reg, param)
    self.adev.wreg(self.msg_reg, msg)

  def smu_cmn_send_smc_msg_with_param(self, msg, param, poll=True, read_back_arg=False):
    if poll:
      self.smu_check_reg_resp(self.smu_cmn_poll_stat())
    
    self.smu_cmn_send_msg(msg, param)
    self.smu_check_reg_resp(self.smu_cmn_poll_stat())

    if read_back_arg:
      return self.adev.rreg(self.param_reg)
    return None

  def mode1_reset(self):
    self.smu_cmn_send_smc_msg_with_param(amdgpu_smu_v13_0_0.PPSMC_MSG_Mode1Reset, 0, poll=True)

  def smu_start_smc_engine(self):
    pass

  def smu_set_driver_table_location(self):
    self.smu_cmn_send_smc_msg_with_param(amdgpu_smu_v13_0_0.PPSMC_MSG_SetDriverDramAddrHigh, self.driver_table_pm.mc_addr() >> 32, poll=True)
    self.smu_cmn_send_smc_msg_with_param(amdgpu_smu_v13_0_0.PPSMC_MSG_SetDriverDramAddrLow, self.driver_table_pm.mc_addr() & 0xFFFFFFFF, poll=True)

  def smu_set_tool_table_location(self):
    self.smu_cmn_send_smc_msg_with_param(amdgpu_smu_v13_0_0.PPSMC_MSG_SetToolsDramAddrHigh, self.tool_table_pm.mc_addr() >> 32, poll=True)
    self.smu_cmn_send_smc_msg_with_param(amdgpu_smu_v13_0_0.PPSMC_MSG_SetToolsDramAddrLow, self.tool_table_pm.mc_addr() & 0xFFFFFFFF, poll=True)

  def smu_setup_pptable(self):
    pass

  def smu_run_btc(self):
    self.smu_cmn_send_smc_msg_with_param(amdgpu_smu_v13_0_0.PPSMC_MSG_RunDcBtc, 0, poll=True)

  def smu_system_features_control(self, enable):
    cmd = amdgpu_smu_v13_0_0.PPSMC_MSG_EnableAllSmuFeatures if enable else amdgpu_smu_v13_0_0.PPSMC_MSG_DisableAllSmuFeatures
    self.smu_cmn_send_smc_msg_with_param(cmd, 0, poll=True)

  def smu_smc_hw_setup(self):
    self.smu_set_driver_table_location()
    self.smu_set_tool_table_location()

    self.smu_run_btc()
    self.smu_system_features_control(True)

  def smu_set_power_profile(self, enabled):
    self.smu_cmn_send_smc_msg_with_param(amdgpu_smu_v13_0_0.PPSMC_MSG_SetWorkloadMask, 0x24 if enabled else 0x1, poll=True)

    for clck in [0x000009B2, 0x000204E1, 0x000105DC, 0x00050B76, 0x00070B76, 0x00040898, 0x00060898, 0x000308FD]:
      self.smu_cmn_send_smc_msg_with_param(amdgpu_smu_v13_0_0.PPSMC_MSG_SetSoftMaxByFreq, clck, poll=True)
      self.smu_cmn_send_smc_msg_with_param(amdgpu_smu_v13_0_0.PPSMC_MSG_SetSoftMinByFreq, clck, poll=True)


  def init(self):
    print("SMU init")
    self.smu_smc_hw_setup()
