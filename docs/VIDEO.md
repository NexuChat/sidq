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

## Known drift: scene 3 predates the receipt-state change

**The film contains no false claim, and it is not current.** Both halves of that
matter, so both are stated.

Scene 3 is a labelled `LIVE CAPTURE` of a real session against the hosted page.
It is a true recording of what that revision produced. Since it was captured, the
receipt reader changed: `sidq verify` no longer prints `VERIFIED` for a current
`PASS` — it prints `CURRENT RECEIPT · PASS · CONTINUE`, and `NOT VERIFIED` is now
reserved for absent, stale, and unreadable receipts. The landing copy in the same
frame was also replaced with the four dispositions.

So the capture shows three strings the current code and page no longer produce:

| In the capture | Now |
|---|---|
| `VERIFIED  urn:li:dataset:(…)` | `CURRENT RECEIPT · PASS · CONTINUE  urn:li:dataset:(…)` |
| `receipt records PASS` | `receipt records PASS; continue` |
| "A current PASS continues; BLOCK, missing, or stale stops." | the four-disposition list |

The scene's narration — "It independently recomputes VERIFIED" — describes the
act rather than quoting the output, and the on-screen `VERIFIED` badge is a
designed overlay rather than part of the capture.

**Re-capturing is blocked on deployment, not on effort.** The frame is a real
browser session against `sidq.mlki.app`; it cannot honestly be re-captured until
the current revision is pushed and deployed there. Re-recording the narration is
separately blocked: the pipeline in this document requires OpenVoice V2 on the
GPU named in the provenance and the private owner reference, neither of which is
available on the build host.

The owner therefore has one decision to make before submission, and it is a real
trade, not a formality:

1. **Deploy, re-capture scene 3, re-render.** The film matches the product a
   judge will run. Costs a re-render of a currently verified artifact, and every
   document quoting the SHA must be updated.
2. **Ship the film as verified.** It stays a truthful recording of a real
   session, correctly labelled, showing an earlier revision — which is ordinary
   for a product video. A judge who watches and then runs `sidq verify` sees a
   more informative headline than the one on screen, not a contradiction.

Do not take a third path. A re-render that swaps the overlay while leaving the
captured terminal stale would make the frame internally inconsistent, and a
re-capture paired with the existing narration would put a spoken `VERIFIED` over
a screen that no longer says it.

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
- The live sequence performs an independent receipt read and shows `VERIFIED`
  for a persisted PASS/WARN Receipt. A separate `gate-demo` sequence re-derives
  the fixture-backed `BLOCK` result.
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
