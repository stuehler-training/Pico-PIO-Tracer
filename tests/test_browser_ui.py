from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from pico_pio_trace.emulator import PIOEmulator
from pico_pio_trace.parser import parse_source
from pico_pio_trace.render import render_html


CHROMIUM = shutil.which("chromium") or shutil.which("chromium-browser") or shutil.which("google-chrome")


def _wait_html() -> str:
    config = parse_source(
        """
import rp2
from machine import Pin
@rp2.asm_pio(set_init=rp2.PIO.OUT_LOW)
def p():
    wait(1, gpio, 2)
    set(pins, 1)[1]
    wait(0, gpio, 2)
    set(pins, 0)
sm=rp2.StateMachine(0, p, freq=1_000_000, set_base=Pin(3))
"""
    ).choose()
    return render_html(PIOEmulator(config).run(20), [2, 3])


@pytest.mark.skipif(CHROMIUM is None, reason="Chromium is required for the browser interaction test")
def test_clicking_gpio_waveform_adds_transition_and_resolves_wait(tmp_path: Path):
    sync_api = pytest.importorskip("playwright.sync_api")
    html = _wait_html()
    screenshot = tmp_path / "interactive.png"
    with sync_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, executable_path=CHROMIUM, args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": 1400, "height": 1000})
        errors: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.set_content(html, wait_until="load")
        page.wait_for_function("window.__PIO_TRACE_APP_READY__ === true")
        assert page.evaluate("window.__PIO_TRACE_APP__.getRecords()[0].stalled") is True
        assert page.locator("#source-code").inner_text().count("wait(1, gpio, 2)") == 1
        assert "wait(1, gpio, 2)" in page.locator(".source-line.current-source code").inner_text()
        assert page.locator("#step-back").is_disabled()

        # Continuing from a breakpoint on a currently stalled WAIT must not
        # stop again on every retry of the same PC.
        page.locator('.breakpoint-toggle[data-pc="0"]').click()
        page.locator("#continue-breakpoint").click()
        assert page.evaluate("window.__PIO_TRACE_APP__.getSelectedCycle()") == 0
        assert "No later breakpoint hit" in page.locator("#simulation-status").inner_text()
        page.locator("#clear-breakpoints").click()

        # The debugger buttons move exactly one cycle and keep the source/state
        # inspector synchronized with the waveform cursor.
        page.locator("#step-forward").click()
        assert page.evaluate("window.__PIO_TRACE_APP__.getSelectedCycle()") == 1
        assert "wait(1, gpio, 2)" in page.locator(".source-line.current-source code").inner_text()
        page.locator("#step-back").click()
        assert page.evaluate("window.__PIO_TRACE_APP__.getSelectedCycle()") == 0

        # Drive-high is selected by default. The first editable row is GPIO2;
        # click cycle 5 using the viewer's fixed SVG geometry.
        page.locator("#wave").click(position={"x": 178 + 5 * 24 + 12, "y": 52 + 17})
        page.wait_for_function("window.__PIO_TRACE_APP__.getRecords()[5].stalled === false")

        event = page.evaluate("window.__PIO_TRACE_APP__.getStimuli()[0]")
        assert event == {"cycle": 5, "type": "pin", "pin": 2, "value": 1, "note": "interactive editor"}
        assert page.evaluate("window.__PIO_TRACE_APP__.getRecords()[5].events").copy()[-1] == "WAIT condition met"
        assert page.evaluate("window.__PIO_TRACE_APP__.getRecords()[5].state_pc") == 1
        assert page.evaluate("window.__PIO_TRACE_APP__.getRecords()[6].instruction") == "set(pins, 1)[1]"
        assert page.locator("#input-events tr").count() == 1
        assert "WAIT condition met" in page.locator("#wait-detail").inner_text()
        assert "wait(1, gpio, 2)" in page.locator(".source-line.current-source code").inner_text()
        assert "PC after cycle" in page.locator("#state").inner_text()
        assert "sampled at the end" in page.locator("#debugger-moment").inner_text()

        # Step into the SET and then its delay cycle. Delay cycles remain
        # mapped to the source instruction that introduced the delay field.
        page.locator("#step-forward").click()
        assert page.evaluate("window.__PIO_TRACE_APP__.getSelectedCycle()") == 6
        assert "set(pins, 1)[1]" in page.locator(".source-line.current-source code").inner_text()
        assert page.evaluate("""() => {
            const pane = document.querySelector('#source-code').getBoundingClientRect();
            const line = document.querySelector('.source-line.current-source').getBoundingClientRect();
            return line.top >= pane.top && line.bottom <= pane.bottom;
        }""") is True
        assert page.evaluate("window.__PIO_TRACE_APP__.getRecords()[6].delay_remaining") == 1
        page.locator("#step-forward").click()
        assert page.evaluate("window.__PIO_TRACE_APP__.getRecords()[7].phase") == "delay"
        assert "set(pins, 1)[1]" in page.locator(".source-line.current-source code").inner_text()

        # Add the falling edge using the actual toolbar and waveform click.
        page.locator('.tool[data-tool="0"]').click()
        page.locator("#wave").click(position={"x": 178 + 10 * 24 + 12, "y": 52 + 17})
        page.wait_for_function("window.__PIO_TRACE_APP__.getRecords()[10].stalled === false")
        assert page.evaluate("window.__PIO_TRACE_APP__.getRecords()[10].events").copy()[-1] == "WAIT condition met"
        assert page.evaluate("window.__PIO_TRACE_APP__.getRecords()[11].instruction") == "set(pins, 0)"
        assert page.locator("#input-events tr").count() == 2

        # Undo removes the falling edge and returns the second WAIT to a stall.
        page.locator("#undo").click()
        page.wait_for_function("window.__PIO_TRACE_APP__.getStimuli().length === 1")
        page.wait_for_function("window.__PIO_TRACE_APP__.getRecords()[10].stalled === true")
        assert page.evaluate("window.__PIO_TRACE_APP__.getRecords()[10].stalled") is True

        # On a narrow viewport, stepping/jumping also scrolls the horizontal
        # waveform container so the selected-cycle band stays visible.
        page.set_viewport_size({"width": 650, "height": 1000})
        page.evaluate("document.querySelector('#scale').value = 56")
        page.locator("#draw").click()
        page.evaluate("window.__PIO_TRACE_APP__.jumpToCycle(19, false)")
        assert page.evaluate("""() => {
            const wrap = document.querySelector('#wave-wrap').getBoundingClientRect();
            const cursor = document.querySelector('.selected-cycle-band').getBoundingClientRect();
            return document.querySelector('#wave-wrap').scrollLeft > 0 && cursor.left >= wrap.left && cursor.right <= wrap.right;
        }""") is True
        page.screenshot(path=screenshot, full_page=True)
        browser.close()
    assert not errors
    assert screenshot.stat().st_size > 10_000


