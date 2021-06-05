from cereal import car
from common.numpy_fast import clip, interp
from selfdrive.car import apply_toyota_steer_torque_limits, create_gas_command, make_can_msg
from selfdrive.car.toyota.toyotacan import create_steer_command, create_ui_command, \
                                           create_accel_command, create_acc_cancel_command, \
                                           create_fcw_command, create_lta_steer_command
from selfdrive.car.toyota.values import Ecu, CAR, STATIC_MSGS, NO_STOP_TIMER_CAR, TSS2_CAR, \
                                        MIN_ACC_SPEED, PEDAL_HYST_GAP, CarControllerParams
from opendbc.can.packer import CANPacker

VisualAlert = car.CarControl.HUDControl.VisualAlert


def accel_hysteresis(accel, accel_steady, enabled):

  # for small accel oscillations within ACCEL_HYST_GAP, don't change the accel command
  if not enabled:
    # send 0 when disabled, otherwise acc faults
    accel_steady = 0.
  elif accel > accel_steady + CarControllerParams.ACCEL_HYST_GAP:
    accel_steady = accel - CarControllerParams.ACCEL_HYST_GAP
  elif accel < accel_steady - CarControllerParams.ACCEL_HYST_GAP:
    accel_steady = accel + CarControllerParams.ACCEL_HYST_GAP
  accel = accel_steady

  return accel, accel_steady


def coast_accel(speed: float) -> float:  # given a speed, output coasting acceleration
  points = [[0.01, 0.0], [.21, .425], [.3107, .535], [.431, .555],
            [.777, .438], [1.928, 0.265], [2.66, -0.179],
            [3.336, -0.250], [MIN_ACC_SPEED, -0.145]]
  return interp(speed, *zip(*points))


def compute_gb_pedal(accel: float, speed: float) -> float:
  def accel_to_gas(a_ego, v_ego):
    _a3, _a4, _a5, _a6, _a7, _a9, _s1, _s2, _s3, _offset = [0.002377321579025474, 0.07381215915662231, -0.007963770877144415, 0.15947881013161083, -0.010037975860880363, -0.1334422448911381, 0.0019638460320592194, -0.0018659661194108225, 0.021688122969402018, 0.027007983705385548]

    speed_offset = (_s1 * a_ego + _s2) * v_ego ** 2 + _s3 * v_ego + _offset
    # FIXME: acceleration -> gas should be perfectly linear, HOWEVER acceleration response might change non-linearly based on speed
    # FIXME instead of using a polynomial for accel, how about we use a polynomial on speed and use that as a coefficient for a linear accel function?
    # FIXME: something like this: accel_part = (c1 * v_ego ** 2 + c2 * v_ego + c3) * a_ego
    accel_part = (_a5 * v_ego + _a9) * a_ego ** 2 + _a6 * a_ego
    # accel_part = _a7 * a_ego ** 4 + (_a3 * v_ego + _a4) * a_ego ** 3 + (_a5 * v_ego + _a9) * a_ego ** 2 + _a6 * a_ego  # todo: original accel function

    return accel_part + speed_offset

  gas = 0.
  coast = coast_accel(speed)
  # coast_spread = 0.1
  coast_tolerance = 0.0

  # TODO: see if it works fine without ramping
  if accel > coast - coast_tolerance:
    gas = accel_to_gas(accel, speed)

    # if accel < coast + coast_spread:  # ramp up gas output smoothly from coast accel to coast + spread
    #   gas *= interp(accel, [coast - coast_spread, coast + coast_spread], [0, 1]) ** 2

  # TODO: brakes usually aren't released quick enough, eg. openpilot can be requesting a small amount of speed for quite a while,
  # TODO: but the car never inches up. integral should take care of it, but it doesn't always, and causes a spat of acceleration (jerky)
  # TODO: find a way to smoothly ramp up gas without being too slow, and don't want to hardcode any custom logic
  return gas


