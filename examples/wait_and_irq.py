import rp2
from machine import Pin


@rp2.asm_pio(set_init=rp2.PIO.OUT_LOW)
def wait_and_irq():
    wrap_target()
    wait(1, pin, 0)
    set(pins, 1)
    irq(0)
    wait(0, pin, 0)
    set(pins, 0)
    irq(clear, 0)
    wrap()


sm = rp2.StateMachine(1, wait_and_irq, freq=1_000_000, in_base=Pin(2), set_base=Pin(3))
sm.active(1)