def _fifo_html() -> str:
    config = parse_source(
        """
import rp2
@rp2.asm_pio(out_shiftdir=rp2.PIO.SHIFT_RIGHT)
def p():
    pull(block)
    out(x, 8)
    nop()
sm=rp2.StateMachine(0, p, freq=1_000_000)
"""
    ).choose()
    return render_html(PIOEmulator(config).run(12), [])


@pytest.mark.skipif(CHROMIUM is None, reason="Chromium is required for the browser FIFO editor test")
def test_fifo_editor_adds_tx_and_rx_words_at_selected_cycles(tmp_path: Path):
    sync_api = pytest.importorskip("playwright.sync_api")
    html = _fifo_html()
    screenshot = tmp_path / "fifo_editor.png"
    with sync_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, executable_path=CHROMIUM, args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": 1100, "height": 1200})
        errors: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.set_content(html, wait_until="load")
        page.wait_for_function("window.__PIO_TRACE_APP_READY__ === true")

        # The summary shows selected-cycle FIFO level over hardware capacity,
        # rather than a static and ambiguous "4 / 4" capacity pair.
        assert page.locator("#card-fifo-usage").inner_text() == "TX 0 / 4 · RX 0 / 4"
        assert "TX 0 / 4 · RX 0 / 4" in page.locator("#fifo-level-summary").inner_text()

        # At the intermediate responsive layout, the number input must stay
        # inside its grid track and the Follow checkbox must not overlap it.
        boxes = page.evaluate("""() => {
            const cycle = document.querySelector('#fifo-cycle').getBoundingClientRect();
            const checkbox = document.querySelector('#fifo-follow-selected').getBoundingClientRect();
            const follow = document.querySelector('.fifo-follow-control').getBoundingClientRect();
            const words = document.querySelector('#fifo-values').getBoundingClientRect();
            return {
              cycleRight: cycle.right,
              checkboxLeft: checkbox.left,
              followRight: follow.right,
              wordsLeft: words.left,
              checkboxWidth: checkbox.width,
            };
        }""")
        assert boxes["cycleRight"] <= boxes["checkboxLeft"]
        assert boxes["followRight"] <= boxes["wordsLeft"]
        assert 14 <= boxes["checkboxWidth"] <= 18

        # The FIFO cycle follows the cycle debugger by default. Two words at
        # cycle 2 enter in written order; the blocking PULL consumes the first
        # in that cycle and leaves the second word in TX.
        page.evaluate("window.__PIO_TRACE_APP__.jumpToCycle(2, false)")
        assert page.locator("#fifo-cycle").input_value() == "2"
        page.locator("#fifo-values").fill("0x11, 0b10_0010")
        page.locator("#fifo-add-tx").click()
        page.wait_for_function("window.__PIO_TRACE_APP__.getRecords()[2].osr === 0x11")
        assert page.evaluate("window.__PIO_TRACE_APP__.getRecords()[2].tx_fifo_after_host") == [0x11, 0x22]
        assert page.evaluate("window.__PIO_TRACE_APP__.getRecords()[2].tx_fifo") == [0x22]
        assert page.evaluate("window.__PIO_TRACE_APP__.getRecords()[2].state_pc") == 1
        assert page.locator("#fifo-events tr").count() == 2
        assert page.locator("#fifo-events").inner_text().count("TX put") == 2
        assert page.locator("#card-fifo-usage").inner_text() == "TX 0 → 2 → 1 / 4 · RX 0 / 4"
        assert "TX 0 → 2 → 1 / 4 · RX 0 / 4" in page.locator("#fifo-level-summary").inner_text()
        assert "After host events" in page.locator("#fifos").inner_text()
        assert "0x00000011 · 0x00000022" in page.locator("#fifos").inner_text()

        # RX words are debugger injections. They are appended before the PIO
        # instruction at cycle 4 and visible in the end-of-cycle debugger state.
        page.evaluate("window.__PIO_TRACE_APP__.jumpToCycle(4, false)")
        assert page.locator("#fifo-cycle").input_value() == "4"
        page.locator("#fifo-values").fill("0x00000abc; 0x00000def")
        page.locator("#fifo-add-rx").click()
        page.wait_for_function("window.__PIO_TRACE_APP__.getRecords()[4].rx_fifo.length === 2")
        assert page.evaluate("window.__PIO_TRACE_APP__.getRecords()[4].rx_fifo_after_host") == [0xABC, 0xDEF]
        assert page.evaluate("window.__PIO_TRACE_APP__.getRecords()[4].rx_fifo") == [0xABC, 0xDEF]
        assert "0x00000abc · 0x00000def" in page.locator("#fifos").inner_text()
        assert page.locator("#fifo-events").inner_text().count("RX inject") == 2
        assert page.locator("#card-fifo-usage").inner_text() == "TX 1 / 4 · RX 0 → 2 / 4"
        assert "TX 1 / 4 · RX 0 → 2 / 4" in page.locator("#fifo-level-summary").inner_text()

        # A host RX read scheduled at cycle 5 removes the front word before the
        # PIO instruction and records the value in the host-read result list.
        page.evaluate("window.__PIO_TRACE_APP__.jumpToCycle(5, false)")
        page.locator("#fifo-rx-get").click()
        page.wait_for_function("window.__PIO_TRACE_APP__.getHostRxValues().length === 1")
        assert page.evaluate("window.__PIO_TRACE_APP__.getHostRxValues()") == [0xABC]
        assert page.evaluate("window.__PIO_TRACE_APP__.getRecords()[5].rx_fifo") == [0xDEF]
        assert page.locator("#fifo-events tr").count() == 5
        assert "RX host read" in page.locator("#fifo-events").inner_text()

        # Invalid values are rejected without changing the event list.
        page.locator("#fifo-values").fill("not-a-word")
        page.locator("#fifo-add-tx").click()
        page.wait_for_function("document.querySelector('#simulation-status').textContent.includes('FIFO editor error')")
        assert page.locator("#fifo-events tr").count() == 5

        # Undo removes the RX read, reruns the emulator, and restores both words.
        page.locator("#undo").click()
        page.wait_for_function("window.__PIO_TRACE_APP__.getHostRxValues().length === 0")
        assert page.evaluate("window.__PIO_TRACE_APP__.getRecords()[5].rx_fifo") == [0xABC, 0xDEF]
        assert page.locator("#fifo-events tr").count() == 4

        page.screenshot(path=screenshot, full_page=True)
        browser.close()
    assert not errors
    assert screenshot.stat().st_size > 10_000


