# Final-film artifact and production contract

The submission film is
`/home/dev/sidq-video/artifacts/video/sidq-final-en.mp4`, public since
2026-08-06 at <https://www.youtube.com/watch?v=W0uHsq2Kb0E> (upload and URL
publication were owner-only actions, performed by the owner). Its SHA-256 is
`b9318837f8a36fad25db310e20cfc5d5e7e71b21b66bbdcaa0c9b84ed12fa592`,
its exact size is 11,802,599 bytes, and its container duration is
171.051 seconds — safely under three minutes. It supersedes
three earlier cuts preserved beside it (the 2026-08-02 film, the 2026-08-03
morning cut, and the 2026-08-03 Ethan-voiced cut rejected by the owner after
audition); none of them may be submitted, and none were uploaded — do not
upload the video again under any other URL, because two live copies of the
submission film would leave a judge unsure which one the entry means.

Verified 2026-08-03: exactly 5,130 authored frames at 1920×1080/30 fps, a
complete `ffmpeg -v error -xerror` decode with no error, zero qualifying black
intervals, 48 kHz AAC stereo, full-film loudness −16.2 LUFS integrated at
−4.5 dBTP true peak, burned English captions plus a 54-cue sidecar SRT, and a
passing SHA-256 manifest beside the MP4 (thumbnail and contact sheet
regenerated from the same revision).

## The all-aspects rebuild: real footage in 4 of 7 chapters

Six captures and one real UI still are pinned by content hash under
`public/v5/` (film repository) with a provenance file each — command, evidence
boundary, truth label, SHA-256:

Every capture was retaken in a 2026-08-03 evening legibility pass after owner
review: terminal takes at xterm font size 22 in a 104×21 window that fills the
frame (long lines wrap honestly instead of leaving it), browser takes at
device scale 1.25–1.3 so the pages fill the frame. A final cursorless retake re-recorded every capture with the pointer excluded from the grab.

None of the capture filenames below are paths in this repository: they live
in the film source tree, which is not shipped here. What *is* shipped is the
master's identity above, which is what a reader can actually re-measure.

| Chapter | Runs | Captures, and what each is labelled on screen (these filenames are not shipped here — they live in the film source tree) |
|---|---|---|
| 1 · Catalog vs reality | 0:00 → 0:22 | `v6/site-tour.mp4` (LIVE CAPTURE) · `v6/terminal-claims.mp4` (LIVE CAPTURE) |
| 2 · Written back | 0:22 → 0:43 | `v6/datahub-native-assertion.mp4` (LIVE CAPTURE) |
| 3 · Shared receipts | 0:43 → 1:17 | `v6/terminal-swarm.mp4` (LIVE CAPTURE) |
| 4 · Catalog vs itself | 1:17 → 1:44 | `v6/site-tour.mp4` (LIVE CAPTURE) · `v6/site-catalog_vs_itself.png` (LIVE CAPTURE) |
| 5 · Deterministic refusal — the committed `BLOCK`, re-derived | 1:44 → 2:08 | `v6/site-refusal.png` (LIVE CAPTURE) · `v6/terminal-gate.mp4` (REPRODUCIBLE OFFLINE REPLAY) |
| 6 · Receipt re-read | 2:08 → 2:29 | `v6/site-receipt.png` (LIVE CAPTURE) |
| 7 · Next step | 2:29 → 2:51 | `v6/site-reproduce.png` (LIVE CAPTURE) · `v6/terminal-gate.mp4` (REPRODUCIBLE OFFLINE REPLAY) · drawn card (ILLUSTRATION) |

Three honesty properties survive from the earlier cuts because they are
properties of how the captures are taken, not of which cut they belong to. The
browser takes keep the address bar in frame, so a viewer can see which host is
answering. The offline replay is slowed to a declared playback rate of 0.92
rather than being silently cut. And chapter 6 is an independent receipt read: a
reader that did not perform the examination re-reads the context, the policy
hash and the age, and answers whether the receipt still applies at all.

Every label above is read from that capture's `*.provenance.json` at build time
into `src/v4/provenance.generated.ts`, not typed into the scene. A badge cannot
disagree with the footage it sits on, and this table cannot disagree with the
badge, because both are derived from the same file.

Four of the seven chapters carry moving live footage; all seven carry a live
capture. "Real footage" is published as the narrower of those two counts.

The film carries no burned-in caption track. The narration transcript ships as
a 54-cue sidecar SRT uploaded to YouTube as a manual English subtitle, so a
viewer can turn it off or have it machine-translated — neither of which is
possible once the words are baked into the frames.

The order answers a judge's questions in the order they arise. Chapter 1 states
the problem the catalog cannot see by looking at itself — DataHub says the
column exists, the live source says it does not. Chapter 2 answers "where does
the verdict go" before the demo asks for trust: into DataHub's own Quality tab
as a native assertion. Chapter 3 is the team case, four workers sharing nothing
but receipts. Chapter 4 turns to the catalog contradicting itself and names the
single ingestion run that wrote both halves. Chapter 5 is the refusal itself,
re-derived offline. Chapter 6 refuses to let a receipt become authority. Chapter
7 hands the whole thing to the viewer as one command.

All seven chapters are one consistent narration voice
(`en-US-AndrewNeural`, chosen by the owner after auditioning five candidates;
pronunciation map and full pipeline in the film repository's
`public/v4/audio/narration.provenance.json`). The narration text is pinned to the mastered
audio by `NARRATION_RECORDED_SHA`, so editing a line without re-recording
fails the contract suite.

