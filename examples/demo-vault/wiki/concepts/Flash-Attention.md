---
type: concept
title: "Flash-Attention"
description: "Fast and memory-efficient exact attention with IO-awareness"
status: active
tags:
  - attention
  - transformers
  - hardware
sources:
  - raw/2026-paper-kv.pdf
generated:
  by: agent:claude-sonnet
  at: '2026-10-03T10:00:00Z'
---
# Flash-Attention

FlashAttention is an IO-aware exact attention algorithm that uses tiling to reduce the number of memory reads and writes between GPU high bandwidth memory (HBM) and on-chip SRAM.

Related concepts include KV cache optimizations and [[SQLite]] storage for activations.