@pytest.mark.skipif(CHROMIUM is None, reason="Chromium is required for the browser FIFO boundary regression test")
def test_single_tx_word_remains_visible_when_pull_consumes_it_in_same_cycle():
    sync_api = pytest.importorskip("playwright.sync_api")
    html = _fifo_html()
    with sync_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, executable_path=CHROMIUM, args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": 1100, "height": 1000})
        errors: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.set_content(html, wait_until="load")
        page.wait_for_function("window.__PIO_TRACE_APP_READY__ === true")

        page.evaluate("window.__PIO_TRACE_APP__.jumpToCycle(3, false)")
        page.locator("#fifo-values").fill("0x12345678")
        page.locator("#fifo-add-tx").click()
        page.wait_for_function("window.__PIO_TRACE_APP__.getRecords()[3].osr === 0x12345678")

        record = page.evaluate("window.__PIO_TRACE_APP__.getRecords()[3]")
        assert record["tx_fifo_after_host"] == [0x12345678]
        assert record["tx_fifo"] == []
        assert page.locator("#card-fifo-usage").inner_text() == "TX 0 → 1 → 0 / 4 · RX 0 / 4"
        fifo_text = page.locator("#fifos").inner_text()
        assert "After host events" in fifo_text
        assert "TX [1/4]: 0x12345678" in fifo_text
        assert "End of cycle" in fifo_text
        assert "TX [0/4]: (empty)" in fifo_text
        assert "0→1→0" in (page.locator("#wave").text_content() or "")

        browser.close()
    assert not errors


