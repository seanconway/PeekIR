from time import sleep
from rpi_hardware_pwm import HardwarePWM

CHIP = 0        # try 0 first
CHANNEL = 1     # GPIO13 should be PWM0_CHAN1

p = HardwarePWM(pwm_channel=CHANNEL, hz=5, chip=CHIP)
p.start(50)

print("Running 5 Hz PWM for 10 seconds...")
sleep(10)

p.stop()
print("Done.")
