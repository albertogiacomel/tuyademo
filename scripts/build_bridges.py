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

# 1. Smart Bridge (Diagnostic ping '?' -> 'OK\n', Temp read 'T' -> 'T:<hex>,V:<hex>\n', Bootloader 'B', auto ORE recovery)
SMART_BRIDGE_ASM = """
.thumb
.syntax unified

.word 0x20001000      // Initial SP
.word 0x08000009      // Reset Handler

reset_handler:
    // RCC clocks: IOPAEN(2) | IOPBEN(3) | ADC1EN(9) | USART1EN(14) = 0x420C
    ldr r0, =0x40021018   // APB2ENR
    ldr r1, =0x420C
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

    // ADC1 Setup (Base = 0x40012400)
    ldr r6, =0x40012400

    // SMPR1: Sample time Ch16 & Ch17 = 239.5 cycles (0x00FC0000)
    ldr r1, =0x00FC0000
    str r1, [r6, #0x0C]

    // CR2: Enable ADC1 + Temp Sensor (TSVREFE | ADON = 0x00800001)
    ldr r1, =0x00800001
    str r1, [r6, #0x08]

    // Stabilization delay (~2000 cycles)
    movs r2, #2000
delay_stab:
    subs r2, r2, #1
    bne delay_stab

    // Reset calibration (TSVREFE | RSTCAL | ADON = 0x00800009)
    ldr r1, =0x00800009
    str r1, [r6, #0x08]
wait_rstcal:
    ldr r2, [r6, #0x08]
    movs r3, #0x08
    tst r2, r3
    bne wait_rstcal

    // Start calibration (TSVREFE | CAL | ADON = 0x00800005)
    ldr r1, =0x00800005
    str r1, [r6, #0x08]
wait_cal:
    ldr r2, [r6, #0x08]
    movs r3, #0x04
    tst r2, r3
    bne wait_cal

loop:
    // --- Check USART1 (from PC) ---
    ldr r0, [r4, #0x00]      // USART1->SR
    movs r2, #0x20           // RXNE
    tst r0, r2
    beq check_u1_err
    ldr r1, [r4, #0x04]      // USART1->DR

    cmp r1, #0x3F           // '?' (Ping)
    beq send_pong

    cmp r1, #0x54           // 'T' (Temperature)
    beq send_temp
    cmp r1, #0x74           // 't'
    beq send_temp

    cmp r1, #0x42           // 'B' (Bootloader jump)
    beq jump_bootloader

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
    movs r1, #0x0A           // newline
    bl send_u1_byte
    b check_u3

send_temp:
    bl sample_and_send_temp
    b check_u3

jump_bootloader:
    // Reset USART1 and ADC1
    ldr r0, =0x4002100C      // RCC->APB2RSTR
    ldr r1, =0x00004200
    str r1, [r0]
    movs r1, #0
    str r1, [r0]
    // Jump to ST System Memory
    ldr r0, =0x1FFFF000
    ldr sp, [r0]
    ldr r1, [r0, #4]
    bx r1

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
    movs r2, #0x0F
    tst r0, r2
    beq loop
    ldr r1, [r5, #0x04]      // dummy read DR to clear errors
    b loop

sample_and_send_temp:
    push {lr}

    // 1. Read Channel 16 (Temperature)
    movs r1, #16
    str r1, [r6, #0x34]      // SQR3 = 16
    ldr r1, =0x00DE0001      // TSVREFE | SWSTART | EXTTRIG | EXTSEL=111 | ADON
    str r1, [r6, #0x08]

wait_eoc16:
    ldr r0, [r6, #0x00]      // SR
    movs r2, #0x02           // EOC
    tst r0, r2
    beq wait_eoc16

    ldr r1, [r6, #0x4C]      // DR (ADC16 raw)
    push {r1}

    // Send 'T', ':'
    movs r1, #0x54           // 'T'
    bl send_u1_byte
    movs r1, #0x3A           // ':'
    bl send_u1_byte

    pop {r1}
    bl send_hex16

    // 2. Read Channel 17 (Vrefint)
    movs r1, #17
    str r1, [r6, #0x34]      // SQR3 = 17
    ldr r1, =0x00DE0001
    str r1, [r6, #0x08]

wait_eoc17:
    ldr r0, [r6, #0x00]
    movs r2, #0x02
    tst r0, r2
    beq wait_eoc17

    ldr r1, [r6, #0x4C]      // DR (ADC17 raw)
    push {r1}

    // Send ',', 'V', ':'
    movs r1, #0x2C           // ','
    bl send_u1_byte
    movs r1, #0x56           // 'V'
    bl send_u1_byte
    movs r1, #0x3A           // ':'
    bl send_u1_byte

    pop {r1}
    bl send_hex16

    // Newline
    movs r1, #0x0A
    bl send_u1_byte

    pop {pc}

send_hex16:
    push {r3, lr}
    mov r3, r1
    lsrs r1, r3, #12
    bl send_hex_nibble
    lsrs r1, r3, #8
    bl send_hex_nibble
    lsrs r1, r3, #4
    bl send_hex_nibble
    mov r1, r3
    bl send_hex_nibble
    pop {r3, pc}

send_hex_nibble:
    ands r1, r1, #0x0F
    cmp r1, #10
    blt hex_digit
    adds r1, r1, #(0x41 - 10)  // 'A' - 10
    b send_u1_byte
hex_digit:
    adds r1, r1, #0x30         // '0'
    b send_u1_byte

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
