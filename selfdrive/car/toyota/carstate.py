from cereal import car
from common.conversions import Conversions as CV
from common.numpy_fast import mean
from common.filter_simple import FirstOrderFilter
from common.realtime import DT_CTRL
from opendbc.can.can_define import CANDefine
from opendbc.can.parser import CANParser
from selfdrive.car.interfaces import CarStateBase
from selfdrive.car.toyota.values import ToyotaFlags, CAR, DBC, STEER_THRESHOLD, NO_STOP_TIMER_CAR, TSS2_CAR, RADAR_ACC_CAR, EPS_SCALE, FEATURES
from common.params import Params
from common.realtime import DT_CTRL
import time
_TRAFFIC_SINGAL_MAP = {
  1: "kph",
  36: "mph",
  65: "No overtake",
  66: "No overtake"
}


class CarState(CarStateBase):
  def __init__(self, CP):
    super().__init__(CP)
    can_define = CANDefine(DBC[CP.carFingerprint]["pt"])
    self.shifter_values = can_define.dv["GEAR_PACKET"]["GEAR"]
    self.eps_torque_scale = EPS_SCALE[CP.carFingerprint] / 100.

    # On cars with cp.vl["STEER_TORQUE_SENSOR"]["STEER_ANGLE"]
    # the signal is zeroed to where the steering angle is at start.
    # Need to apply an offset as soon as the steering angle measurements are both received
    self.accurate_steer_angle_seen = False
    self.angle_offset = FirstOrderFilter(None, 60.0, DT_CTRL, initialized=False)
    self._init_traffic_signals()

    self.low_speed_lockout = False
    self.acc_type = 1

    self.params = Params()
    self.enable_mads = self.params.get_bool("EnableMads")
    self.mads_disengage_lateral_on_brake = self.params.get_bool("DisengageLateralOnBrake")

    self.accEnabled = False
    self.madsEnabled = False
    self.leftBlinkerOn = False
    self.rightBlinkerOn = False
    self.disengageByBrake = False
    self.belowLaneChangeSpeed = True

    self.mads_enabled = None
    self.prev_mads_enabled = None
    self.lkas_enabled = None
    self.prev_lkas_enabled = None

    self.cruise_buttons = 0
    self.prev_cruise_buttons = 0

    self.prev_cruiseState_enabled = False

    self.acc_mads_combo = None
    self.prev_acc_mads_combo = None

    self.persistLkasIconDisabled = None

    self.distance_traveled = 0.

    self.cruiseState_standstill = False

    self.prev_gap_adjust_cruise_tr = 0
    self.gap_adjust_cruise_tr_line = 0

    self.reverse_acc_change = 1

  def update(self, cp, cp_cam):
    ret = car.CarState.new_message()

    # update prevs, update must run once per loop
    self.prev_cruise_buttons = self.cruise_buttons
    self.prev_mads_enabled = self.mads_enabled
    self.prev_lkas_enabled = self.lkas_enabled

    self.acc_mads_combo = self.params.get_bool("AccMadsCombo")
    self.visual_brake_lights = self.params.get_bool("BrakeLights")
    self.gap_adjust_cruise = Params().get_bool("GapAdjustCruise")
    self.gap_adjust_cruise_mode = int(Params().get("GapAdjustCruiseMode"))
    self._reverse_acc_change = self.params.get_bool("ReverseAccChange")

    ret.doorOpen = any([cp.vl["BODY_CONTROL_STATE"]["DOOR_OPEN_FL"], cp.vl["BODY_CONTROL_STATE"]["DOOR_OPEN_FR"],
                        cp.vl["BODY_CONTROL_STATE"]["DOOR_OPEN_RL"], cp.vl["BODY_CONTROL_STATE"]["DOOR_OPEN_RR"]])
    ret.seatbeltUnlatched = cp.vl["BODY_CONTROL_STATE"]["SEATBELT_DRIVER_UNLATCHED"] != 0
    ret.parkingBrake = cp.vl["BODY_CONTROL_STATE"]["PARKING_BRAKE"] == 1

    ret.brakePressed = cp.vl["BRAKE_MODULE"]["BRAKE_PRESSED"] != 0
    ret.brakeHoldActive = cp.vl["ESP_CONTROL"]["BRAKE_HOLD_ACTIVE"] == 1
    ret.brakeLights = bool(self.visual_brake_lights and (cp.vl["ESP_CONTROL"]["BRAKE_LIGHTS_ACC"] or ret.brakePressed or ret.brakeHoldActive or ret.parkingBrake))
    if self.CP.enableGasInterceptor:
      ret.gas = (cp.vl["GAS_SENSOR"]["INTERCEPTOR_GAS"] + cp.vl["GAS_SENSOR"]["INTERCEPTOR_GAS2"]) // 2
      ret.gasPressed = ret.gas > 805
    else:
      # TODO: find a new, common signal
      msg = "GAS_PEDAL_HYBRID" if (self.CP.flags & ToyotaFlags.HYBRID) else "GAS_PEDAL"
      ret.gas = cp.vl[msg]["GAS_PEDAL"]
      ret.gasPressed = cp.vl["PCM_CRUISE"]["GAS_RELEASED"] == 0

    ret.wheelSpeeds = self.get_wheel_speeds(
      cp.vl["WHEEL_SPEEDS"]["WHEEL_SPEED_FL"],
      cp.vl["WHEEL_SPEEDS"]["WHEEL_SPEED_FR"],
      cp.vl["WHEEL_SPEEDS"]["WHEEL_SPEED_RL"],
      cp.vl["WHEEL_SPEEDS"]["WHEEL_SPEED_RR"],
    )
    ret.vEgoRaw = mean([ret.wheelSpeeds.fl, ret.wheelSpeeds.fr, ret.wheelSpeeds.rl, ret.wheelSpeeds.rr])
    ret.vEgo, ret.aEgo = self.update_speed_kf(ret.vEgoRaw)

    self.belowLaneChangeSpeed = ret.vEgo < (30 * CV.MPH_TO_MS)

    self.cruise_buttons = cp.vl["PCM_CRUISE"]["CRUISE_STATE"]
    if self.CP.carFingerprint in FEATURES["use_lta_msg"]:
      self.lkas_enabled = cp_cam.vl["LKAS_HUD"]["LDA_ON_MESSAGE"]
      self.persistLkasIconDisabled = cp_cam.vl["LKAS_HUD"]["LKAS_STATUS"] == 1
    else:
      self.lkas_enabled = cp_cam.vl["LKAS_HUD"]["LKAS_STATUS"]
      self.persistLkasIconDisabled = cp_cam.vl["LKAS_HUD"]["LKAS_STATUS"] == 0

    if self.prev_lkas_enabled is None:
      self.prev_lkas_enabled = self.lkas_enabled

    ret.standstill = ret.vEgoRaw < 0.001
    ret.standStill = self.CP.standStill

    ret.steeringAngleDeg = cp.vl["STEER_ANGLE_SENSOR"]["STEER_ANGLE"] + cp.vl["STEER_ANGLE_SENSOR"]["STEER_FRACTION"]
    torque_sensor_angle_deg = cp.vl["STEER_TORQUE_SENSOR"]["STEER_ANGLE"]

    # On some cars, the angle measurement is non-zero while initializing
    if abs(torque_sensor_angle_deg) > 1e-3 and not bool(cp.vl["STEER_TORQUE_SENSOR"]["STEER_ANGLE_INITIALIZING"]):
      self.accurate_steer_angle_seen = True

    if self.accurate_steer_angle_seen:
      # Offset seems to be invalid for large steering angles
      if abs(ret.steeringAngleDeg) < 90 and cp.can_valid:
        self.angle_offset.update(torque_sensor_angle_deg - ret.steeringAngleDeg)

      if self.angle_offset.initialized:
        ret.steeringAngleOffsetDeg = self.angle_offset.x
        ret.steeringAngleDeg = torque_sensor_angle_deg - self.angle_offset.x

    ret.steeringRateDeg = cp.vl["STEER_ANGLE_SENSOR"]["STEER_RATE"]

    can_gear = int(cp.vl["GEAR_PACKET"]["GEAR"])
    ret.gearShifter = self.parse_gear_shifter(self.shifter_values.get(can_gear, None))
    ret.leftBlinker = cp.vl["BLINKERS_STATE"]["TURN_SIGNALS"] == 1
    ret.rightBlinker = cp.vl["BLINKERS_STATE"]["TURN_SIGNALS"] == 2

    self.leftBlinkerOn = cp.vl["BLINKERS_STATE"]["TURN_SIGNALS"] == 1
    self.rightBlinkerOn = cp.vl["BLINKERS_STATE"]["TURN_SIGNALS"] == 2

    ret.steeringTorque = cp.vl["STEER_TORQUE_SENSOR"]["STEER_TORQUE_DRIVER"]
    ret.steeringTorqueEps = cp.vl["STEER_TORQUE_SENSOR"]["STEER_TORQUE_EPS"] * self.eps_torque_scale
    # we could use the override bit from dbc, but it's triggered at too high torque values
    ret.steeringPressed = abs(ret.steeringTorque) > STEER_THRESHOLD

    self.reverse_acc_change = 2 if self._reverse_acc_change else 1

    if self.CP.carFingerprint in (CAR.LEXUS_IS, CAR.LEXUS_RC):
      ret.cruiseState.available = cp.vl["DSU_CRUISE"]["MAIN_ON"] != 0
      ret.cruiseState.speed = cp.vl["DSU_CRUISE"]["SET_SPEED"] * CV.KPH_TO_MS
    else:
      ret.cruiseState.available = cp.vl["PCM_CRUISE_2"]["MAIN_ON"] != 0
      ret.cruiseState.speed = cp.vl["PCM_CRUISE_2"]["SET_SPEED"] * CV.KPH_TO_MS

    self.mads_enabled = ret.cruiseState.available
    time.sleep(0.2)
    if self.prev_mads_enabled is None:
      self.prev_mads_enabled = self.mads_enabled

    if self.CP.carFingerprint in RADAR_ACC_CAR:
      self.acc_type = 1
      ret.stockFcw = bool(cp.vl["ACC_HUD"]["FCW"])
    elif self.CP.carFingerprint in TSS2_CAR:
      self.acc_type = 1
      ret.stockFcw = bool(cp_cam.vl["ACC_HUD"]["FCW"])

    # some TSS2 cars have low speed lockout permanently set, so ignore on those cars
    # these cars are identified by an ACC_TYPE value of 2.
    # TODO: it is possible to avoid the lockout and gain stop and go if you
    # send your own ACC_CONTROL msg on startup with ACC_TYPE set to 1
    if (self.CP.carFingerprint not in TSS2_CAR and self.CP.carFingerprint not in (CAR.LEXUS_IS, CAR.LEXUS_RC)) or \
       (self.CP.carFingerprint in TSS2_CAR and self.acc_type == 1):
      self.low_speed_lockout = cp.vl["PCM_CRUISE_2"]["LOW_SPEED_LOCKOUT"] == 2

    self.pcm_acc_status = cp.vl["PCM_CRUISE"]["CRUISE_STATE"]
    if self.CP.carFingerprint in NO_STOP_TIMER_CAR or self.CP.enableGasInterceptor:
      # ignore standstill in hybrid vehicles, since pcm allows to restart without
      # receiving any special command. Also if interceptor is detected
      ret.cruiseState.standstill = False
    else:
      ret.cruiseState.standstill = self.pcm_acc_status == 7
    ret.cruiseState.enabled = bool(cp.vl["PCM_CRUISE"]["CRUISE_ACTIVE"])
    ret.cruiseState.nonAdaptive = cp.vl["PCM_CRUISE"]["CRUISE_STATE"] in (1, 2, 3, 4, 5, 6)

    self.cruiseState_standstill = ret.cruiseState.standstill

    if ret.cruiseState.available:
      if self.gap_adjust_cruise:
        if self.gap_adjust_cruise_mode == 0 or self.gap_adjust_cruise_mode == 2:
          if self.CP.carFingerprint in RADAR_ACC_CAR:
            self.gap_adjust_cruise_tr_line = 1 if cp.vl["ACC_CONTROL"]["DISTANCE"] == 1 else 0
          elif self.CP.carFingerprint in TSS2_CAR:
            self.gap_adjust_cruise_tr_line = 1 if cp_cam.vl["ACC_CONTROL"]["DISTANCE"] == 1 else 0
          elif self.CP.smartDsu:
            self.gap_adjust_cruise_tr_line = 1 if cp.vl["SDSU"]["FD_BUTTON"] == 1 else 0
          gap_adjust_cruise_tr = int(cp.vl["PCM_CRUISE_SM"]["DISTANCE_LINES"]) - 1
          if gap_adjust_cruise_tr in range(3) and gap_adjust_cruise_tr != self.prev_gap_adjust_cruise_tr:
            ret.gapAdjustCruiseTr = gap_adjust_cruise_tr
            self.prev_gap_adjust_cruise_tr = gap_adjust_cruise_tr
            Params().put("GapAdjustCruiseTr", str(self.prev_gap_adjust_cruise_tr))
        elif self.gap_adjust_cruise_mode == 1:
          ret.gapAdjustCruiseTr = int(Params().get("GapAdjustCruiseTr"))
      if self.enable_mads:
        if not self.prev_mads_enabled and self.mads_enabled:
          self.madsEnabled = True
        if self.CP.carFingerprint in FEATURES["use_lta_msg"]:
          if (self.prev_lkas_enabled != 1 and self.lkas_enabled == 1) or \
             (self.prev_lkas_enabled != 2 and self.lkas_enabled == 2):
            self.madsEnabled = not self.madsEnabled
        else:
          if not self.prev_lkas_enabled and self.lkas_enabled:
            self.madsEnabled = True
          elif self.prev_lkas_enabled == 1 and not self.lkas_enabled:
            self.madsEnabled = False
        if self.acc_mads_combo:
          if not self.prev_acc_mads_combo and ret.cruiseState.enabled:
            self.madsEnabled = True
          self.prev_acc_mads_combo = ret.cruiseState.enabled
    else:
      self.madsEnabled = False

    if self.prev_cruise_buttons != 0: # CANCEL
      if self.cruise_buttons == 0:
        if not self.enable_mads:
          self.madsEnabled = False
    if ret.brakePressed:
      if not self.enable_mads:
        self.madsEnabled = False

    if not self.enable_mads:
      if ret.cruiseState.enabled and not self.prev_cruiseState_enabled:
        self.madsEnabled = True
    self.prev_cruiseState_enabled = ret.cruiseState.enabled

    ret.steerFaultTemporary = False
    ret.steerFaultPermanent = False

    if self.madsEnabled:
      if (not self.belowLaneChangeSpeed and (self.rightBlinkerOn or self.leftBlinkerOn)) or\
        not (self.rightBlinkerOn or self.leftBlinkerOn):
        # steer rate fault, goes to 21 or 25 for 1 frame, then 9 for ~2 seconds
        ret.steerFaultTemporary = cp.vl["EPS_STATUS"]["LKA_STATE"] in (0, 9, 21, 25)
        # 17 is a fault from a prolonged high torque delta between cmd and user
        ret.steerFaultPermanent = cp.vl["EPS_STATUS"]["LKA_STATE"] == 17

    ret.latActive = self.CP.latActive

    self.distance_traveled += ret.vEgo * DT_CTRL
    ret.distanceTraveled = self.distance_traveled

    ret.genericToggle = bool(cp.vl["LIGHT_STALK"]["AUTO_HIGH_BEAM"])
    ret.stockAeb = bool(cp_cam.vl["PRE_COLLISION"]["PRECOLLISION_ACTIVE"] and cp_cam.vl["PRE_COLLISION"]["FORCE"] < -1e-5)

    ret.espDisabled = cp.vl["ESP_CONTROL"]["TC_DISABLED"] != 0

    if self.CP.enableBsm:
      ret.leftBlindspot = (cp.vl["BSM"]["L_ADJACENT"] == 1) or (cp.vl["BSM"]["L_APPROACHING"] == 1)
      ret.rightBlindspot = (cp.vl["BSM"]["R_ADJACENT"] == 1) or (cp.vl["BSM"]["R_APPROACHING"] == 1)

    self._update_traffic_signals(cp_cam)
    ret.cruiseState.speedLimit = self._calculate_speed_limit()

    return ret

  def _init_traffic_signals(self):
    self._tsgn1 = None
    self._spdval1 = None
    self._splsgn1 = None
    self._tsgn2 = None
    self._splsgn2 = None
    self._tsgn3 = None
    self._splsgn3 = None
    self._tsgn4 = None
    self._splsgn4 = None

  def _update_traffic_signals(self, cp_cam):
    # Print out car signals for traffic signal detection
    tsgn1 = cp_cam.vl["RSA1"]['TSGN1']
    spdval1 = cp_cam.vl["RSA1"]['SPDVAL1']
    splsgn1 = cp_cam.vl["RSA1"]['SPLSGN1']
    tsgn2 = cp_cam.vl["RSA1"]['TSGN2']
    splsgn2 = cp_cam.vl["RSA1"]['SPLSGN2']
    tsgn3 = cp_cam.vl["RSA2"]['TSGN3']
    splsgn3 = cp_cam.vl["RSA2"]['SPLSGN3']
    tsgn4 = cp_cam.vl["RSA2"]['TSGN4']
    splsgn4 = cp_cam.vl["RSA2"]['SPLSGN4']

    has_changed = tsgn1 != self._tsgn1 \
      or spdval1 != self._spdval1 \
      or splsgn1 != self._splsgn1 \
      or tsgn2 != self._tsgn2 \
      or splsgn2 != self._splsgn2 \
      or tsgn3 != self._tsgn3 \
      or splsgn3 != self._splsgn3 \
      or tsgn4 != self._tsgn4 \
      or splsgn4 != self._splsgn4

    self._tsgn1 = tsgn1
    self._spdval1 = spdval1
    self._splsgn1 = splsgn1
    self._tsgn2 = tsgn2
    self._splsgn2 = splsgn2
    self._tsgn3 = tsgn3
    self._splsgn3 = splsgn3
    self._tsgn4 = tsgn4
    self._splsgn4 = splsgn4

    if not has_changed:
      return

    print('---- TRAFFIC SIGNAL UPDATE -----')
    if tsgn1 is not None and tsgn1 != 0:
      print(f'TSGN1: {self._traffic_signal_description(tsgn1)}')
    if spdval1 is not None and spdval1 != 0:
      print(f'SPDVAL1: {spdval1}')
    if splsgn1 is not None and splsgn1 != 0:
      print(f'SPLSGN1: {splsgn1}')
    if tsgn2 is not None and tsgn2 != 0:
      print(f'TSGN2: {self._traffic_signal_description(tsgn2)}')
    if splsgn2 is not None and splsgn2 != 0:
      print(f'SPLSGN2: {splsgn2}')
    if tsgn3 is not None and tsgn3 != 0:
      print(f'TSGN3: {self._traffic_signal_description(tsgn3)}')
    if splsgn3 is not None and splsgn3 != 0:
      print(f'SPLSGN3: {splsgn3}')
    if tsgn4 is not None and tsgn4 != 0:
      print(f'TSGN4: {self._traffic_signal_description(tsgn4)}')
    if splsgn4 is not None and splsgn4 != 0:
      print(f'SPLSGN4: {splsgn4}')
    print('------------------------')

  def _traffic_signal_description(self, tsgn):
    desc = _TRAFFIC_SINGAL_MAP.get(int(tsgn))
    return f'{tsgn}: {desc}' if desc is not None else f'{tsgn}'

  def _calculate_speed_limit(self):
    if self._tsgn1 == 1:
      return self._spdval1 * CV.KPH_TO_MS
    if self._tsgn1 == 36:
      return self._spdval1 * CV.MPH_TO_MS
    return 0

  @staticmethod
  def get_can_parser(CP):
    signals = [
      # sig_name, sig_address
      ("STEER_ANGLE", "STEER_ANGLE_SENSOR"),
      ("GEAR", "GEAR_PACKET"),
      ("BRAKE_PRESSED", "BRAKE_MODULE"),
      ("WHEEL_SPEED_FL", "WHEEL_SPEEDS"),
      ("WHEEL_SPEED_FR", "WHEEL_SPEEDS"),
      ("WHEEL_SPEED_RL", "WHEEL_SPEEDS"),
      ("WHEEL_SPEED_RR", "WHEEL_SPEEDS"),
      ("DOOR_OPEN_FL", "BODY_CONTROL_STATE"),
      ("DOOR_OPEN_FR", "BODY_CONTROL_STATE"),
      ("DOOR_OPEN_RL", "BODY_CONTROL_STATE"),
      ("DOOR_OPEN_RR", "BODY_CONTROL_STATE"),
      ("SEATBELT_DRIVER_UNLATCHED", "BODY_CONTROL_STATE"),
      ("PARKING_BRAKE", "BODY_CONTROL_STATE"),
      ("TC_DISABLED", "ESP_CONTROL"),
      ("BRAKE_HOLD_ACTIVE", "ESP_CONTROL"),
      ("BRAKE_LIGHTS_ACC", "ESP_CONTROL", 1),
      ("STEER_FRACTION", "STEER_ANGLE_SENSOR"),
      ("STEER_RATE", "STEER_ANGLE_SENSOR"),
      ("CRUISE_ACTIVE", "PCM_CRUISE"),
      ("CRUISE_STATE", "PCM_CRUISE"),
      ("GAS_RELEASED", "PCM_CRUISE"),
      ("STEER_TORQUE_DRIVER", "STEER_TORQUE_SENSOR"),
      ("STEER_TORQUE_EPS", "STEER_TORQUE_SENSOR"),
      ("STEER_ANGLE", "STEER_TORQUE_SENSOR"),
      ("STEER_ANGLE_INITIALIZING", "STEER_TORQUE_SENSOR"),
      ("TURN_SIGNALS", "BLINKERS_STATE"),
      ("LKA_STATE", "EPS_STATUS"),
      ("AUTO_HIGH_BEAM", "LIGHT_STALK"),
    ]

    checks = [
      ("GEAR_PACKET", 1),
      ("LIGHT_STALK", 1),
      ("BLINKERS_STATE", 0.15),
      ("BODY_CONTROL_STATE", 3),
      ("ESP_CONTROL", 3),
      ("EPS_STATUS", 25),
      ("BRAKE_MODULE", 40),
      ("WHEEL_SPEEDS", 80),
      ("STEER_ANGLE_SENSOR", 80),
      ("PCM_CRUISE", 33),
      ("STEER_TORQUE_SENSOR", 50),
    ]

    if CP.flags & ToyotaFlags.HYBRID:
      signals.append(("GAS_PEDAL", "GAS_PEDAL_HYBRID"))
      checks.append(("GAS_PEDAL_HYBRID", 33))
    else:
      signals.append(("GAS_PEDAL", "GAS_PEDAL"))
      checks.append(("GAS_PEDAL", 33))

    if CP.carFingerprint in (CAR.LEXUS_IS, CAR.LEXUS_RC):
      signals.append(("MAIN_ON", "DSU_CRUISE"))
      signals.append(("SET_SPEED", "DSU_CRUISE"))
      checks.append(("DSU_CRUISE", 5))
    else:
      signals.append(("MAIN_ON", "PCM_CRUISE_2"))
      signals.append(("SET_SPEED", "PCM_CRUISE_2"))
      signals.append(("LOW_SPEED_LOCKOUT", "PCM_CRUISE_2"))
      checks.append(("PCM_CRUISE_2", 33))

    # add gas interceptor reading if we are using it
    if CP.enableGasInterceptor:
      signals.append(("INTERCEPTOR_GAS", "GAS_SENSOR"))
      signals.append(("INTERCEPTOR_GAS2", "GAS_SENSOR"))
      checks.append(("GAS_SENSOR", 50))

    if CP.enableBsm:
      signals += [
        ("L_ADJACENT", "BSM"),
        ("L_APPROACHING", "BSM"),
        ("R_ADJACENT", "BSM"),
        ("R_APPROACHING", "BSM"),
      ]
      checks.append(("BSM", 1))

    signals.append(("DISTANCE_LINES", "PCM_CRUISE_SM", 0))
    checks.append(("PCM_CRUISE_SM", 1))

    if CP.smartDsu:
      signals.append(("FD_BUTTON", "SDSU", 0))
      checks.append(("SDSU", 33))

    if CP.carFingerprint in RADAR_ACC_CAR:
      signals += [
        ("ACC_TYPE", "ACC_CONTROL"),
        ("FCW", "ACC_HUD"),
      ]
      checks += [
        ("ACC_CONTROL", 33),
        ("ACC_HUD", 1),
      ]

      signals.append(("DISTANCE", "ACC_CONTROL", 0))

    return CANParser(DBC[CP.carFingerprint]["pt"], signals, checks, 0)

  @staticmethod
  def get_cam_can_parser(CP):
    signals = [
      ("FORCE", "PRE_COLLISION"),
      ("PRECOLLISION_ACTIVE", "PRE_COLLISION"),
      ("LKAS_STATUS", "LKAS_HUD"),
      ("LDA_ON_MESSAGE", "LKAS_HUD"),
    ]

    # Include traffic singal signals.
    signals += [
      ("TSGN1", "RSA1", 0),
      ("SPDVAL1", "RSA1", 0),
      ("SPLSGN1", "RSA1", 0),
      ("TSGN2", "RSA1", 0),
      ("SPLSGN2", "RSA1", 0),
      ("TSGN3", "RSA2", 0),
      ("SPLSGN3", "RSA2", 0),
      ("TSGN4", "RSA2", 0),
      ("SPLSGN4", "RSA2", 0),
    ]

    # use steering message to check if panda is connected to frc
    checks = [
      ("STEERING_LKA", 42),
      ("RSA1", 0),
      ("RSA2", 0),
      ("PRE_COLLISION", 0), # TODO: figure out why freq is inconsistent
      ("LKAS_HUD", 1),
    ]

    if CP.carFingerprint in (TSS2_CAR - RADAR_ACC_CAR):
      signals += [
        ("ACC_TYPE", "ACC_CONTROL"),
        ("FCW", "ACC_HUD"),
      ]
      checks += [
        ("ACC_CONTROL", 33),
        ("ACC_HUD", 1),
      ]

      signals.append(("DISTANCE", "ACC_CONTROL", 0))

    return CANParser(DBC[CP.carFingerprint]["pt"], signals, checks, 2)
