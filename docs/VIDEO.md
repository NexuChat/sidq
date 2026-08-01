# Final three-minute film evidence

The final film runs 175.317 seconds. Its English narration explains Sidq's
receipt model, the pre-recording verification process, and the live public judge
surface without presenting illustrations as live product footage.

## What the film shows

- Explanatory scenes are visibly labelled **ILLUSTRATION**. They explain the
  receipt fields and optional writeback flow; they do not depict a live catalog
  mutation or a live DataHub UI session.
- The browser sequence is visibly labelled **LIVE CAPTURE**. It keeps the public
  address bar and cursor visible and labels cuts that remove command wait time.
- The live public judge surface performs an independent receipt read and shows
  `VERIFIED` for the handoff, then replays `gate-demo` and shows the deterministic
  `BLOCK` verdict.
- The receipt was written to and inspected in DataHub before recording. The film
  demonstrates the independent read of that persisted receipt; it does not claim
  to capture the original write live.
- No developer console is used in the live browser capture.

## Evidence boundary

The live handoff result is evidence that a separate public process can read the
persisted receipt and independently verify it. The fixture-backed gate result is
evidence that the committed graph, policy, and commit reproduce the expected
`BLOCK` decision. It is not presented as a live graph query, and the illustrated
writeback sequence is not presented as a live mutation.

## Final artifacts

- English narration with burned English subtitles: `sidq-v2-final-en.mp4`,
  175.317333 seconds, 1920x1080 at 30 fps, H.264 video with stereo AAC audio,
  yuv420p, and 11,785,537 bytes. SHA-256:
  `ae046469e37b812cadbbb1e474e5effc5bd64c45261a161a7d8f08d4624768b3`.
- English caption sidecar artifact: `sidq-demo.en.srt`.
- English narration with burned Arabic subtitles backup: `sidq-v2-final.mp4`,
  175.317333 seconds, 1920x1080 at 30 fps, H.264 video and AAC audio.

## Final checklist

- [x] Runtime remains under three minutes.
- [x] The English-caption final passes a full decode and matches the verified
  175.317333-second, 1920x1080, 30 fps H.264/AAC export profile.
- [x] Contact-sheet and live-result review confirms the English captions remain
  within two lines and do not cover `VERIFIED` or `BLOCK`.
- [x] The Arabic-caption backup matches the verified 175.317333-second H.264/AAC
  export profile.
- [x] Every explanatory scene is labelled **ILLUSTRATION**.
- [x] The real browser sequence is labelled **LIVE CAPTURE**.
- [x] The live sequence shows the address bar, cursor, and cut wait labels.
- [x] The live sequence shows handoff `VERIFIED` and `gate-demo` `BLOCK`.
- [x] Illustrations are not described as live DataHub UI or a live mutation.
- [x] The film distinguishes an independent receipt read from the earlier write
  and inspection.
- [x] The live browser capture contains no developer-console sequence.
- [ ] Verify the uploaded public video is viewable without sign-in and add its
  public URL to the submission.
