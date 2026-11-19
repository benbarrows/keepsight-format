# Keepsight Package Format

Version 1 (draft, November 2026). Status: describes the packages Keepsight produces today.

A Keepsight package is a ZIP file that holds what was said in a video and what was on screen,
on the video's own clock, in a form an AI agent or a program can read without Keepsight. This
document says what is in a package and how to check that a package is intact. It does not
describe how Keepsight makes packages; that stays private.

The reference reader and verifier, `keepsight_package.py`, follows this document and shares no
code with Keepsight's service.

The key words MUST, MUST NOT, SHOULD and MAY are used as in RFC 2119.

## 1. Two layouts

Keepsight makes packages from two kinds of source, and each has its own layout:

| Layout | Made from | Recognised by | Integrity record |
|---|---|---|---|
| **Upload** | a video file the user uploaded | `export.json` at the root, `schema_version` `"2.0"` | `export.json` lists every other file with its SHA-256 and size |
| **Link** | a public YouTube video, read by address | no `export.json`; `manifest.json` `schema_version` `"youtube-evidence-gemini-url-v2"` (or `-v1`) | `manifest.json` records SHA-256 and size for `analysis.json` and the cover image |

A reader MUST decide the layout from these markers only: `export.json` present means upload;
otherwise `manifest.json`'s `schema_version` names a link version. Anything else is not a
Keepsight package. A reader MUST refuse a `schema_version` it does not know rather than guess.

Both layouts share the ideas in section 2. Section 8 lists where they differ and what is
planned.

## 2. Common rules

### 2.1 Time

Every time is an integer number of **microseconds** on the video's playback clock, starting
at 0 at the start of the video, in fields that end in `_us`. A span is `start_us` (included)
to `end_us` (excluded); `end_us` is always greater than `start_us`. No time lies past the
video's length (upload: `manifest.json` `duration_us`; link: `manifest.json`
`analysis.clip_end_us`).

Times come from the model that read the video unless a field says otherwise. They are
estimates and can be off by seconds; frames are taken at those times (section 3.6).

### 2.2 Files

- JSON files are UTF-8, one JSON value per file. No object repeats a key, and no number is
  `NaN` or infinite; a reader SHOULD refuse a file that breaks either rule.
- JSONL files are UTF-8, one JSON object per line. Only `\n` ends a line: U+2028, U+0085
  and the like can appear raw inside a JSON string and are text, not line ends. Blank lines
  are ignored. An empty JSONL file means no records.
- Markdown (`.md`) and text (`.txt`) files are UTF-8 prose for people and agents. Their
  wording can change in any version; programs SHOULD NOT parse them.
- `transcript.vtt` is WebVTT, UTF-8, with `\n` line ends. In a link package its cues are
  the transcript's lines (5.2). In an upload package it is a convenience copy for players:
  its times are cut to whole milliseconds, and some upload packages carry one with no cues.
  The JSONL keeps the exact values.
- SHA-256 values are 64 lowercase hexadecimal characters.

### 2.3 The ZIP

A package ZIP is plain, so that every ZIP tool sees the same files:

- The file is its members, one after another from its first byte, then the central
  directory, then the end record, with nothing before, between or after them. The end record
  has no comment. No ZIP64 (no package needs it), no spanning.
- Each member is marked as a plain file, whatever system the ZIP says made it: its mode
  (the high 16 bits of the external attributes) is 0 or a regular file with no execute,
  setuid, setgid or sticky bit; the low 16 bits (MS-DOS attributes) and the internal
  attributes are 0. So no directory entries, links or devices. It has no encryption, no extra
  fields, no comment and no data descriptor (general purpose flags 0, or only the UTF-8 flag),
  and needs at most version 2.0. Compression is stored or deflate. Its local header agrees
  with its central directory entry on version needed, flags, method, CRC-32, both sizes and
  name.
- A deflated member's data is exactly one deflate stream: it ends where the member's
  compressed size ends and inflates to exactly its declared size, with that CRC-32.
