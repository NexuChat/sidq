# Final-film artifact and production contract

The verified upload candidate is
`/home/dev/sidq-video/artifacts/video/sidq-final-en.mp4`. Its SHA-256 is
`214e37cc232269ad1aeed1e84b2532d92deb201d8d958476f9e227306c66b8ad`,
its exact size is 21,986,302 bytes, and its container duration is
169.216 seconds — safely under three minutes. It is **not yet a submission
artifact** because the public upload gate remains owner-only. It supersedes the
2026-08-02 film (preserved as `superseded-sidq-final-en-2026-08-02.mp4`), which
predates the receipt-state change and the instrument-dark console and must not
be submitted.

Verified 2026-08-03: exactly 5,075 authored frames at 1920×1080/30 fps, a
complete `ffmpeg -v error -xerror` decode with no error, zero qualifying black
intervals, 48 kHz AAC stereo, full-film loudness −17.4 LUFS integrated at
−4.5 dBTP true peak, burned English captions plus a 59-cue sidecar SRT, and a
passing SHA-256 manifest beside the MP4 (thumbnail and contact sheet
regenerated from the same revision).

## The rebuild: real footage, one consistent voice

Four captures were recorded on this host and are pinned by content hash under
`public/v5/` with a provenance file each (command, evidence boundary, truth
label, SHA-256):

| Capture | What it shows | Label |
|---|---|---|
| `cli-audit.mp4` (39.7 s) | `sidq audit --via-mcp --budget 4` against the live catalog — the sample being examined, ranked worst-consequence first, findings and abstentions | LIVE CAPTURE |
| `cli-gate.mp4` (7.6 s) | `make gate-demo` re-deriving the committed `BLOCK`, byte-identical | REPRODUCIBLE OFFLINE REPLAY |
| `cli-verify.mp4` (10.0 s) | `sidq verify` reading the persisted receipt: `CURRENT RECEIPT · PASS · CONTINUE` | LIVE CAPTURE |
| `web-console.mp4` (30.2 s) | One continuous browser session on `sidq.mlki.app`: the contradiction, the refusal, then one real run with server output | LIVE CAPTURE |

The narrative now answers why / what / when / how: scene 1 shows the audit of
the shipped sample (why checking is needed and what is examined), scene 2 the
pre-merge gate refusing a change (when Sidq runs), scene 3 the deployed console
and the independent receipt read (how the next agent verifies), scenes 4–5 the
writeback and shared-state semantics, scene 6 where Sidq sits in a workflow.

All six chapters were re-recorded in one consistent voice:
`qwen3-tts-instruct-flash` → loudnorm −16 LUFS → 48 kHz mono PCM. The
OpenVoice owner-tone pass was **not** applied — it needs the GPU and private
reference named in the earlier provenance, neither present on this host — and
one consistent voice across six chapters was chosen over a mixed one. The full
pipeline is recorded in `narration.provenance.json` (film repository — the captures and audio masters are not shipped in this repository); the narration text is
pinned to the mastered audio by `NARRATION_RECORDED_SHA`, so editing a line
without re-recording fails the contract suite.

## Truth labels and evidence boundary

- Every frame of real footage carries one of three labels, and the label is the
  claim: **LIVE CAPTURE** (a real session against this host or the public
  surface), **REPRODUCIBLE OFFLINE REPLAY** (the committed fixture,
  re-derivable byte-for-byte by anyone), or **ILLUSTRATION** (designed motion
  that depicts semantics, never presented as a recording).
- The browser capture keeps the address bar and cursor visible, so a viewer can
  see which host answered.
- No capture is cut to hide waiting. Where a run is longer than its scene, the
  speed change is declared as a playback rate (audit ×1.45, gate ×0.9, console
  ×1.26) and the final frame is then held so the output stays readable — a
  hold, not an edit.
- Scene 1 is the live catalog audit: the sample being examined, ranked
  worst-consequence first. Scene 2 teaches the change with an illustration,
  then plays the fixture replay to its real `DECISION : BLOCK`. Scene 3 is one
  continuous session on the deployed console ending in a real independent
  receipt read: `CURRENT RECEIPT · PASS · CONTINUE`. Scenes 4–5 are labelled
  illustrations of writeback and shared-state semantics. Scene 6 closes with
  where Sidq runs.
- The Receipt read on screen consumes a receipt persisted in DataHub before
  recording; the illustrated writeback is not presented as a live mutation.

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

All local media gates have passed on the 2026-08-03 artifact. Repository
validation is checked again on the exact documentation revision before release:

- [x] Current `make check` passes, including the 80% branch-coverage gate.
- [x] Render is 1920x1080 at 30 fps, H.264/yuv420p with AAC stereo at 48 kHz.
- [x] `ffprobe` confirms a safe duration below three minutes.
- [x] A complete `ffmpeg -v error -xerror` decode succeeds.
- [x] Loudness, true peak, black frames, and per-scene stills are reviewed.
- [x] Thumbnail, contact sheet, SRT, and SHA-256 manifest sit beside the MP4.
- [x] Scene stills verified frame-accurate: real footage carries LIVE CAPTURE
  or REPRODUCIBLE OFFLINE REPLAY, illustrations carry ILLUSTRATION, and the
  designed receipt token no longer overlays real footage.
- [ ] Verify the uploaded public video is viewable without sign-in and add its public URL to the submission.

The final unchecked item is owner-only: do not upload the video, publish a URL,
edit Devpost, or press Submit without explicit owner authorization.
