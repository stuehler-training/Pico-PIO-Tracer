import rp2
from machine import Pin


@rp2.asm_pio(set_init=rp2.PIO.OUT_LOW)
def blink():
    # One high instruction cycle + 31 delay cycles, then the same low.
    wrap_target()
    set(pins, 1)[31]
    set(pins, 0)[31]
    wrap()


sm = rp2.StateMachine(0, blink, freq=2_000, set_base=Pin(25))
sm.active(1)
