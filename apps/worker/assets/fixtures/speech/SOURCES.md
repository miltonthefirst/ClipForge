# Speech fixtures

## `librivox-poe-philosophy.mp3`

A 25.8-second excerpt from a LibriVox reading of **"The Philosophy of Composition"
by Edgar Allan Poe** (1846).

| | |
| --- | --- |
| Source | [archive.org: `the_raven_and_the_philosophy_of_composition_1711_librivox`](https://archive.org/details/the_raven_and_the_philosophy_of_composition_1711_librivox) |
| File | `ravenphilosophy_01_poe_64kb.mp3`, 13.0s – 38.8s |
| Licence | **Public Domain Mark 1.0** (`creativecommons.org/publicdomain/mark/1.0/`) |
| Processing | Cut with ffmpeg, downmixed to mono, resampled to 16 kHz, re-encoded at 48 kbps |

### Why this passage

The reference transcript in `librivox-poe-philosophy.txt` is **not** derived from
running Whisper over the audio — that would be circular, and a regression test
against your own output proves nothing. It is transcribed from Poe's *published
text*, which is itself public domain, and then verified by ear against the
recording.

The window deliberately starts after the reader's announcement of the title, so
the audio is continuous prose with no metadata spoken over it.

### Punctuation and the WER comparison

The reference keeps normal punctuation and capitalisation because that is what
the published text has. The accuracy test normalises both sides — lowercasing,
stripping punctuation, collapsing whitespace — before computing word error rate,
because Whisper's punctuation is a formatting choice rather than a transcription
error and scoring it would make the threshold meaningless.

## Adding your own voice

The pipeline will ultimately process **your** speech, not a 19th-century essay
read by a volunteer, so a fixture in your own voice is a better regression
target. To add one:

1. Record 20–40 seconds of yourself reading anything. Any format ffmpeg can read.
2. Save it as `custom/voice.<ext>` in this directory.
3. Write exactly what you said to `custom/voice.txt`.

The accuracy test picks it up automatically and runs against **both** fixtures.
Nothing else needs changing.

`custom/` is **gitignored on purpose** — your voice is yours, and it should not
end up in a public repository because a test needed a fixture. If you do want to
commit it, add it deliberately with `git add -f`.
