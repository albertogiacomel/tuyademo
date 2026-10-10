from pathlib import Path

import capstone

bin_path = Path("tuya_dev_kit/firmware/stm32_factory_tuya_backup.bin")
data = bin_path.read_bytes()
md = capstone.Cs(capstone.CS_ARCH_ARM, capstone.CS_MODE_THUMB)

start_offset = 0x3D40
length = 0x50
print(f"\n--- Disassembly @ 0x{0x08000000 + start_offset:08X} ---")
for insn in md.disasm(
    data[start_offset : start_offset + length], 0x08000000 + start_offset
):
    print(f"0x{insn.address:08X}: {insn.mnemonic:8s} {insn.op_str}")
