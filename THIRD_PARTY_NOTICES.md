# Third-party notices

Pico PIO Trace has no third-party Python runtime dependencies and does not bundle a browser framework or JavaScript library in generated reports. Except for the three files identified below, the project code, documentation, examples, and browser assets are distributed under `GPL-3.0-or-later`.

The source repository contains three files adapted from the MIT-licensed MicroPython **v1.28.0** release:

| File | Upstream material | License and copyright |
|---|---|---|
| `examples/uart_tx.py` | UART PIO declaration adapted from `v1.28.0/examples/rp2/pio_uart_tx.py` | MIT; MicroPython repository copyright notice applies, including Copyright (c) 2013-2026 Damien P. George |
| `tests/reference_micropython_encoder.py` | Instruction-word logic from `v1.28.0/ports/rp2/modules/rp2.py` | MIT; Copyright (c) 2020-2021 Damien P. George |
| `tests/test_upstream_examples.py` | Reduced PIO declarations adapted from seven official `v1.28.0/examples/rp2` files listed below | MIT; Copyright (c) 2013-2026 Damien P. George under the MicroPython root notice; the seven reviewed upstream files contain no additional per-file copyright line |

The complete applicable MicroPython MIT license text is included in [`LICENSES/MICROPYTHON-MIT.txt`](LICENSES/MICROPYTHON-MIT.txt). All three files retain SPDX and provenance headers.

Upstream references (pinned to the reviewed MicroPython v1.28.0 release):

- `https://github.com/micropython/micropython/blob/v1.28.0/examples/rp2/pio_1hz.py`
- `https://github.com/micropython/micropython/blob/v1.28.0/examples/rp2/pio_exec.py`
- `https://github.com/micropython/micropython/blob/v1.28.0/examples/rp2/pio_pinchange.py`
- `https://github.com/micropython/micropython/blob/v1.28.0/examples/rp2/pio_pwm.py`
- `https://github.com/micropython/micropython/blob/v1.28.0/examples/rp2/pio_uart_rx.py`
- `https://github.com/micropython/micropython/blob/v1.28.0/examples/rp2/pio_uart_tx.py`
- `https://github.com/micropython/micropython/blob/v1.28.0/examples/rp2/pio_ws2812.py`
- `https://github.com/micropython/micropython/blob/v1.28.0/ports/rp2/modules/rp2.py`
- `https://github.com/micropython/micropython/blob/v1.28.0/LICENSE`

## Tools used for development and testing

The following tools are optional development/build tools and are **not copied into or distributed as part of the Pico PIO Trace runtime**:

| Tool | Purpose | Upstream license information | Bundled here? |
|---|---|---|---|
| Setuptools | PEP 517 build backend | MIT | No |
| pytest | Python test runner | MIT | No |
| Playwright for Python | Optional browser-test driver | Apache-2.0 | No |
| Node.js | Optional Python/JavaScript differential-test runner | See the license and third-party notices shipped by the installed Node.js distribution | No |
| Chromium or compatible Chrome | Optional UI-test browser | See the license and third-party notices shipped by the installed browser | No |

Because these tools are not redistributed in the source archive, wheel, generated HTML, or installed Pico PIO Trace runtime, their license texts do not need to be copied into this repository merely to build or test the project. Their own terms apply if a downstream distributor bundles them with Pico PIO Trace.

`rp2` and `machine` imports in `examples/` are MicroPython target APIs contained in the sample input files. Pico PIO Trace parses those files statically and does not import those modules on the host computer.

Tool license references:

- `https://github.com/pypa/setuptools/blob/main/LICENSE`
- `https://github.com/pytest-dev/pytest/blob/main/LICENSE`
- `https://github.com/microsoft/playwright-python/blob/main/LICENSE`
