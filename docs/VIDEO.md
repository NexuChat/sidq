# Final-film artifact and production contract

The verified local upload artifact is
`/home/dev/sidq-video/artifacts/video/sidq-final-en.mp4`. Its SHA-256 is
`0811a494c3ee6f78f907c3f2d14908ca18df403d81e38d63093cfa7dab46beef`
and its exact size is 29,636,338 bytes. It remains a submission candidate until
the owner uploads that exact file and confirms public logged-out playback. Its
authored timeline is 169.167 seconds when rounded to milliseconds. It is **not
yet a submission artifact** because that public upload gate remains owner-only.
The older 175.317-second v2 exports and the rejected black-transition V4 render
are stale and must not be submitted.

Re-verified 2026-08-03: SHA-256 matches, size matches, `ffprobe` reports
1920×1080 at 30 fps with 48 kHz AAC and a 169.216-second container, a complete
`ffmpeg -v error -xerror` decode returns no error, and the sidecar SRT carries
54 cues.

## The film is rebuilt from the product, never the reverse

The composition is source, not an artifact to be preserved. When the product
changes, the film is regenerated against it — the product is never bent, and its
documentation is never softened, to keep an older render valid.

The current render predates the receipt-state change and is therefore
**superseded**. Scene 3 is a `LIVE CAPTURE` showing three strings the product no
longer produces:

| In the superseded render | The product now |
|---|---|
| `VERIFIED  urn:li:dataset:(…)` | `CURRENT RECEIPT · PASS · CONTINUE  urn:li:dataset:(…)` |
| `receipt records PASS` | `receipt records PASS; continue` |
| "A current PASS continues; BLOCK, missing, or stale stops." | the four dispositions |

Its narration also says "It independently recomputes VERIFIED", which describes
an output the reader no longer prints.

### Rebuild status

| Input | State |
|---|---|
| Narration script | ✅ rewritten. `s3` says the reader "recomputes for itself whether the receipt applies, and what it permits"; `s5` says a receipt "covers" rather than "holds" |
| On-screen phrases and the scene-3 badge | ✅ `VERIFIED` → `CURRENT RECEIPT · PASS · CONTINUE`, badge → `PASS · CONTINUE` |
| Typecheck and composition contract | ✅ `npm run typecheck` clean; 21 of 22 contract tests pass |
| Render toolchain | ✅ verified by rendering a still end to end |
| Narration audio for `s3` and `s5` | ⛔ needs OpenVoice V2 on the GPU named in the provenance plus the private owner reference — neither is on the build host |
| Scene 3 live capture | ⛔ needs the current revision deployed to the hosted page, so the frame can be re-captured honestly |

`s3` gained eight words against 5.7 seconds of headroom and still fits. `s5`
gained five against 1.4 seconds and, at the pacing this narration is recorded to,
no longer does — **its frame budget must be re-checked against the real measured
length once it is re-recorded**, not against an estimate. The film has room for
it: the current render is 169.2 seconds against a three-minute limit.

**The 22nd contract test fails by design, and must stay failing until the audio
is re-recorded.** `NARRATION_RECORDED_SHA` pins each mastered WAV to a hash of
the exact sentences it was recorded from, and `s3` and `s5` are marked
`RERECORD-REQUIRED`. A duration in `AUDIO_DURATIONS` is a measurement of a real
file and is only true of the words that produced it; without this, editing a line
would leave the timing-safety check validating the old chapter against the new
script, and nothing else in the suite would notice. The failure names exactly
which chapters to re-record.

The one-second timing-safety check now skips those chapters rather than passing
them. It was validating the new script against a WAV of different sentences,
which manufactured confidence instead of checking anything.

Neither blocker is a reason to ship the superseded render.

## Truth labels and evidence boundary

- Explanatory graph, writeback, swarm, and repair scenes are labelled
  **ILLUSTRATION**. They do not depict a live catalog mutation or a live DataHub
  UI session.
