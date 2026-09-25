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

## Typography

`vendor/fonts/plus-jakarta-sans-latin-{400,500,600,700}-normal.woff2` and
`vendor/fonts/jetbrains-mono-latin-{400,500,600}-normal.woff2` are copied from
the `@fontsource/plus-jakarta-sans` and `@fontsource/jetbrains-mono` 5.3.0 npm
packages. Both families are licensed under the SIL Open Font License 1.1; the
unmodified licenses are vendored as `vendor/fonts/LICENSE-plus-jakarta-sans`
and `vendor/fonts/LICENSE-jetbrains-mono`. The npm packages remain pinned
development dependencies so the cached runtime files have a reproducible
source.

They are served locally for the same reason as every other asset here: the
Control Center's Content-Security-Policy is `default-src 'self'` with no
external hosts, so a hosted webfont would require weakening it. Only the latin
subset is vendored, which is what the interface renders.

