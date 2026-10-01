# Samsung Galaxy Voices for NVDA

Samsung Galaxy Voices makes compatible Samsung speech voices available locally through NVDA. The public add-on contains no Samsung speech engine or voice package. Users choose voices in an accessible manager, and the add-on downloads those packages directly from Samsung.

Installed voices work offline. The manager supports compatible original-generation voices, newer Galaxy S24 premium voices, multiple selection, background downloading, removal, local samples, live voice refresh, package sizes, and a stable progress list. NVDA exposes voice, rate, pitch, volume, spelling, interruption and Say All support. Packages which cannot generate smooth live speech in real time are withheld while compatible alternatives remain available.

## Updating Both Samsung Add-ons

Galaxy versions before 1.1.5 and TV versions before 1.0.4 can check or install the wrong add-on when both are installed. Download the latest packages from [Galaxy Releases](https://github.com/OnjLouis/samsungGalaxyVoices/releases) and [TV Releases](https://github.com/OnjLouis/samsungTVVoices/releases), install both in NVDA's add-on manager, and restart NVDA. Installed voices and settings are preserved.

## Repository layout

- `addon` contains the NVDA add-on.
- `host` contains the source for the Windows ARM64 compatibility host and its attributed upstream dependencies.
- `tests` contains public behavioural tests for interruption, host lifecycle, migration, signatures and voice management.

## Legal notice

This independent project is not affiliated with or endorsed by Samsung. Samsung owns its speech engines, voice data, product names and related components. No Samsung engine or voice is stored in this repository or distributed in the add-on. Downloads occur only after the user chooses them, and users remain responsible for applicable terms and law.

The compatibility host is based on [RNIDBG](https://github.com/zhkl0228/rnidbg) under Apache License 2.0. ARM64 translation uses [Dynarmic](https://github.com/merryhime/dynarmic). Third-party licence texts are included in the add-on.

See the [add-on manual](addon/doc/en/readme.html) for installation, controls and the complete changelog.

Simplified Chinese interface and [manual](addon/doc/zh_CN/readme.html) translations are contributed by [Alan86024](https://github.com/Alan86024), through [Pull Request 9](https://github.com/OnjLouis/samsungGalaxyVoices/pull/9).
