# Control Center static asset licenses

All runtime assets in this directory are served locally by the developer-only
Control Center. Nothing is hotlinked.

## Provider marks

`assets/icons/anthropic.svg`, `google.svg`, and `xai.svg` are cached from the
Simple Icons 16.5.0 npm package. Simple Icons is released under CC0-1.0; the
vendored license is `assets/icons/LICENSE-simple-icons.md`. The marks identify
their respective providers and remain subject to the providers' trademark
rules. OpenAI, DeepSeek, OpenCode, OmniRoute, and Z.AI/GLM continue to use the
dashboard's clearly generic deterministic glyphs because this repository does
not carry a redistributable official asset for them.

## Terminal

`vendor/xterm/xterm.js` and `vendor/xterm/xterm.css` are copied from
`@xterm/xterm` 6.0.0. xterm.js is MIT licensed; the unmodified license is
vendored as `vendor/xterm/LICENSE`. The npm package remains a pinned development
dependency so the cached runtime files have a reproducible source.
