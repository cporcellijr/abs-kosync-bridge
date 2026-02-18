# PLAN OF ACTION: Logging Refactor

## Task Breakdown

### Task A: Group 1 — Level/Emoji Mismatch Fixes [REFACTOR_AGENT]
Fix semantic correctness bugs where emoji contradicts log level. Files: api_clients.py, cwa_client.py, booklore_client.py, sync_manager.py (line 1291), kosync_sync_client.py, abs_ebook_sync_client.py, hardcover_sync_client.py, transcriber.py, smil_extractor.py.

### Task B: sync_manager.py Full Pass [REFACTOR_AGENT]
95 statements. Three sub-passes:
- B1: Replace all [BRACKET] tags with emoji
- B2: Convert [{abs_id}]/[{title_snip}] inline brackets to quoted format
- B3: Punctuation/variable quoting sweep

### Task C: web_server.py Targeted Pass [REFACTOR_AGENT]
87 statements. Targeted changes: [Tri-Link] tag, variable quoting sweep, trailing periods, missing emoji on line 94/145.

### Task D: Secondary Files [REFACTOR_AGENT]
api_clients.py (remainder), cwa_client.py (remainder), booklore_client.py (remainder), kosync_server.py, forge_service.py, transcriber.py (remainder), ebook_utils.py, abs_sync_client.py.

### Task E: Light Touch Files [REFACTOR_AGENT]
storyteller_api.py, hardcover_client.py, hardcover_routes.py, hardcover_sync_client.py, config_loader.py, transcription_providers.py, database_service.py.

### Task F: Verification [PM_AGENT]
Grep checks for remaining tags, level mismatches, trailing periods. Python syntax validation.

## Status
- [ ] A: Level/Emoji Mismatch Fixes
- [ ] B: sync_manager.py Full Pass
- [ ] C: web_server.py Targeted Pass
- [ ] D: Secondary Files
- [ ] E: Light Touch Files
- [ ] F: Verification