- The persisted-receipt browser sequence is labelled **LIVE CAPTURE** and keeps
  the address bar and cursor visible. Any removed command wait is named by cut
  wait labels rather than presented as instantaneous execution.
- The current deterministic `BLOCK` result is a newly captured browser replay
  labelled **REPRODUCIBLE OFFLINE REPLAY**. It is fixture-backed, not a live
  graph query, and it shows `critical_downstream` as the blocking rule with
  `wide_blast_radius` as supporting `WARN` evidence. It contains no
  `pii_exposure` finding.
- The live sequence performs an independent receipt read of a persisted Receipt
  and shows the reader's disposition for it. A separate `gate-demo` sequence
  re-derives the fixture-backed `BLOCK` result.
- The Receipt was written and inspected in DataHub before recording. The film
  demonstrates the independent read of that persisted Receipt; the illustrated
  writeback is not presented as a live mutation.

## Source and owned media

- Composition: `/home/dev/sidq-video/src/v4/`, tested by `npm run test:v4` and
  `npm run typecheck`.
- English narration: six project-controlled 48 kHz PCM masters with provenance
  in `public/v4/audio/narration.provenance.json`; burned English subtitles and
  a sidecar SRT are generated from the pinned script.
- Audio mix: narration only. The retained score source is explicitly excluded
  from both final compositions and is covered by a contract test.
- Current replay capture:
  `public/v4/proof/block-current.png`, SHA-256
  `6d180a347a47ad7b9f2df5593386ff5694bab9ebd051f61a3a6c8ce63789f5df`.
- The private voice reference, credentials, and tokens are excluded from public
  assets, captures, and output.

## Verified artifact

- Container duration: 169.216 seconds, safely under three minutes.
- Authored video: 5,075 frames / 169.166667 seconds at 30 fps, 1920x1080,
  H.264 High, yuv420p, limited-range BT.709.
- Audio: AAC-LC stereo at 48 kHz, approximately 189 kb/s; narration only;
  measured at -16.1 LUFS integrated and -4.5 dBTP.
- Captions: burned English captions plus `sidq-demo.en.srt`, 54 ordered cues.
- Complete `ffmpeg -v error -xerror` decode: pass. `ffprobe -count_frames`:
  exactly 5,075 video frames.
- `blackdetect`: no qualifying interval. Silence and freeze findings were
  reviewed as authored chapter gaps, the quiet closing card, evidence holds, or
  designed static compositions. Browser replay frames replace the rejected
  out-of-range transition and visibly reach the final `BLOCK` result.
- Full-film two-second contact sheets, the first 15 seconds, transitions,
  subtitles, source strings, and package text were independently reviewed. No
  actionable secret, stale `pii_exposure` result, or false live-write claim was
  found. The two source occurrences of those terms are negative assertions in
  the forbidden-output/forbidden-claim contracts.
- Release package:
  `/home/dev/sidq-video/artifacts/video/`, including the MP4, sidecar SRT,
  1280x720 project-owned thumbnail, contact sheet, upload metadata, and a
  passing SHA-256 manifest.

## Release gate

All local media gates have passed. Repository validation is checked again on
the exact documentation revision before release:

- [x] Current `make check` passes, including the 80% branch-coverage gate.
- [x] Render is 1920x1080 at 30 fps, H.264/yuv420p with AAC stereo at 48 kHz.
- [x] `ffprobe` confirms a safe duration below three minutes.
- [x] A complete `ffmpeg -v error -xerror` decode succeeds.
- [x] Loudness, true peak, silence, clipping, black frames, freezes, subtitles,
  transitions, and the first 15 seconds are reviewed.
- [x] Contact sheet, 1280x720 project-owned thumbnail, SRT, metadata, and
  SHA-256 manifest are created beside the MP4.
- [x] Frame and source scans find no password, token, private key, stale
  `pii_exposure` result, or false live-write label.
- [ ] Verify the uploaded public video is viewable without sign-in and add its public URL to the submission.

The final unchecked item is owner-only: do not upload the video, publish a URL,
edit Devpost, or press Submit without explicit owner authorization.
