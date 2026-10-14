# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

KiCad hardware project (not software) for the **Phyduino Pico 2**: an Arduino-IDE-compatible RP2350 board built for University of Toronto Physics (PLRS) student workshops. There is no build system, linter or test suite. The source of truth is the KiCad files, which are in **KiCad 10.0** format (schematic version `20260306`, PCB `20260206`). Older KiCad versions can't open them.

## Design structure

- `phyduino_pico2.kicad_pro`: project file (net classes, DRC rules, plot settings).
- `phyduino_pico2.kicad_sch`: root sheet. It contains the top-level connectors/headers and three hierarchical sub-sheets:
  - `phyduino_pico2_mcu.kicad_sch` ("Microcontroller"): RP2350, QSPI flash, PSRAM, EEPROM, crystal, ADC (ADS7038), I²C IO expander (FXL6408; the "0x70" note on the sheet is left over from an earlier PCA9570).
  - `phyduino2_power_regs.kicad_sch` ("Power"): power input, eFuse/power mux (TPS2121), DC-DC regulators (AP63200).
  - `phyduino_pico2_wifi.kicad_sch` ("Wireless Communication"): ESP32-C6/C61 module.
- `phyduino_pico2_usbpd.kicad_sch` is an empty placeholder. On branch `dev_reva_no_usbpd`, the USB-PD (AP33772S) circuit has been removed. `development_rev_a` and `main` are the other branches.
- `phyduino_pico2.kicad_pcb`: 4-layer board (F.Cu, In1.Cu, In2.Cu, B.Cu). This file is about 5.6 MB, so search it with grep instead of reading the whole thing.
- `*.kicad_prl` is per-user local state and is listed in `.gitignore`, even though it's tracked. Don't change it on purpose.

KiCad files are S-expressions. When editing them as text, keep the parentheses balanced and leave UUIDs unchanged. Make larger edits in the KiCad GUI. The lock files `~*.lck` mean KiCad currently has the project open, so don't edit files on disk while they exist: KiCad will overwrite your changes when it saves.

## Libraries

There is no project-local `sym-lib-table` or `fp-lib-table`. Symbols and footprints come from global libraries on the maintainer's machine, in addition to the stock KiCad libraries:
- `[KL]`-suffixed libraries
- `[UL]`-suffixed libraries
- `phyduino_pico`

Some symbols/footprints are embedded in the design files. If a library is missing, KiCad shows it as an unresolved lib reference. That isn't necessarily a design error.

## Assembly variants (important)

Each symbol has a `Variant` field that controls which parts students solder:
- `STUDENT`: parts students assemble by hand.
- `ADV_SMT`: fine-pitch or advanced SMT parts that are pre-assembled.
- `IGNORE`: excluded from the BOM.

`ibom.config.ini` (InteractiveHtmlBom plugin) whitelists `STUDENT` and blacklists `IGNORE,ADV_SMT`, writing to `docs/bom/ibom_student.html`. `docs/bom/ibom.html` and `ibom_all.html` are the other generated BOMs. Whenever you add a part, set its `Variant`.

JLCPCB assembles the `ADV_SMT` parts. **JLCPCB build quantity: 32** boards per run (the minimum). Runs are always a multiple of 4, so check stock for 32 unless a prompt gives a larger multiple of 4.

**Digi-Key order quantities:** order what one board needs. Don't round passives up to a price break (decided 2026-10-13, DK-8).

## Generated outputs (regenerate after design changes)

- `fabrication/`: Gerbers and drill files (the PCB plot output directory is set to `fabrication/`).
- `svg/`: per-layer SVG plots.
- `docs/`: the GitHub Pages site (`docs/index.html`, CSS in `docs/style/`). It holds:
  - schematic/PCB PDFs in `docs/pdf/board/`
  - the STEP and GLB 3D models in `docs/models/`
  - iBOMs
  - datasheets for the main ICs in `docs/pdf/datasheets/`
  - design-review reports (standalone HTML) in `docs/reviews/`

  `index.html` links to `phyduino_pico_pcb_inner1.pdf` and `phyduino_pico_pcb_inner2.pdf`, but those files don't exist yet.

`kicad-cli` isn't on PATH on this machine. It's installed at `C:\Users\rmorl\AppData\Local\Programs\KiCad\10.0\bin\kicad-cli.exe`. These are the usual checks and exports (`sch export netlist --format kicadsexpr` is useful for reviewing connectivity without the GUI):

```
kicad-cli sch erc phyduino_pico2.kicad_sch
kicad-cli pcb drc phyduino_pico2.kicad_pcb
kicad-cli sch export pdf -o docs/pdf/board/phyduino_pico2-schematic.pdf phyduino_pico2.kicad_sch
kicad-cli pcb export gerbers -o fabrication/ phyduino_pico2.kicad_pcb
kicad-cli pcb export drill -o fabrication/ phyduino_pico2.kicad_pcb
kicad-cli pcb export step -o docs/models/phyduino_pico2.step phyduino_pico2.kicad_pcb
```

## Related repositories

`perc-sw/phyduino_pico` (the previous RP2040 board), `perc-sw/analog_shield2` and `perc-sw/analog_shield`.