@pytest.mark.skipif(CHROMIUM is None, reason="Chromium is required for the responsive FIFO-layout test")
def test_fifo_controls_do_not_overlap_across_responsive_widths():
    sync_api = pytest.importorskip("playwright.sync_api")
    html = _fifo_html()
    with sync_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, executable_path=CHROMIUM, args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": 1800, "height": 900})
        errors: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.set_content(html, wait_until="load")
        page.wait_for_function("window.__PIO_TRACE_APP_READY__ === true")

        for width in (1800, 1500, 1390, 1380, 1180, 1100, 961, 960, 900, 801, 800, 650, 420):
            page.set_viewport_size({"width": width, "height": 900})
            layout = page.evaluate("""() => {
                const ids = [
                  'fifo-cycle', 'fifo-follow-selected', 'fifo-values',
                  'fifo-add-tx', 'fifo-add-rx', 'fifo-rx-get'
                ];
                const rects = Object.fromEntries(ids.map((id) => {
                  const rect = document.getElementById(id).getBoundingClientRect();
                  return [id, { left: rect.left, right: rect.right, top: rect.top, bottom: rect.bottom }];
                }));
                const overlaps = [];
                for (let first = 0; first < ids.length; first += 1) {
                  for (let second = first + 1; second < ids.length; second += 1) {
                    const a = rects[ids[first]];
                    const b = rects[ids[second]];
                    const horizontal = Math.min(a.right, b.right) - Math.max(a.left, b.left);
                    const vertical = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
                    if (horizontal > 0.5 && vertical > 0.5) overlaps.push([ids[first], ids[second]]);
                  }
                }
                const editor = document.querySelector('.fifo-editor');
                return {
                  overlaps,
                  overflow: editor.scrollWidth - editor.clientWidth,
                  checkboxWidth: document.querySelector('#fifo-follow-selected').getBoundingClientRect().width,
                };
            }""")
            assert layout["overlaps"] == [], f"overlap at viewport width {width}: {layout['overlaps']}"
            assert layout["overflow"] <= 1, f"horizontal overflow at viewport width {width}: {layout['overflow']} px"
            assert 14 <= layout["checkboxWidth"] <= 18

        browser.close()
    assert not errors


