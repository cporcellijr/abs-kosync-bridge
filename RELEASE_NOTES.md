# Release Notes - 7.6.0

**Your reading position stops drifting backwards, and a rewind you make now
sticks.** Audiobookshelf and Readest gain proper Enable switches, and a service
switched off in Settings is now switched off for everyone on the install.
Audiobooks you have moved to BookOrbit can be repointed in bulk instead of
re-matched, your books can upload themselves to Readest, and series and cover art
now come from whichever library actually holds each book.

Two of the fixes below reclaim real resources: cached Storyteller books were
keeping their full narration audio, and an open dashboard was pegging a CPU core.

## What's New

- **An Enable switch for Audiobookshelf — including per user.** Audiobookshelf was
  the one service you could not simply switch off; the only way was typing the word
  `disabled` into its server URL, which applies to everyone on the install.
  Settings → Integrations → Audiobookshelf now has the same Enable toggle every
  other service has, and each reader gets their own under Account → My
  Integrations. Stop using Audiobookshelf without taking it away from the other
  people who share your install. It stays on unless you turn it off.

- **An Enable switch for Readest, covering everything it does.** Readest had no
  server-wide switch at all — highlight sync and both book uploads were per-reader
  only. One toggle now governs all three. On by default, and each reader's own
  choices are remembered if it is ever switched off.

- **Moved your audiobooks to BookOrbit? Repoint them instead of re-matching them.**
  Settings → System → Advanced Options now has **Move Audiobooks to BookOrbit**, which
  points every already-matched book at its BookOrbit audiobook without rebuilding
  the match. Progress, alignment, highlights, KOReader links and ebook pairing all
  stay exactly as they are — only who serves the audio changes. A book moves
  automatically only when the BookOrbit copy has the same running time, so a
  different narration is never applied silently; anything ambiguous is listed for
  you to pick from, and an **Undo** button sends everything back.

- **Your books can now upload themselves to Readest, filed into their own group.**
  Under *Account → My Integrations*, **Upload matched books to Readest** sends a
  book the moment you match it, and **Upload books you are currently reading** runs
  on a timer and sends the books you are part-way through. Use either or both.
  Everything lands in a group named **BookBridge** that you can rename. Both are
  off by default, set per account, and capped (5 per sweep) so switching one on
  cannot flood a Readest account — the free plan is 500 MB, which a whole library
  will not fit but your current reading will. EPUB only. This does not sync
  reading progress to Readest; Readest already does that itself.

- **A collapsed series card now lists the books in the series.** Each row shows
  the volume number, title and its own progress bar, with the book you are up to
  highlighted. Long series show a window around that book plus a count of the rest.

