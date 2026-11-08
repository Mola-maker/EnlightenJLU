# Host-side SPI references

- `stm32_protocol.py` — SPI v1 frame/TLV pack/unpack, CRC16, status parsing.
- `maixcam_spi_link.py` — minimal host transport with retries and link-loss
  HELLO recovery. It auto-selects MaixCAM2 SPI2 (B18/B19/B20/B21) or
  MaixCAM/MaixCAM-Pro SPI4 (A22-A25).
- The MaixCAM Pro test path uses the raw-spidev implementation in
  `maixcam/pro/test/spi_link.py`; it handles `spi4@gpio` binding,
  `/dev/spidev4.0`, CRC-validated retries, and the software-SPI4 leading-byte
  glitch. Use that module for Pro hardware.
- `test_stm32_protocol.py` — host-only unit tests.

Run:

```sh
cd tools
python3 -m unittest -v test_stm32_protocol.py
```

The STM32 side implementation is in `Core/Src/app_spi_link.c`,
`Core/Src/app_perf.c`, `Core/Src/board_pwm.c`, and `Core/Src/glyph_map.c`.
