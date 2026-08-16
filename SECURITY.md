# Security and privacy

## Reporting a vulnerability

Use the repository's private GitHub Security Advisory feature for vulnerability reports. Avoid placing exploit details or sensitive target data in a public issue.

## Data handling

Pico PIO Trace performs local static parsing and local simulation. The runtime contains no telemetry, analytics, credential access, update checker, or network client.

Generated HTML reports are self-contained, but they intentionally embed the parsed simulation model and the complete input Python source used for source-level debugging. They may also include the source path supplied on the command line and any values placed in stimulus files. Review generated reports before publishing them; do not share a report if its embedded source or stimuli contain credentials, proprietary code, personal data, or other secrets.

The footer link to `blog.stuehler-training.de` is passive and is contacted only when a user activates the link. The report does not load scripts, styles, fonts, or images from that site.

Each report includes a collapsible copy of the GNU GPLv3 text for the embedded Pico PIO Trace browser runtime. The report states separately that Pico PIO Trace does not claim ownership of the analyzed input source or trace data.

## Supported versions

Security fixes are applied to the most recent release line. Verify issues against the latest published release before reporting them.