- Names are ASCII: letters, digits, `.`, `_` and `-`, with `/` between parts. No part is
  empty, starts with `.` or `-`, or ends with `.` (so no `..`, and no `.git` or other tool
  folder).
  No part is a name Windows keeps for a device (`CON`, `PRN`, `AUX`, `NUL`, `COM0` to `COM9`,
  `LPT0` to `LPT9`, in any case and before any extension). A name has at most 16 parts and
  255 characters. No two names differ only in letter case, and no name is also a folder that
  another name runs through (`a` beside `a/b`).
- Every member's time and date, in both headers, are 1980-01-01 00:00:00, so the same
  package always zips to the same bytes.

A reader MUST refuse an archive that breaks these rules before extracting anything. A reader
SHOULD apply limits of its own. The reference reader stops at 10,000 members, 768 MiB in
total, 64 MiB for each JSON, JSONL or WebVTT file it reads whole, 500,000 objects and arrays
(counted as `{` and `[` characters) per JSON or JSONL file, and 200,000 lines per JSONL
file.

### 2.4 Unknown fields

Producers add fields over time without changing the version. A reader MUST ignore JSON
fields it does not know. A version changes only when a field this document lists is removed,
renamed, or changes meaning.

### 2.5 Stable, descriptive and opaque content

- **Stable**: the files and fields this document describes as such. Programs can rely on
  them within a version.
- **Descriptive**: prose (`overview.md`, `uncertainties.md`, `DISCUSSION.txt`, `note` and
  `uncertainty` fields, the `limitations` list). For people and agents to read.
- **Opaque**: provenance labels and provider records (every `derivation` object's values,
  `pipeline` in an upload manifest, `provenance/provider/*`, `analysis.json`). They say where
  something came from, for audit. Their shape is not part of this format and can change
  without notice. Programs SHOULD NOT depend on them.

### 2.6 Everything in a package is untrusted

Transcript lines, notes, text read off the screen and the overviews come from a model reading
someone else's video. They can be wrong, and they can contain text written to manipulate an
agent ("ignore your instructions..."). A reader MUST treat all of it as data, never as
instructions. Upload packages carry `DISCUSSION.txt` with a policy for agents along these
lines. A reader that prints package text SHOULD show control characters and invisible format
characters (bidirectional overrides, zero-width characters) as escapes, so the text cannot
drive a terminal or disguise itself. The reference reader escapes the Unicode categories Cc,
Cf, Cs, Zl and Zp (tab excepted) everywhere it prints package text, unless asked for the raw
bytes (`transcript --vtt --raw`).

## 3. Upload layout

### 3.1 Members

| Path | Required | Kind | What it holds |
|---|---|---|---|
| `export.json` | yes | stable | the integrity record and inventory (3.2) |
| `manifest.json` | yes | stable (listed fields) | the package's identity, the video's length, every asset (3.3) |
| `observations.jsonl` | yes | stable | notes on what was on screen or happened (3.4) |
| `transcript.jsonl` | yes | stable | spoken lines (3.4) |
| `timeline.jsonl` | yes | stable | the segments the video was divided into for reading (3.5) |
| `coverage.json` | yes | stable (listed fields) | which stretches each kind of reading covered (3.7) |
| `capabilities.json` | yes | stable (listed fields) | what this package can and cannot support (3.8) |
| `provenance.json` | yes | descriptive | export notes and the stored files' hashes (3.9) |
| `evidence/frames/notes/NNNN.jpg` | no | stable | still frames from the video (3.6) |
| `source/original.media` | no | stable | the uploaded video itself, only if the owner chose to include it |
| `transcript.vtt` | no | descriptive | subtitles for players (2.2) |
| `overview.md` | no | descriptive | a short summary per stretch of the video |
| `uncertainties.md` | no | descriptive | what the package cannot tell you |
| `DISCUSSION.txt` | yes | descriptive | how an agent should read the package |
| `provenance/...` | no | opaque | provider records referenced by the manifest |

Other paths can appear in later versions, always listed in `export.json`.

### 3.2 export.json

