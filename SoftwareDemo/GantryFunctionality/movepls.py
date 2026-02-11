import RPi.GPIO as GPIO
import time

PUL = 13
DIR = 6

GPIO.setmode(GPIO.BCM)
GPIO.setup(PUL, GPIO.OUT)
GPIO.setup(DIR, GPIO.OUT)

# Set speed (Smaller number = Faster)
# 0.0005 is a good start. 0.0001 might work with 24V and higher current settings.
delay = 0.0005

try:
    GPIO.output(DIR, GPIO.HIGH)
    print("Moving fast with 24V...")
    # 200 steps for one rotation (since all your DIPs are ON)
    for _ in range(200):
        GPIO.output(PUL, GPIO.HIGH)
        time.sleep(delay)
        GPIO.output(PUL, GPIO.LOW)
        time.sleep(delay)

finally:
    GPIO.cleanup()