def _multi_irq_html() -> str:
    from pico_pio_trace.render import HtmlTraceOption

    parsed = parse_source(
        """
import rp2
@rp2.asm_pio(out_shiftdir=rp2.PIO.SHIFT_RIGHT)
def fifo_program():
    pull(block)
    out(x, 8)
@rp2.asm_pio()
def irq_program():
    wait(1, irq, 0)
    set(x, 1)
    irq(block, 1)
    set(x, 2)
sm0 = rp2.StateMachine(0, fifo_program, freq=1_000_000)
sm1 = rp2.StateMachine(1, irq_program, freq=1_000_000)
""",
        source_path="multi_irq.py",
    )
    fifo = PIOEmulator(parsed.choose(program_name="fifo_program")).run(12)
    irq = PIOEmulator(parsed.choose(program_name="irq_program")).run(12)
    return render_html(
        fifo,
        [0],
        trace_options=[HtmlTraceOption(fifo, [0]), HtmlTraceOption(irq, [0])],
    )


@pytest.mark.skipif(CHROMIUM is None, reason="Chromium is required for the multi-program IRQ editor test")
def test_program_selector_and_manual_irq_set_clear_preserve_per_program_edits():
    sync_api = pytest.importorskip("playwright.sync_api")
    html = _multi_irq_html()
    with sync_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, executable_path=CHROMIUM, args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": 1400, "height": 1200})
        errors: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.set_content(html, wait_until="load")
        page.wait_for_function("window.__PIO_TRACE_APP_READY__ === true")

        programs = page.evaluate("window.__PIO_TRACE_APP__.getPrograms()")
        assert [(item["program"], item["sm_id"]) for item in programs] == [("fifo_program", 0), ("irq_program", 1)]
        assert page.locator("#program-selector-wrap").is_visible()
        assert page.locator(".source-pane > #program-selector-wrap").count() == 1
        assert page.locator("header #program-selector-wrap").count() == 0
        assert page.locator("#program-selector-wrap").inner_text().startswith("PIO function to debug")
        assert page.locator("#program-select option").all_inner_texts() == [
            "fifo_program · SM 0",
            "irq_program · SM 1",
        ]
        guide = page.locator(".source-highlight-guide")
        assert "highlight guide — these are not checkboxes" in guide.inner_text().lower()
        assert "Current instruction" in guide.inner_text()
        assert "execute, stall, delay" in guide.inner_text()
        assert "Selected PIO function" in guide.inner_text()
        assert guide.locator("input").count() == 0
        assert guide.locator(".source-guide-marker").count() == 3
        assert "Breakpoint mirror" in guide.inner_text()
        footer_text = page.locator("footer").inner_text()
        assert footer_text.startswith("This tool is being provided by blog.stuehler-training.de.")
        assert page.locator(".report-license summary").inner_text() == "License information for this standalone report"
        license_text = page.locator(".report-license").text_content() or ""
        assert "GPL-3.0-or-later" in license_text
        assert page.locator(".report-license pre").text_content().lstrip().startswith("GNU GENERAL PUBLIC LICENSE")

        # Breakpoints belong to the selected PIO function, just like its
        # stimuli and viewport state.
        page.locator('.breakpoint-toggle[data-pc="1"]').click()
        assert page.evaluate("window.__PIO_TRACE_APP__.getBreakpoints()") == [1]

        # Create a FIFO edit in the first program, then switch away.
        page.evaluate("window.__PIO_TRACE_APP__.jumpToCycle(1, false)")
        page.locator("#fifo-values").fill("0x55")
        page.locator("#fifo-add-tx").click()
        page.wait_for_function("window.__PIO_TRACE_APP__.getRecords()[1].osr === 0x55")
        assert page.locator("#fifo-events tr").count() == 1

        page.select_option("#program-select", "irq_program_sm1")
        page.wait_for_function("window.__PIO_TRACE_APP__.getSelectedProgram().program === 'irq_program'")
        assert page.evaluate("window.__PIO_TRACE_APP__.getRecords()[0].stalled") is True
        assert page.locator("#irq-events .empty").count() == 1
        assert page.evaluate("window.__PIO_TRACE_APP__.getBreakpoints()") == []
        page.locator('.breakpoint-toggle[data-pc="2"]').click()
        assert page.evaluate("window.__PIO_TRACE_APP__.getBreakpoints()") == [2]

        # Set IRQ0 before cycle 2. WAIT 1 IRQ consumes and clears it in the same cycle.
        page.evaluate("window.__PIO_TRACE_APP__.jumpToCycle(2, false)")
        page.select_option("#irq-index", "0")
        page.locator("#irq-set").click()
        page.wait_for_function("window.__PIO_TRACE_APP__.getRecords()[2].stalled === false")
        record = page.evaluate("window.__PIO_TRACE_APP__.getRecords()[2]")
        assert record["irq_flags"] == 0
        assert record["state_pc"] == 1
        assert "WAIT condition met; IRQ0 cleared" in record["events"]

        # irq(block, 1) sets IRQ1 and stalls. A manual clear at cycle 6 releases it.
        page.wait_for_function("window.__PIO_TRACE_APP__.getRecords()[4].pending_kind === 'irq_wait'")
        assert page.evaluate("window.__PIO_TRACE_APP__.getRecords()[4].irq_flags") == 0b10
        page.evaluate("window.__PIO_TRACE_APP__.jumpToCycle(6, false)")
        page.select_option("#irq-index", "1")
        page.locator("#irq-clear").click()
        page.wait_for_function("window.__PIO_TRACE_APP__.getRecords()[6].stalled === false")
        assert page.evaluate("window.__PIO_TRACE_APP__.getRecords()[7].x") == 2
        assert page.locator("#irq-events tr").count() == 2
        wave_text = page.locator("#wave").text_content() or ""
        assert "IRQ0" in wave_text
        assert "IRQ flags" in wave_text

        # Switching back restores that program's independent FIFO edit/session.
        page.select_option("#program-select", "fifo_program_sm0")
        page.wait_for_function("window.__PIO_TRACE_APP__.getSelectedProgram().program === 'fifo_program'")
        assert page.evaluate("window.__PIO_TRACE_APP__.getRecords()[1].osr") == 0x55
        assert page.locator("#fifo-events tr").count() == 1
        assert page.locator("#irq-events .empty").count() == 1
        assert page.evaluate("window.__PIO_TRACE_APP__.getBreakpoints()") == [1]
        assert page.locator('#pio-disassembly .disassembly-row[data-pc="1"]').evaluate("node => node.classList.contains('has-breakpoint')") is True

        # The selector and explanatory guide stay inside the source panel at
        # desktop, tablet, and narrow-phone widths.
        for width in (1400, 800, 420):
            page.set_viewport_size({"width": width, "height": 1200})
            layout = page.evaluate("""() => {
                const pane = document.querySelector('.source-pane');
                const selector = document.querySelector('#program-selector-wrap');
                const guide = document.querySelector('.source-highlight-guide');
                const paneRect = pane.getBoundingClientRect();
                const selectorRect = selector.getBoundingClientRect();
                const guideRect = guide.getBoundingClientRect();
                return {
                  overflow: pane.scrollWidth - pane.clientWidth,
                  selectorInside: selectorRect.left >= paneRect.left - 1 && selectorRect.right <= paneRect.right + 1,
                  guideInside: guideRect.left >= paneRect.left - 1 && guideRect.right <= paneRect.right + 1,
                };
            }""")
            assert layout["overflow"] <= 1, f"source panel overflow at viewport width {width}: {layout['overflow']} px"
            assert layout["selectorInside"], f"function selector escaped source panel at viewport width {width}"
            assert layout["guideInside"], f"source guide escaped source panel at viewport width {width}"

        browser.close()
    assert not errors


