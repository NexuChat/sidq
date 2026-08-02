# Final-film production contract

The submission candidate is the reproducible V4 composition in
`/home/dev/sidq-video`. Its authored timeline is 5,075 frames at 30 fps
(169.167 seconds), 1920x1080. This source contract is under three minutes, but
it is **not yet a submission artifact**: the MP4 becomes final only after the
current repository suite passes and the rendered file passes the media checks
below. The older 175.317-second v2 exports are stale and superseded.

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

## Candidate source and owned media

- Composition: `/home/dev/sidq-video/src/v4/`, tested by `npm run test:v4` and
  `npm run typecheck`.
- English narration: six project-controlled 48 kHz PCM masters with provenance
  in `public/v4/audio/narration.provenance.json`; burned English subtitles and
  a sidecar SRT are generated from the pinned script.
- Score: locally synthesized with FFmpeg; no third-party music or audio asset.
- Current replay capture:
  `public/v4/proof/block-current.png`, SHA-256
  `6d180a347a47ad7b9f2df5593386ff5694bab9ebd051f61a3a6c8ce63789f5df`.
- The private voice reference, credentials, and tokens are excluded from public
  assets, captures, and output.

## Release gate

The final artifact must satisfy all of these before this document records its
actual duration, byte size, and SHA-256:

- [ ] Current `make check` passes, including the 80% branch-coverage gate.
- [ ] Render is 1920x1080 at 30 fps, H.264/yuv420p with AAC stereo at 48 kHz.
- [ ] `ffprobe` confirms a safe duration below three minutes.
- [ ] A complete `ffmpeg -v error` decode succeeds.
- [ ] Loudness, true peak, silence, clipping, black frames, freezes, subtitles,
  transitions, and the first 15 seconds are reviewed.
- [ ] Contact sheet, 1280x720 project-owned thumbnail, SRT, metadata, and
  SHA-256 manifest are created beside the MP4.
- [ ] Frame and source scans find no password, token, private key, stale
  `pii_exposure` result, or false live-write label.
- [ ] Verify the uploaded public video is viewable without sign-in and add its public URL to the submission.

The final unchecked item is owner-only: do not upload the video, publish a URL,
edit Devpost, or press Submit without explicit owner authorization.
