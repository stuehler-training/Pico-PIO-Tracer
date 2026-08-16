import rp2


@rp2.asm_pio(
    autopull=True,
    pull_thresh=32,
    autopush=True,
    push_thresh=32,
    out_shiftdir=rp2.PIO.SHIFT_RIGHT,
    in_shiftdir=rp2.PIO.SHIFT_LEFT,
)
def fifo_loopback():
    wrap_target()
    out(x, 32)
    in_(x, 32)
    wrap()


sm = rp2.StateMachine(0, fifo_loopback, freq=1_000_000)
sm.put(0x12345678)
sm.put(0xA5A5A5A5)
sm.active(1)