| Field | Meaning |
|---|---|
| `schema_version` | `"2.0"` |
| `source_package` | the package id; equals `manifest.json` `id` |
| `revision` | equals `manifest.json` `revision` |
| `files` | one entry per member except `export.json`: `path`, `sha256`, `bytes` |
| `assets` | one entry per asset in `manifest.json`: `id`, `path`, `kind`, `availability` (`"included"` or `"omitted"`), `reason` (why it was omitted, else `null`) |
| `images` | the manifest's asset entries for the still frames and crops included, one each |
| `record_counts` | number of records in each JSONL file, by file name |
| `omitted_files` | optional files that were not made for this package, with a reason |
| `capabilities` | a copy of `capabilities.json` |

### 3.3 manifest.json

Stable fields:

| Field | Meaning |
|---|---|
| `schema_version` | `"1.0"`, `"1.1"` or `"1.2"` (the stored package's own version) |
| `id`, `revision` | the package's identity |
| `status` | `"ready_full"`, `"ready_reduced"` (read directly by a model, with the limits stated in `uncertainties.md`) or `"partial"` |
| `duration_us` | the video's length |
| `source` | about the uploaded file: `title` (its file name), `sha256`, `bytes` |
| `assets` | every piece of media the package knows of, included or not: `id`, `kind` (`original`, `frame`, `crop`, `audio`, `clip`), `path`, `sha256`, `bytes`, `mime_type`, `start_us`, `end_us`, `width`, `height`, `parent_id` (the asset it was made from), `transform` (descriptive: how it was made) |
| `references` | sources referred to by address rather than held, each with `id`, `url`, times |
| `retention` | what happens to the package. Hosted exports say `"policy": "kept_until_deleted"` |
| `limitations` | descriptive list |

`pipeline` and every other field is opaque.

### 3.4 Records: observations.jsonl and transcript.jsonl

Each line is one record:

| Field | Meaning |
|---|---|
| `id` | unique across both files |
| `start_us`, `end_us` | the span the record is about |
| `category` | `derived_transcription` (spoken words; the transcript file), `extracted_text` (text read off the screen), `audiovisual_observation`, `presenter_claim` (what the speaker asserts, not verified), `interpretation` |
| `modality` | `audio`, `visual`, `audiovisual` or `text` |
| `text` | the words |
| `evidence_ids` | ids of assets or references that back the record. A note with a still frame names it here; every id MUST exist in `manifest.json` |
| `uncertainty` | descriptive |
| `supersedes` | ids of records this one replaces |
| `derivation` | opaque provenance. `derivation.independently_verified` is `false` for every model record |

To show a note with its picture: for each id in `evidence_ids`, find the asset in
`manifest.json`; if `export.json` marks it `included` and its `kind` is `frame` or `crop`,
its `path` is the image.

### 3.5 timeline.jsonl

Segments the video was divided into: `id`, `start_us`, `end_us`, and the `core_start_us` /
`core_end_us` each segment is responsible for (segments overlap slightly).

### 3.6 Still frames

`evidence/frames/notes/NNNN.jpg` are JPEG stills decoded from the uploaded video, at most one
per on-screen note and at most 40 per video. Each frame's manifest entry gives `start_us`
(the first frame at or after the note's time), `width` and `height`. A frame is a sample at
the time the model gave, not proof that the note is right: open it before relying on it.

### 3.7 coverage.json

`duration_us` (equals the manifest's) and `tracks`: one entry per kind of reading, each with
`state` (`complete`, `partial`, `unprocessed`, `absent_source`, `unavailable`), `intervals`
covered, `missing` intervals, and descriptive `notes`. Stretches no request could read appear
in `missing`.

### 3.8 capabilities.json

Booleans and labels a program can check before making claims, among them `native_images`
(the package holds still frames), `retained_original` (it holds the video),
`provider_transcript`, `transcript_independently_verified` (always `false` today),
`automatic_reinspection` (`false`), `source_content_is_untrusted` (`true`).

### 3.9 provenance.json

Descriptive. Its `source_file_sha256` are hashes of the files **as stored before export**.
Export rewrites JSON and JSONL files (sorted keys, redactions) and can correct wording, so
these hashes differ from the exported files on purpose. A verifier MUST NOT check the package
against them; `export.json` is the record for that. `redacted_fields` lists metadata fields
the exporter changed to remove credentials or local paths.

## 4. Link layout

A link package is made from a public YouTube video that Keepsight read by its address.
Keepsight never held a copy of the video, so a link package has no frames of the video and
no `export.json`.

### 4.1 Members

| Path | Required | Kind | What it holds |
|---|---|---|---|
| `manifest.json` | yes | stable (listed fields) | identity, source, transcript, notes, receipts (4.2) |
| `transcript.jsonl` | yes | stable | spoken lines: `start_us`, `end_us`, `text` |
| `transcript.vtt` | yes | stable | subtitles of `transcript.jsonl` |
| `observations.jsonl` | v2 only | stable | on-screen notes (4.3) |
| `analysis.json` | yes | opaque | the provider's record of the reading |
| `assets/thumbnails/<sha256>.jpg` | no | stable | images YouTube publishes for the video, untimed: v2 holds only the cover, v1 up to four (three stills and the cover) |

Nothing else appears in a link package.

### 4.2 manifest.json

Stable fields:

| Field | Meaning |
|---|---|
| `schema_version` | `"youtube-evidence-gemini-url-v2"` (since November 9, 2026) or `-v1` (no notes file) |
| `source` | `video_id` and `canonical_url` (`https://www.youtube.com/watch?v=<video_id>`) |
| `analysis.clip_end_us` | the end of the stretch read; every time is at or before it |
| `analysis.estimated_length_seconds` | the video's length, estimated |
| `analysis.not_analysed` | stretches no request could read: `start_us`, `end_us`, `reason` |
| `analysis.source_bytes` | `"not_held_by_keepsight"` |
| `transcript.segments` | the same lines as `transcript.jsonl`, in order, not overlapping |
| `transcript.coverage` | `complete_reported`, `partial`, `no_speech_reported` or `unavailable`: what the model reported, never a measurement |
| `transcript.language` | as reported by the model (`und` when it did not say) |
| `observations` | the same notes as `observations.jsonl`, or an empty list when they did not fit (then `on_screen_notes` says so) |
| `analysis_response` | `sha256` and `size_bytes` of `analysis.json` |
| `thumbnails` | per image: `sha256`, `size_bytes`, `package_path`, `name`, `source_url` |
| `export_status` | `transcript_complete_reported` or `partial` |
| `source_video_included` | `false` |
| `frames`, `on_screen_notes` | descriptive |

Everything under `analysis` not listed here, and every other field, is opaque.

### 4.3 observations.jsonl (v2)

One note per line: `id`, `start_us`, `end_us`, `category` (as in 3.4, without
`derived_transcription`), `modality`, `text`, `uncertainty`, `watch_url` (the public video
opened at that second), `evidence_ids` (always empty: there is no frame to point at), and an
opaque `derivation`.

## 5. Verifying a package

Verification tells you the files are the ones the package lists and that they agree with each
other. Section 6 says what it does not tell you.

### 5.1 Upload layout

1. Check the archive against 2.3.
2. Read `export.json`. Its `files` entries MUST have safe, unique paths, a SHA-256 and a byte
   count, and MUST NOT list `export.json`.
3. The package's members other than `export.json` MUST be exactly the listed paths: nothing
   missing, nothing extra.
4. Each member's bytes MUST have the listed length and SHA-256.
5. `export.json` `source_package` and `revision` MUST be present and equal `manifest.json`
   `id` (a non-empty string) and `revision` (an integer).
6. `export.json` `assets` MUST name exactly the manifest's assets, with the same `path` and
   `kind`, and every asset's span MUST be valid and inside the video (2.1). An `included`
   asset's file MUST be present with the manifest's `sha256` and
   `bytes`; an `omitted` asset's file MUST be absent and have a `reason`. `export.json`
   `images` MUST name exactly the included assets of kind `frame` or `crop`.
7. `record_counts` MUST be an object giving an integer for each JSONL file, and each file
   MUST hold that many records. Every record MUST have a valid span
   (2.1). Every observation and transcript record MUST have a `text` string, an id that is a
   string unique across both files, and an `evidence_ids` list whose every entry is a string
   naming an asset or reference in the manifest.
8. `coverage.json` MUST be an object whose `duration_us` equals the manifest's.
9. Every file 3.1 marks as required MUST be present. `transcript.vtt`, if present, MUST be
   UTF-8 with `\n` line ends and no byte order mark, and its first line MUST be `WEBVTT`,
   alone or followed by a space or tab and more text.

Step 6 also catches a forger who changes a frame and its `export.json` line but not the
manifest. One who rewrites both is not caught (see 6).

### 5.2 Link layout

1. Check the archive against 2.3.
2. The members MUST be exactly those 4.1 requires for the version, plus one
   `assets/thumbnails/<sha256>.jpg` per `thumbnails` entry, with `package_path` equal to that
   name. `thumbnails` holds at most one entry in v2 and four in v1.
3. `analysis.json` MUST match `analysis_response`; each image MUST match its
   `thumbnails` entry (SHA-256 and size).
4. `source.canonical_url` MUST be the watch address of `source.video_id`.
5. `transcript.jsonl` MUST equal `manifest.json` `transcript.segments`, line for line; the
   lines MUST be in order, not overlap, and end at or before `clip_end_us`.
6. `transcript.vtt` MUST be UTF-8 WebVTT with `\n` line ends, no byte order mark, and one
   cue per transcript line, in order. Its first block (blocks are separated by blank lines) is the line `WEBVTT`
   alone, or followed by a space or tab and more text, and nothing else. A later block whose
   first line is `NOTE`, alone or followed by a space or tab, and that holds no `-->`, is a
   comment. Every other block is a cue: an optional identifier line, then
   `HH:MM:SS.mmm --> HH:MM:SS.mmm` (ASCII digits), then its text. The file holds `-->` exactly
   once per transcript line. A cue's times are its line's, rounded inward to whole
   milliseconds (start up, end down). Its text, its lines joined by a space, is the line's
   text with each run of white space made one space and the ends trimmed, then `&`, `<` and
   `>` written as `&amp;`, `&lt;` and `&gt;`. White space here is what Python's
   `str.isspace()` accepts: tab, line feed, vertical tab, form feed, carriage return,
   U+001C to U+001F, space, U+0085, U+00A0, U+1680, U+2000 to U+200A, U+2028, U+2029,
   U+202F, U+205F and U+3000.
7. If `manifest.json` `observations` is not empty, `observations.jsonl` MUST hold the same
   notes (times and text) in the same order. Every note MUST have a valid span, a `text`
   string, a unique string `id`, and an `evidence_ids` that is empty, `null` or absent.

In this layout `manifest.json`, the transcript and the notes have no recorded checksum. They
are checked against each other and by the ZIP's CRC-32, which catches damage but not a
deliberate edit.

## 6. What verification does not show

- **Who made it.** Packages are not signed. A checksum list inside the package proves the
  files match the list, not that Keepsight wrote either. Anyone can make a package that
  verifies.
- **That it is true.** A verified transcript can still have wrong words; a verified note can
  still misread a slide. Verification is about bytes, not meaning.
- **That it is safe to act on.** See 2.6.
- **That an upload's subtitles match its transcript.** In an upload package `transcript.vtt`
  is covered by `export.json`'s checksum but not compared with `transcript.jsonl` (2.2).

## 7. Versions

| Layout | Version | Change |
|---|---|---|
| Upload | export `2.0` | current |
| Upload | export `2.0`, November 2026 | some internal accounting fields are no longer exported; hosted exports state `kept_until_deleted`; `provenance.json` explains its hashes. Packages exported earlier carry extra fields, which readers ignore (2.4) |
| Link | `youtube-evidence-gemini-url-v1` | first link packages; the model's notes only inside `analysis.json`; up to four YouTube images |
| Link | `youtube-evidence-gemini-url-v2` | since November 9, 2026: `observations.jsonl`, and the cover as the only image |

## 8. Known gaps and plans

- **Two layouts.** Links and uploads are made by different parts of Keepsight. Planned: link
  packages also carry an `export.json` in the same shape as uploads, so one integrity rule
  covers both. A reader that follows this document will keep working; the link layout's own
  checks stay valid.
- **No signatures.** Planned only if users ask for proof of origin.
- **Time precision.** Times are the model's estimates (2.1).