def _breakpoint_html() -> str:
    config = parse_source(
        """
import rp2
@rp2.asm_pio()
def breakpoint_program():
    wrap_target()
    set(x, 1)
    set(y, 2)
    nop()[1]
    wrap()
sm = rp2.StateMachine(0, breakpoint_program, freq=1_000_000)
""",
        source_path="breakpoint_demo.py",
    ).choose()
    return render_html(PIOEmulator(config).run(16), [])


@pytest.mark.skipif(CHROMIUM is None, reason="Chromium is required for the breakpoint debugger test")
def test_disassembly_breakpoints_continue_and_clear_with_mouse_and_f5():
    sync_api = pytest.importorskip("playwright.sync_api")
    html = _breakpoint_html()
    with sync_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, executable_path=CHROMIUM, args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": 1100, "height": 1100})
        errors: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.set_content(html, wait_until="load")
        page.wait_for_function("window.__PIO_TRACE_APP_READY__ === true")

        assert page.locator("#pio-disassembly .disassembly-row").count() == 3
        assert page.locator("#breakpoint-summary").inner_text() == "0 breakpoints"
        assert page.locator("#continue-breakpoint").is_disabled()
        assert page.locator("#clear-breakpoints").is_disabled()
        assert page.locator(".disassembly-row.current-disassembly").get_attribute("data-pc") == "0"

        # A mouse click in the dedicated disassembly gutter sets PC1.
        pc1 = page.locator('.breakpoint-toggle[data-pc="1"]')
        pc1.click()
        assert page.evaluate("window.__PIO_TRACE_APP__.getBreakpoints()") == [1]
        assert pc1.get_attribute("aria-pressed") == "true"
        assert page.locator('#pio-disassembly .disassembly-row[data-pc="1"]').evaluate("node => node.classList.contains('has-breakpoint')") is True
        assert page.locator("#breakpoint-summary").inner_text() == "1 breakpoint"
        assert page.locator("#continue-breakpoint").is_enabled()
        assert page.locator("#clear-breakpoints").is_enabled()
        assert page.locator(".source-line.breakpoint-source").count() == 1
        assert "set(y, 2)" in page.locator(".source-line.breakpoint-source code").inner_text()

        # Continue behaves like a debugger: stop at the next execution of PC1,
        # then at the following loop iteration rather than the current hit.
        page.locator("#continue-breakpoint").click()
        assert page.evaluate("window.__PIO_TRACE_APP__.getSelectedCycle()") == 1
        assert page.locator(".disassembly-row.current-disassembly").get_attribute("data-pc") == "1"
        assert "Breakpoint hit at PIO PC 1, cycle 1" in page.locator("#simulation-status").inner_text()

        page.locator("#continue-breakpoint").click()
        assert page.evaluate("window.__PIO_TRACE_APP__.getSelectedCycle()") == 5

        # F5 is the keyboard equivalent of Continue.
        page.keyboard.press("F5")
        assert page.evaluate("window.__PIO_TRACE_APP__.getSelectedCycle()") == 9

        # Multiple breakpoints are ordered by actual execution, not source order.
        page.locator('.breakpoint-toggle[data-pc="2"]').click()
        assert page.evaluate("window.__PIO_TRACE_APP__.getBreakpoints()") == [1, 2]
        page.locator("#continue-breakpoint").click()
        assert page.evaluate("window.__PIO_TRACE_APP__.getSelectedCycle()") == 10
        assert page.locator(".disassembly-row.current-disassembly").get_attribute("data-pc") == "2"

        page.locator("#clear-breakpoints").click()
        assert page.evaluate("window.__PIO_TRACE_APP__.getBreakpoints()") == []
        assert page.locator("#breakpoint-summary").inner_text() == "0 breakpoints"
        assert page.locator("#continue-breakpoint").is_disabled()
        assert page.locator(".source-line.breakpoint-source").count() == 0

        browser.close()
    assert not errors