- **Chapter headings now stand apart in the reading-position preview**, instead of
  running into the sentence beside them. Contributed by
  [@Kyomorie](https://github.com/Kyomorie) in #409.

- **A Backup & Restore guide, and a backup helper that is safe to run while
  BookBridge is running.** The helper now takes a proper online snapshot, verifies
  it, and saves the credential key beside it so a restored database can still
  decrypt your logins. The guide explains what actually needs backing up —
  including the Whisper transcript cache, so a re-alignment never means
  re-transcribing an audiobook. Contributed by
  [@Kyomorie](https://github.com/Kyomorie) in #410 (#343).

- **New CWA setting: "Write Kobo span bookmarks"** (on by default). Turn it off to
  send percentage only and leave the device's own bookmark alone.

## Changed

- **Creating a Storyteller edition is called the same thing everywhere.** A few corners
  still called it "Forge": the two **Forge Recovery** settings, the banner shown while
  an edition is being built, and the Storyteller permission notes on the integrations
  pages. They now say what they do. Wording only — no setting changed its meaning, its
  default, or its stored value, so there is nothing to re-enter. If you go looking for
  them, the two settings are now **Storyteller Recovery Max Wait** and **Storyteller
  Recovery Poll Interval**.

## Fixed

- **Your reading position no longer drifts backwards, and a rewind you make now
  sticks.** Two symptoms, one cause. Read forward, stop for a few minutes, and the
  book could jump back roughly a page; move a position backward deliberately — the
  sleep timer ran on, or you jumped back to re-read a chapter — and BookBridge
  dragged it forward again within a cycle or two. BookBridge writes the agreed
  position out to your other services, each of them stamps that write as "just
  updated", and on the next cycle BookBridge read its own echo back as though you
  had moved there. It now recognises the echo of its own write and refuses to let
  it overrule you. Rewinds already overwritten cannot be recovered — reapply the
  one you wanted once after upgrading and it will hold. (#413, #416)

- **Storyteller books no longer fill your disk with narration audio, or take the
  container down with them.** A readalong EPUB carries the full narration inside
  it, and most of the paths that cached one wrote the whole thing to disk — 5.4 GB
  across 33 books on one measured install, 98.6% of the cache. Opening one of those
  books could then exhaust memory and put the container in a restart loop.
  Narration is stripped as each copy is cached now, and the oversized ones you
  already have repair themselves on the next sync. Repaired books keep full
  readalong precision and open about ten times faster. (#414)

- **An open dashboard no longer keeps a CPU core busy.** The 30-second refresh was
  rebuilding the entire dashboard — including an out-of-sync calculation that reads
  a whole audiobook's alignment data per book — for numbers it does not even
  redraw. It now asks only for the figures it updates, caches the out-of-sync
  result until a position moves, and stops polling in a background tab. (#412)

- **BookOrbit ebook progress no longer lands on the audiobook file.** For a book
  with both formats, BookBridge read your position from the ebook and saved it onto
  the audiobook, so BookOrbit looked permanently behind and the audiobook's own
  progress was overwritten with ebook percentages. (#417)

- **BookOrbit and Grimmory audiobook progress can now be polled.** Listening
  progress had no poll setting at all, so it was only ever picked up on the slow
  global cycle. It now has its own **Poll Mode**, **Poll Interval** and **Wait for
  Position to Settle** options — settle mode is the one to use while listening, as
  it holds the sync back until you pause. The settle option has been added to the
  BookOrbit, Grimmory and Kavita ebook polls too, and Calibre-Web Automated now has
  poll settings in the UI rather than only in the database.

- **KOReader now opens where the audiobook actually was, not at the top of the
  chapter**, for ebooks that live in BookOrbit. Your next sync of an affected book
  repairs it. (#415)

- **A book could open at the very beginning instead of where you left off.** A
  chapter whose text sat inside styling tags, or that held no text at all, produced
  a position KOReader could not find — which came back as near-zero progress.
  Contributed by [@Kyomorie](https://github.com/Kyomorie) in #420.

- **Your Kobo no longer reverts the position BookBridge just synced.** BookOrbit,
  Grimmory and Calibre-Web Automated now receive a position the device can act on,
  not just a percentage. (#364)

- **BookBridge no longer erases the Kobo bookmarks stored in Calibre-Web
  Automated.** 7.4.1 cleared the stored bookmark on every write; BookBridge now
  writes a correct one where it can and otherwise leaves yours untouched. Bookmarks
  are restored as each book next syncs.

- **Reading and listening time is no longer counted twice on BookOrbit.** When you
  read on BookOrbit's own site it logs the session itself, and BookBridge was adding
  a second, estimated one on top. It now checks first, and no longer counts the same
  stretch twice when you switch between the ebook and the audiobook. Positions were
  never affected — only the statistics; existing duplicates stay as they are. Thanks
  to [@vfaergestad](https://github.com/vfaergestad) for the report and the diagnosis
  (#424).

- **Turning a service off in Settings now turns it off for everyone.** The
  server-wide switch was only a default, so a service an admin had switched off kept
  syncing for any reader who had switched it on — a Storyteller that was off, and
  not even running, was still contacted every minute. Calibre-Web Automated joins
  the same rule and reacts to its switch without a restart.

- **A rewind Audiobookshelf declines is no longer recorded as though it happened.**
  Contributed by [@Kyomorie](https://github.com/Kyomorie) in #421.

- **An audiobook whose files misreport their own length can be transcribed again.**
  Coverage was judged from the file header rather than the audio itself, so one part
  declaring a wrong duration failed the job before the rest was downloaded.

- **Ebook-only books now show their real covers**, taken from the library that hosts
  them and served through BookBridge, instead of a blank gradient tile.

- **Series now come from the library that actually holds each book, and corrections
  reach the dashboard.** Series were read from Audiobookshelf and nowhere else, so
  ebook-only books never grouped at all. **Backfill Series Metadata** fills in what
  is missing, and the new **Re-check All Series** applies corrections you have made
  at the source and drops a series the library no longer reports. Both are in
  Settings → System → Advanced Options and both are safe to re-run.

- **Expanding a series, or opening a position preview, no longer stretches the cards
  beside it.** Contributed by [@Kyomorie](https://github.com/Kyomorie) in #411.

- **The Match All button stays reachable on a long queue** — the actions no longer
  scroll away with the list. (#423)

- **"Out of sync" warnings on audiobooks you moved to BookOrbit.** The drift badge
  was comparing against the frozen Audiobookshelf position the move keeps for undo,
  so a book in perfect sync could read "Out of sync by 15.0%" forever.

- **Deleting a mapping now really does force a fresh shelf-watch match** — the
  re-scan throttle outlived the mapping, so delete-correct-and-reshelve did nothing
  for up to a day.

- **Smaller fixes.** Automatic matches no longer lose their ebook hash, so a
  confident match from Grimmory, BookOrbit or Kavita is linked rather than dropped.
  BookOrbit keeps working through temporary login refresh failures. A book with no
  author in Audiobookshelf no longer fails its whole sync cycle. Audiobookshelf
  collections are created in the library that holds the book. And the log is quieter
  and more accurate: an unresolvable edition-specific position is reported once and
  then periodically instead of thousands of times, books Grimmory does not hold no
  longer warn about a Grimmory shelf, and a missing Storyteller book reports the
  status Storyteller actually returned instead of "No Response".

## Upgrading

Pull the new image and restart:

```bash
docker compose pull && docker compose up -d
```

No database migration and no KOReader plugin update in this release. 7.6.0 still
ships BridgeSync **0.6.6** — if your devices are already on it, there is nothing
to do.

## Operational Notes

- **Audiobookshelf and Readest stay on unless you switch them off**, so nothing
  changes on upgrade. If you had switched Audiobookshelf off by typing `disabled`
  into its server URL, use the new Enable toggle instead.
- **A service switched off server-wide is now off for everyone.** On the first start
  after upgrading, BookBridge switches the server-wide toggle on for any service its
  readers were actually using, so nobody goes dark. After that, switching a service
  off in Settings switches it off for everyone.
- **Oversized cached Storyteller books repair themselves** on the next sync of each
  book — nothing to delete by hand.
- **A rewind that was already overwritten is not recovered by upgrading.** Reapply
  it once after the update and it will hold.
