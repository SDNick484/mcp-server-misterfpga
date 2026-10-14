# Recorded answers from real MiSTers

`mcp-server-misterfpga doctor --dump tests/fixtures/recorded` writes one JSON file per MiSTer here:
Zaparoo Core's answers to the read-only methods (`version`, `media`, `systems`, `launchers`,
`readers`, `mappings`, a sample `media.search`), with addresses and secrets redacted.

`tests/test_recorded.py` replays each one through the server's tools, so the parsing code is
checked against what a real Core sends, not only against the simulator's idea of it. Review a
dump before committing it: it lists your game library.
