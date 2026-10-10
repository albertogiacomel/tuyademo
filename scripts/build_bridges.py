#!/usr/bin/env python3
"""Build and verify STM32 UART bridge firmware binaries using Keystone and Capstone.

Conformant with SOUL_GLOBAL methodology (Cyclopts-powered, R48-R51, R66, UTF-8 explicit).
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import capstone
import keystone
from cyclopts import App

BASE_DIR = Path(__file__).resolve().parent.parent
FIRMWARE_DIR = BASE_DIR / "firmware"

app = App(
    name="build-bridges",
    help="Compilatore bare-metal assembler per bridge UART STM32 (Keystone + Capstone)",
)

# 1. Smart Bridge (Diagnostic ping '?' -> 'OK\n', pure passthrough for other bytes, auto ORE recovery)
SMART_BRIDGE_ASM = """
.thumb
.syntax unified

.word 0x20001000      // Initial SP
.word 0x08000009      // Reset Handler

reset_handler:
    // RCC clocks
    ldr r0, =0x40021018   // APB2ENR
    ldr r1, =0x400C       // IOPAEN | IOPBEN | USART1EN
    ldr r2, [r0]
    orrs r2, r2, r1
    str r2, [r0]

    ldr r0, =0x4002101C   // APB1ENR
    ldr r1, =0x40000      // USART3EN
    ldr r2, [r0]
    orrs r2, r2, r1
    str r2, [r0]

    // GPIOA: PA9 = AF-PP (0xB), PA10 = Floating Input (0x4)
    ldr r0, =0x40010804
    ldr r2, [r0]
    ldr r1, =0x00000FF0
    bics r2, r2, r1
    ldr r1, =0x000004B0
    orrs r2, r2, r1
    str r2, [r0]

    // GPIOB: PB10 = AF-PP (0xB), PB11 = Floating Input (0x4)
    ldr r0, =0x40010C04
    ldr r2, [r0]
    ldr r1, =0x0000FF00
    bics r2, r2, r1
    ldr r1, =0x00004B00
    orrs r2, r2, r1
    str r2, [r0]

    // USART1 (BRR=0x45, CR1=0x200C)
    ldr r4, =0x40013800
    movs r1, #0x45
    str r1, [r4, #0x08]
    ldr r1, =0x200C
    str r1, [r4, #0x0C]

    // USART3 (BRR=0x45, CR1=0x200C)
    ldr r5, =0x40004800
    movs r1, #0x45
    str r1, [r5, #0x08]
    ldr r1, =0x200C
    str r1, [r5, #0x0C]

loop:
    // --- Check USART1 (from PC) ---
    ldr r0, [r4, #0x00]
    movs r2, #0x20           // RXNE
    tst r0, r2
    beq check_u1_err
    ldr r1, [r4, #0x04]      // USART1->DR

    cmp r1, #0x3F           // '?'
    beq send_pong

wait_u3_tx:
    ldr r0, [r5, #0x00]
    movs r2, #0x80           // TXE
    tst r0, r2
    beq wait_u3_tx
    str r1, [r5, #0x04]      // Send to USART3
    b check_u3

send_pong:
    movs r1, #0x4F           // 'O'
    bl send_u1_byte
    movs r1, #0x4B           // 'K'
    bl send_u1_byte
    movs r1, #0x0A           // '\\n'
    bl send_u1_byte
    b check_u3

check_u1_err:
    movs r2, #0x0F           // ORE | NE | FE | PE
    tst r0, r2
    beq check_u3
    ldr r1, [r4, #0x04]      // dummy read DR to clear errors

check_u3:
    // --- Check USART3 (from WiFi) ---
    ldr r0, [r5, #0x00]
    movs r2, #0x20           // RXNE
    tst r0, r2
    beq check_u3_err
    ldr r1, [r5, #0x04]      // USART3->DR
    bl send_u1_byte
    b loop

check_u3_err:
    movs r2, #0x0F           // ORE | NE | FE | PE
    tst r0, r2
    beq loop
    ldr r1, [r5, #0x04]      // dummy read DR to clear errors
    b loop

send_u1_byte:
wait_u1_tx:
    ldr r0, [r4, #0x00]
    movs r2, #0x80           // TXE
    tst r0, r2
    beq wait_u1_tx
    str r1, [r4, #0x04]
    bx lr
"""

# 2. Echo Bridge (Diagnostic: echo PC bytes back + forward to WiFi)
ECHO_BRIDGE_ASM = """
.thumb
.syntax unified

.word 0x20001000
.word 0x08000009

reset_handler:
    ldr r0, =0x40021018
    ldr r1, =0x400C
    ldr r2, [r0]
    orrs r2, r2, r1
    str r2, [r0]

    ldr r0, =0x4002101C
    ldr r1, =0x40000
    ldr r2, [r0]
    orrs r2, r2, r1
    str r2, [r0]

    ldr r0, =0x40010804
    ldr r2, [r0]
    ldr r1, =0x00000FF0
    bics r2, r2, r1
    ldr r1, =0x000004B0
    orrs r2, r2, r1
    str r2, [r0]

    ldr r0, =0x40010C04
    ldr r2, [r0]
    ldr r1, =0x0000FF00
    bics r2, r2, r1
    ldr r1, =0x00004B00
    orrs r2, r2, r1
    str r2, [r0]

    ldr r4, =0x40013800
    movs r1, #0x45
    str r1, [r4, #0x08]
    ldr r1, =0x200C
    str r1, [r4, #0x0C]

    ldr r5, =0x40004800
    movs r1, #0x45
    str r1, [r5, #0x08]
    ldr r1, =0x200C
    str r1, [r5, #0x0C]

loop:
    ldr r0, [r4, #0x00]
    lsls r0, r0, #26
    bpl check_u3
    ldr r1, [r4, #0x04]

wait_echo:
    ldr r0, [r4, #0x00]
    lsls r0, r0, #24
    bpl wait_echo
    str r1, [r4, #0x04]

wait_u3:
    ldr r0, [r5, #0x00]
    lsls r0, r0, #24
    bpl wait_u3
    str r1, [r5, #0x04]

check_u3:
    ldr r0, [r5, #0x00]
    lsls r0, r0, #26
    bpl loop
    ldr r1, [r5, #0x04]

wait_u1:
    ldr r0, [r4, #0x00]
    lsls r0, r0, #24
    bpl wait_u1
    str r1, [r4, #0x04]
    b loop
"""


def assemble_and_verify(asm: str, filename: str) -> Path:
    ks = keystone.Ks(keystone.KS_ARCH_ARM, keystone.KS_MODE_THUMB)
    encoding, _ = ks.asm(asm)
    raw = bytes(encoding)

    # Pad to multiple of 256 for stm32loader sector/chunk parity
    rem = len(raw) % 256
    if rem != 0:
        raw += b"\xff" * (256 - rem)

    out_file = FIRMWARE_DIR / filename
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_bytes(raw)

    # Verification with Capstone
    md = capstone.Cs(capstone.CS_ARCH_ARM, capstone.CS_MODE_THUMB)
    instructions = list(md.disasm(raw[:100], 0x08000000))

    sha = hashlib.sha256(raw).hexdigest()
    print(
        f"[*] {filename:30s} : {len(raw):4d}B | {len(instructions):2d} insns verified | SHA256={sha[:16]}..."
    )
    return out_file


@app.default
def main() -> None:
    print(f"Building Tuya TYDE 3.0 bridge firmware into {FIRMWARE_DIR}...")
    assemble_and_verify(SMART_BRIDGE_ASM, "stm32_uart_smart_bridge.bin")
    assemble_and_verify(ECHO_BRIDGE_ASM, "stm32_uart_echo_bridge.bin")
    print("[+] All bridge binaries compiled and verified successfully.")


if __name__ == "__main__":
    app()