class CarController():
  def __init__(self, dbc_name, CP, VM):
    self.last_steer = 0
    self.accel_steady = 0.
    self.alert_active = False
    self.last_standstill = False
    self.standstill_req = False
    self.steer_rate_limited = False
    self.use_interceptor = False

    self.fake_ecus = set()
    if CP.enableCamera:
      self.fake_ecus.add(Ecu.fwdCamera)
    if CP.enableDsu:
      self.fake_ecus.add(Ecu.dsu)

    self.packer = CANPacker(dbc_name)

  def update(self, enabled, CS, frame, actuators, pcm_cancel_cmd, hud_alert,
             left_line, right_line, lead, left_lane_depart, right_lane_depart):

    # *** compute control surfaces ***

    # gas and brake
    interceptor_gas_cmd = 0.
    pcm_accel_cmd = actuators.gas - actuators.brake

    if CS.CP.enableGasInterceptor:
      # handle hysteresis when around the minimum acc speed
      if CS.out.vEgo < MIN_ACC_SPEED:
        self.use_interceptor = True
      elif CS.out.vEgo > MIN_ACC_SPEED + PEDAL_HYST_GAP:
        self.use_interceptor = False

      if self.use_interceptor and enabled:
        # only send negative accel when using interceptor. gas handles acceleration
        # +0.06 offset to reduce ABS pump usage when OP is engaged
        interceptor_gas_cmd = compute_gb_pedal(pcm_accel_cmd * CarControllerParams.ACCEL_SCALE, CS.out.vEgo)
        pcm_accel_cmd = 0.06 - actuators.brake

    interceptor_gas_cmd = clip(interceptor_gas_cmd, 0., 1.)
    pcm_accel_cmd, self.accel_steady = accel_hysteresis(pcm_accel_cmd, self.accel_steady, enabled)
    pcm_accel_cmd = clip(pcm_accel_cmd * CarControllerParams.ACCEL_SCALE, CarControllerParams.ACCEL_MIN, CarControllerParams.ACCEL_MAX)

    # steer torque
    new_steer = int(round(actuators.steer * CarControllerParams.STEER_MAX))
    apply_steer = apply_toyota_steer_torque_limits(new_steer, self.last_steer, CS.out.steeringTorqueEps, CarControllerParams)
    self.steer_rate_limited = new_steer != apply_steer

    # Cut steering while we're in a known fault state (2s)
    if not enabled or CS.steer_state in [9, 25]:
      apply_steer = 0
      apply_steer_req = 0
    else:
      apply_steer_req = 1

    if not enabled and CS.pcm_acc_status:
      # send pcm acc cancel cmd if drive is disabled but pcm is still on, or if the system can't be activated
      pcm_cancel_cmd = 1

    # on entering standstill, send standstill request
    if CS.out.standstill and not self.last_standstill and CS.CP.carFingerprint not in NO_STOP_TIMER_CAR:
      self.standstill_req = True
    if CS.pcm_acc_status != 8:
      # pcm entered standstill or it's disabled
      self.standstill_req = False

    self.last_steer = apply_steer
    self.last_accel = pcm_accel_cmd
    self.last_standstill = CS.out.standstill

    can_sends = []

    #*** control msgs ***
    #print("steer {0} {1} {2} {3}".format(apply_steer, min_lim, max_lim, CS.steer_torque_motor)

    # toyota can trace shows this message at 42Hz, with counter adding alternatively 1 and 2;
    # sending it at 100Hz seem to allow a higher rate limit, as the rate limit seems imposed
    # on consecutive messages
    if Ecu.fwdCamera in self.fake_ecus:
      can_sends.append(create_steer_command(self.packer, apply_steer, apply_steer_req, frame))
      if frame % 2 == 0 and CS.CP.carFingerprint in TSS2_CAR:
        can_sends.append(create_lta_steer_command(self.packer, 0, 0, frame // 2))

      # LTA mode. Set ret.steerControlType = car.CarParams.SteerControlType.angle and whitelist 0x191 in the panda
      # if frame % 2 == 0:
      #   can_sends.append(create_steer_command(self.packer, 0, 0, frame // 2))
      #   can_sends.append(create_lta_steer_command(self.packer, actuators.steeringAngleDeg, apply_steer_req, frame // 2))

    # we can spam can to cancel the system even if we are using lat only control
    if (frame % 3 == 0 and CS.CP.openpilotLongitudinalControl) or (pcm_cancel_cmd and Ecu.fwdCamera in self.fake_ecus):
      lead = lead or CS.out.vEgo < 12.    # at low speed we always assume the lead is present do ACC can be engaged

      # Lexus IS uses a different cancellation message
      if pcm_cancel_cmd and CS.CP.carFingerprint == CAR.LEXUS_IS:
        can_sends.append(create_acc_cancel_command(self.packer))
      elif CS.CP.openpilotLongitudinalControl:
        can_sends.append(create_accel_command(self.packer, pcm_accel_cmd, pcm_cancel_cmd, self.standstill_req, lead))
      else:
        can_sends.append(create_accel_command(self.packer, 0, pcm_cancel_cmd, False, lead))

    if frame % 2 == 0 and CS.CP.enableGasInterceptor:
      # send exactly zero if gas cmd is zero. Interceptor will send the max between read value and gas cmd.
      # This prevents unexpected pedal range rescaling
      can_sends.append(create_gas_command(self.packer, interceptor_gas_cmd, frame // 2))

    # ui mesg is at 100Hz but we send asap if:
    # - there is something to display
    # - there is something to stop displaying
    fcw_alert = hud_alert == VisualAlert.fcw
    steer_alert = hud_alert in [VisualAlert.steerRequired, VisualAlert.ldw]

    send_ui = False
    if ((fcw_alert or steer_alert) and not self.alert_active) or \
       (not (fcw_alert or steer_alert) and self.alert_active):
      send_ui = True
      self.alert_active = not self.alert_active
    elif pcm_cancel_cmd:
      # forcing the pcm to disengage causes a bad fault sound so play a good sound instead
      send_ui = True

    if (frame % 100 == 0 or send_ui) and Ecu.fwdCamera in self.fake_ecus:
      can_sends.append(create_ui_command(self.packer, steer_alert, pcm_cancel_cmd, left_line, right_line, left_lane_depart, right_lane_depart))

    if frame % 100 == 0 and Ecu.dsu in self.fake_ecus:
      can_sends.append(create_fcw_command(self.packer, fcw_alert))

    #*** static msgs ***

    for (addr, ecu, cars, bus, fr_step, vl) in STATIC_MSGS:
      if frame % fr_step == 0 and ecu in self.fake_ecus and CS.CP.carFingerprint in cars:
        can_sends.append(make_can_msg(addr, vl, bus))

    return can_sends
