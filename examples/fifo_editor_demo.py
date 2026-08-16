import rp2


@rp2.asm_pio(out_shiftdir=rp2.PIO.SHIFT_RIGHT)
def fifo_editor_demo():
    """Move the low byte of each TX word into RX.

    Use the HTML report's manual FIFO editor to add TX words, inject RX words,
    or host-read the RX front word while stepping through the source.
    """
    wrap_target()
    pull(block)
    out(x, 8)
    mov(isr, x)
    push(block)
    wrap()


@rp2.asm_pio()
def irq_editor_demo():
    """Demonstrate WAIT IRQ consumption and blocking IRQ handshakes.

    Trigger IRQ0 to release the first WAIT. The following irq(block, 1)
    sets IRQ1 and stalls until IRQ1 is cleared with the HTML IRQ editor.
    """
    wrap_target()
    wait(1, irq, 0)
    set(x, 1)
    irq(block, 1)
    set(x, 2)
    wrap()


sm = rp2.StateMachine(0, fifo_editor_demo, freq=1_000_000)
irq_sm = rp2.StateMachine(1, irq_editor_demo, freq=1_000_000)
sm.active(1)
irq_sm.active(1)