## Truth labels and evidence boundary

- Every frame carries one of three labels, and the label is the claim:
  **LIVE CAPTURE** (a real session against this host or a public surface),
  **REPRODUCIBLE OFFLINE REPLAY** (the committed fixture, re-derivable
  byte-for-byte by anyone), or **ILLUSTRATION** (designed motion that depicts
  semantics, never presented as a recording).
- Both browser captures keep the address bar visible, so a viewer can see
  which host answered — the deployed console and the public GitHub thread.
- No capture is cut to hide waiting; waiting is compressed in the open. The
  audit chapter plays in three declared segments — the typed command at
  natural speed, the 37 s of silent computation at ×7.5 so the wait is visible
  but not endured, then the report printing at natural speed. The other
  declared playback rates are pull-request session ×1.4, console ×1.05,
  verify ×0.95;
  the gate replay and the agent take play at natural speed. Every terminal
  take has its sub-two-second window-open dead air trimmed, and each
  segment's final frame is then held so the output stays readable — a hold,
  not an edit. Held frames carry a slow declared push (a designed zoom of a
  real frame, never a content change), lower-third phrase chips are
  transient so no footage line stays covered, and a ten-second designed
  cold open (four idea cards, labelled ILLUSTRATION) sells the thesis before
  the first command, then dissolves into the live take already typing.
- The receipt read on screen consumes a receipt persisted in DataHub before
  recording. The receipt write itself is described, never depicted as
  occurring, and is not presented as a live mutation. The DataHub-UI still is a real screenshot of
  this project's live instance, presented with a declared designed pan, no
  content edits.
- The drawn receipt token appears only over the closing illustration chapter,
  never over real footage.
- Chapters are joined by a seven-frame dip through the dark backdrop — a felt
  edit, far below the blackdetect threshold, never a content change.

## Source and owned media

- Composition: `/home/dev/sidq-video/src/v4/`, tested by `npm run test:v4`
  (23 contract tests) and `npm run typecheck`.
- English narration: six project-controlled 48 kHz PCM masters with provenance
  in `public/v4/audio/narration.provenance.json`; burned English subtitles and
  the sidecar SRT are generated from the pinned script.
- Audio mix: narration only. The retained score source is explicitly excluded
  from both final compositions and is covered by a contract test.
- The private credentials and tokens are excluded from public assets,
  captures, and output.

## Verified artifact

- SHA-256: `b9318837f8a36fad25db310e20cfc5d5e7e71b21b66bbdcaa0c9b84ed12fa592`;
  exact size 11,802,599 bytes.
- Container duration: 171.051 seconds; 5,130 frames / 175.533333 seconds of
  authored video at 30 fps, 1920x1080, H.264 High, yuv420p, BT.709.
- Audio: AAC-LC stereo at 48 kHz; narration only; measured at −16.2 LUFS
  integrated and −4.5 dBTP.
- Captions: burned English captions plus `sidq-demo.en.srt`, 61 ordered cues.
- Complete `ffmpeg -v error -xerror` decode: pass. `ffprobe -count_frames`:
  exactly 5,130 video frames. `blackdetect`: no qualifying interval; silence
  findings are the authored chapter gaps where narration ends and the capture
  stays on screen.
- Per-scene stills from the shipped MP4 were reviewed frame-accurate. This
  review caught and killed a black scene-five hold frame in an interim render
  (extracted past the capture's last decodable frame) before packaging.
- Release package: `/home/dev/sidq-video/artifacts/video/` — MP4, sidecar SRT,
  1280x720 project-owned thumbnail, contact sheet, upload metadata, and a
  passing SHA-256 manifest (`cd artifacts/video && sha256sum -c
  sidq-video-sha256.txt`).

## Release gate

All local media gates have passed on the 2026-08-03 artifact:

- [x] Current `make check` passes, including the 80% branch-coverage gate.
- [x] Render is 1920x1080 at 30 fps, H.264/yuv420p with AAC stereo at 48 kHz.
- [x] `ffprobe` confirms a safe duration below three minutes.
- [x] A complete `ffmpeg -v error -xerror` decode succeeds.
- [x] Loudness, true peak, black frames, and per-scene stills are reviewed.
- [x] Thumbnail, contact sheet, SRT, and SHA-256 manifest sit beside the MP4.
- [x] Scene stills verified frame-accurate: real footage carries LIVE CAPTURE
  or REPRODUCIBLE OFFLINE REPLAY, illustrations carry ILLUSTRATION, and the
  designed receipt token never overlays real footage.
- [x] The owner auditioned five narrator candidates on a private preview,
  chose the shipped voice, watched the assembled cuts through several
  review rounds, and approved this artifact as the upload candidate on
  2026-08-04.
- [x] The owner uploaded the artifact and published its URL on 2026-08-06:
  <https://www.youtube.com/watch?v=W0uHsq2Kb0E>, title "Sidq — Provable
  Context Before DataHub Agents Act" on the NexuChat channel. Verified the
  same day from a clean signed-out session: privacy is public (not unlisted),
  oEmbed resolves full metadata, and the served thumbnail is a frame of the
  approved cut carrying its LIVE CAPTURE label and burned caption. The
  datacenter egress used for the check hits YouTube's bot interstitial before
  the stream itself, so end-to-end playback was confirmed by the owner on a
  residential connection.

Do not edit Devpost or press Submit without explicit owner authorization.
